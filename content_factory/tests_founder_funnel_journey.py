"""Real source rows, unknown historical timings, read-only staff access and event durability."""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.models import CommunityChatDevice
from community_chat.onboarding_models import CommunityMemberProfile
from community_chat.staff_console_views import FounderFunnelView
from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from .founder_funnel import STAGES, emit_event, journey_funnel
from .models import OrganizationContentConfig, WrittenArticle
from .website_models import WebsiteConnection

START = datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
END = START + timedelta(days=1)


class JourneyFunnelTests(TransactionTestCase):
    def founder(self, *, seconds=None, legacy=False, staff=False):
        user = User.objects.create_user(email=f'{uuid4().hex}@synthetic.test', is_staff=staff)
        User.objects.filter(pk=user.pk).update(date_joined=START)
        CommunityMemberProfile.objects.update_or_create(user=user, defaults={
            'submitted_at': START+timedelta(seconds=10), 'interests': ['startups'], 'status': 'approved'})
        profile = VibeRaisingProfile.objects.create(user=user, role='founder')
        org = Organization.objects.create(name='Synthetic', domain=f'{uuid4().hex}.synthetic.test')
        VibeRaisingCompany.objects.create(profile=profile, organization=org, name='Synthetic')
        result = {'draft_ready_v1': {'version': 1, 'revision': 'rev1', 'at': (START+timedelta(seconds=seconds)).isoformat()}} if seconds is not None else {}
        run = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, domain=org.domain,
            workflow='confirmed_topic', status='awaiting_approval' if seconds is not None or legacy else 'running', result=result)
        return user, org, run

    def report(self):
        return journey_funnel(since=START, until=END)

    def test_nearest_rank_uses_readiness_proof_not_mutable_run_clock(self):
        rows = [self.founder(seconds=seconds) for seconds in (60, 120, 900, 1800)]
        self.founder(legacy=True)
        self.founder(seconds=1, staff=True)
        for _, _, run in rows:
            ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=START+timedelta(days=20))
        report = self.report()
        self.assertEqual(report['timeToFirstDraft'], {'p50Seconds': 120, 'p90Seconds': 1800,
            'sampleSize': 4, 'unknownTimingUsers': 1})
        self.assertEqual(report['stages']['signed_up']['users'], 5)
        self.assertEqual(report['stages']['intent'], {'users': 5, 'timedUsers': 0})
        self.assertEqual(set(report['stages']), set(STAGES))
        self.assertNotIn('@synthetic', json.dumps(report))

    def test_real_package_step_copy_live_and_github_prove_their_stages(self):
        _, org, run = self.founder()
        ContentFactoryRunStep.objects.create(run=run, step_key='package_content_delivery', status='completed',
            completed_at=START+timedelta(seconds=75))
        OrganizationContentConfig.objects.create(organization=org, pillar_strategy={'founder_posted_articles': {
            run.run_id: {'copiedAt': (START+timedelta(seconds=80)).isoformat(), 'copiedRevision': 'rev1'}}})
        WrittenArticle.objects.create(organization=org, title='Synthetic', slug='synthetic', source_run_id=run.run_id,
            publish_status='live', live_verified_at=START+timedelta(seconds=90))
        WebsiteConnection.objects.create(organization=org, github_repo='synthetic/site',
            state='connected', last_verified_at=START+timedelta(seconds=100))
        report = self.report()
        for stage in ('draft_ready', 'copied', 'published', 'github_upgraded'):
            self.assertEqual(report['stages'][stage], {'users': 1, 'timedUsers': 1})
        self.assertEqual(report['timeToFirstDraft']['p50Seconds'], 75)

    def test_cancelled_and_malformed_receipts_do_not_become_timed_drafts(self):
        _, _, run = self.founder(seconds=50)
        run.status = 'cancelled'
        run.save(update_fields=['status'])
        self.assertEqual(self.report()['stages']['draft_ready']['users'], 0)
        run.status, run.result = 'running', {'draft_ready_v1': {'version': True, 'revision': 'r', 'at': START.isoformat()}}
        run.save(update_fields=['status', 'result'])
        self.assertEqual(self.report()['timeToFirstDraft']['sampleSize'], 0)
        self.assertIsNone(self.report()['timeToFirstDraft']['p90Seconds'])

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=START.isoformat())
    def test_real_delivery_survives_deletion_without_consumed_cancel_false_positive(self):
        from .founder_funnel import has_first_draft
        from .first_article_entitlement import _write
        user, org, run = self.founder()
        config = OrganizationContentConfig.objects.create(organization=org)
        clock = config.updated_at
        _write(organization_id=org.pk, suffix='consumed', user=user, reference_id=run.run_id)
        self.assertFalse(has_first_draft(org))
        run.result = {'draft_ready_v1': {'version': 1, 'revision': 'real', 'at': (START+timedelta(seconds=90)).isoformat()}}
        run.save(update_fields=['result'])
        self.assertTrue(has_first_draft(org))
        config.refresh_from_db()
        self.assertEqual(config.updated_at, clock)
        run.delete()
        self.assertTrue(has_first_draft(org))
        self.assertEqual(self.report()['timeToFirstDraft']['p50Seconds'], 90)

    def test_additive_command_keeps_original_table_and_matches_staff_query(self):
        self.founder(seconds=90)
        def command(*extra):
            out = StringIO()
            call_command('founder_funnel_report', '--since', START.isoformat(), '--until', END.isoformat(),
                         '--json', *extra, stdout=out)
            return json.loads(out.getvalue())
        original, expanded = command(), command('--journey')
        self.assertEqual(original, {k: v for k, v in expanded.items() if k != 'journey'})
        staff = User.objects.create_superuser(email=f'{uuid4().hex}@staff.synthetic.test')
        device = CommunityChatDevice.objects.create(user=staff, public_key='b'*64, status='verified')
        request = APIRequestFactory().get('/', {'since': START.isoformat(), 'until': END.isoformat()})
        force_authenticate(request, user=staff)
        request.community_chat_public_key, request.community_chat_installation_id = device.public_key, device.installation_id
        with override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=True):
            response = FounderFunnelView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['journey']['stages'], expanded['journey']['stages'])
        self.assertEqual(response.data['journey']['timeToFirstDraft'], expanded['journey']['timeToFirstDraft'])
        self.assertIn('no-store', response['Cache-Control'])

    def test_staff_gate_refuses_signed_out_founder_and_unverified_staff(self):
        founder, _, _ = self.founder()
        staff = User.objects.create_superuser(email=f'{uuid4().hex}@staff.synthetic.test')
        with override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=True):
            for user in (None, founder, staff):
                request = APIRequestFactory().get('/')
                if user:
                    force_authenticate(request, user=user)
                self.assertEqual(FounderFunnelView.as_view()(request).status_code, 403)

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True)
    def test_events_wait_for_commit_and_rollback_emits_nothing(self):
        with patch('content_factory.founder_funnel.logger.info') as log:
            with self.assertRaises(ValueError):
                with transaction.atomic():
                    User.objects.create_user(email=f'{uuid4().hex}@rollback.synthetic.test')
                    log.assert_not_called()
                    raise ValueError('rollback')
            log.assert_not_called()
            with transaction.atomic():
                user = User.objects.create_user(email=f'{uuid4().hex}@commit.synthetic.test')
                log.assert_not_called()
            fields = log.call_args.kwargs['extra']['founder_journey_event']
            self.assertEqual(fields['event'], 'signed_up')
            self.assertEqual(fields['seconds_since_signup'], 0)
            self.assertNotIn(user.email, json.dumps(fields))
            first_id = fields['event_id']
            emit_event('signed_up', user_id=user.pk, source='retry', at=timezone.now())
            self.assertEqual(log.call_args.kwargs['extra']['founder_journey_event']['event_id'], first_id)

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True)
    def test_real_run_and_package_saves_emit_required_stages_without_customer_text(self):
        with patch('content_factory.founder_funnel.logger.info') as log:
            _, org, run = self.founder(seconds=100)
            research = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, workflow='startup_autofill', status='completed')
            ContentFactoryRunStep.objects.create(run=run, step_key='package_content_delivery', status='completed', completed_at=timezone.now())
            WrittenArticle.objects.create(organization=org, title='Private customer text', slug='private',
                publish_status='live', live_verified_at=timezone.now())
            WebsiteConnection.objects.create(organization=org, github_repo='private/repo', last_verified_at=timezone.now())
        events = [call.kwargs['extra']['founder_journey_event'] for call in log.call_args_list]
        self.assertTrue({'signed_up', 'startup_created', 'first_article_started', 'queued', 'draft_ready',
                         'research_done', 'published', 'github_upgraded'}.issubset({e['event'] for e in events}))
        self.assertNotIn('Private customer text', json.dumps(events))
        self.assertNotIn('private/repo', json.dumps(events))
