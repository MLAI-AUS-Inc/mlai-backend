"""Accepted dispatches require an observed child or authenticated no-work proof."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from founder_tools.services import FounderCompanyContext
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from . import billing, founder_website_checks as checks, vibe_marketing_views as views
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org

FLAGS = {**ALL_FLAGS_ON, 'MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED': True,
    'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED': True, 'ARTICLES_FREE_SETUP_RESEARCH_ENABLED': True,
    'ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP': 2, 'COMMUNITY_CHAT_SIGNUP_ENABLED': False}
REMOTE = {'enabled': True, 'base_url': 'https://dispatch-boundary.synthetic.test',
    'api_key_configured': True, 'is_local_env': False}


@override_settings(**FLAGS)
class MissingAcceptedIdentityTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        built = make_p2_org()
        self.org, self.owner, self.config = built.organization, built.owner, built.config
        self.context = FounderCompanyContext(built.company.profile, built.company, self.org)
        self.now = timezone.now()
        ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow='confirmed_topic', status='awaiting_approval')
        self.enterContext(patch.object(views, '_roo_points_balance_for_user', return_value=0))
        self.enterContext(patch.object(views, 'get_content_factory_ai_agent_required_points', return_value=6))

    def receipt(self):
        config = OrganizationContentConfig.objects.get(pk=self.config.pk)
        return deepcopy(config.pillar_strategy['self_heal'][checks.KEY])

    def manual(self):
        return views._start_website_baseline(context=self.context, config=self.config, user=self.owner)

    def slots(self):
        return len(billing._free_setup_uses(self.owner, 'website_baseline', None)[3])

    @contextmanager
    def remote(self, *, status=202, lookup_status=503, lookup=None, accepted=None):
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            response = Mock(status_code=status, content=b'accepted synthetic response')
            if accepted == 'invalid-json': response.json.side_effect = ValueError('synthetic invalid JSON')
            else: response.json.return_value = {'status': 'queued'} if accepted is None else accepted
            return response
        def get(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            key = kwargs['params']['client_request_id']
            payload = {'client_request_id': key, **(lookup or {})}
            response = Mock(status_code=lookup_status, content=b'synthetic lookup')
            response.json.return_value = payload
            return response
        with patch.object(views, '_content_factory_remote_config', return_value=REMOTE), \
                patch.object(views, '_content_factory_headers', return_value={'X-API-Key': 'synthetic'}), \
                patch.object(views.http_client, 'post', side_effect=post) as send, \
                patch.object(views.http_client, 'get', side_effect=get) as find:
            yield send, find

    def test_accepted_unknown_keeps_scheduled_key_date_envelope_and_capacity_across_days(self):
        with self.remote() as (post, lookup):
            self.assertEqual(checks.run_founder_website_check_sweep(now=self.now)['waiting'], 1)
            first = self.receipt()
            self.assertEqual(first['state'], 'pending')
            self.assertEqual(self.slots(), 1)
            self.assertEqual(checks.run_founder_website_check_sweep(now=self.now + timedelta(days=2))['waiting'], 1)
            again = self.receipt()
            self.assertEqual((again['key'], again['date'], again['payload']), (first['key'], first['date'], first['payload']))
            self.assertEqual(post.call_count, 2)
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(post.call_args_list[0].kwargs['json'], post.call_args_list[1].kwargs['json'])
            self.assertTrue(all(call.kwargs['params']['client_request_id'] == first['key'] for call in lookup.call_args_list))
        run = ContentFactoryRun.objects.get(organization=self.org, workflow='website_baseline')
        self.assertEqual(run.run_id, first['key'])
        self.assertIs(run.run_request['dispatch_pending_resolution'], True)

    def test_later_found_identity_rebinds_the_original_provisional_row_and_its_steps(self):
        with self.remote():
            self.assertEqual(checks.run_founder_website_check_sweep(now=self.now)['waiting'], 1)
        first = self.receipt()
        original = ContentFactoryRun.objects.get(organization=self.org, workflow='website_baseline')
        original_pk = original.pk
        step = ContentFactoryRunStep.objects.create(run=original, step_key='retained-history', status='queued')
        observed_id = 'later-observed-worker-' + uuid4().hex
        with self.remote(lookup_status=200, lookup={'key_status':'dispatched','run_id':observed_id,'run_status':'running'}):
            result = checks.run_founder_website_check_sweep(now=self.now + timedelta(minutes=3))
        self.assertEqual(result['queued'], 1)
        self.assertEqual(ContentFactoryRun.objects.filter(organization=self.org, workflow='website_baseline').count(), 1)
        original.refresh_from_db(); step.refresh_from_db()
        self.assertEqual((original.pk, original.run_id, step.run_id), (original_pk, observed_id, original_pk))
        self.assertFalse(original.run_request.get('dispatch_pending_resolution', False))
        self.assertEqual(original.run_request['client_request_id'], first['key'])
        original.status = 'completed'; original.save(update_fields=['status'])
        state, claim = checks._claim(self.config.pk, self.now + timedelta(days=2))
        self.assertEqual(state, 'claimed', 'A resolved old placeholder must not block every future nightly check')
        self.assertNotEqual(claim[2]['key'], first['key'])

    def test_concurrent_callback_binding_cannot_resurrect_a_pending_placeholder(self):
        with self.remote(): checks.run_founder_website_check_sweep(now=self.now)
        first = self.receipt()
        original = ContentFactoryRun.objects.get(organization=self.org, workflow='website_baseline')
        real_bind = views.bind_dispatch_token_run
        def callback_wins(**kwargs):
            real_bind(**kwargs)
            return None  # Our subsequent contender finds no token row to bind.
        observed_id = 'callback-won-' + uuid4().hex
        with self.remote(lookup_status=200, lookup={'key_status':'dispatched','run_id':observed_id}),                 patch.object(views, 'bind_dispatch_token_run', side_effect=callback_wins):
            result = checks.run_founder_website_check_sweep(now=self.now + timedelta(minutes=3))
        self.assertEqual(result['queued'], 1)
        original.refresh_from_db()
        self.assertEqual(original.run_id, observed_id)
        self.assertFalse(ContentFactoryRun.objects.filter(run_id=first['key']).exists())
        self.assertEqual(ContentFactoryRun.objects.filter(organization=self.org, workflow='website_baseline').count(), 1)
        self.assertFalse(original.run_request.get('dispatch_pending_resolution', False))

    def test_manual_accepted_missing_identity_retains_its_original_free_slot(self):
        with self.remote(status=200): run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.status, 'blocked')
        self.assertIs(run.run_request['dispatch_pending_resolution'], True)
        self.assertEqual(self.slots(), 1)

    def test_started_lookup_adopts_exact_observed_run_without_overwriting_callback_authority(self):
        remote_id = 'observed-worker-run-' + uuid4().hex
        ContentFactoryRun.objects.create(run_id=remote_id, organization=self.org, domain=self.org.domain,
            workflow='website_baseline', status='running', current_step='crawl_owned_web',
            result={'callback_evidence': 'already-observed-progress'})
        with self.remote(lookup_status=200, lookup={'key_status':'dispatched','run_id':remote_id,'run_status':'queued'}) as (post, find):
            run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.run_id, remote_id)
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.current_step, 'crawl_owned_web')
        self.assertEqual(run.result, {'callback_evidence': 'already-observed-progress'})
        self.assertFalse(run.run_request.get('dispatch_pending_resolution', False))
        self.assertEqual(find.call_args.kwargs['params']['client_request_id'], post.call_args.kwargs['json']['client_request_id'])
        self.assertEqual(self.slots(), 1)


    def test_new_observed_child_waits_for_a_real_snapshot_before_projecting_lookup_status(self):
        remote_id = 'observed-without-callback-' + uuid4().hex
        with self.remote(lookup_status=200, lookup={'key_status':'dispatched','run_id':remote_id,'run_status':'failed'}):
            run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.run_id, remote_id)
        self.assertEqual(run.status, 'queued')
        self.assertEqual(run.result, {})
        self.assertFalse(run.run_request.get('dispatch_pending_resolution', False))
        self.assertEqual(self.slots(), 1)

    def test_immediate_absence_after_acceptance_waits_for_existing_grace_resolution(self):
        with self.remote(lookup_status=404, lookup={'detail':'No dispatch recorded for this client_request_id'}):
            run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.status, 'blocked')
        self.assertIs(run.run_request['dispatch_pending_resolution'], True)
        self.assertEqual(self.slots(), 1)

    def test_authenticated_failed_key_receipt_is_the_existing_confirmed_no_work_proof(self):
        with self.remote(lookup_status=200, lookup={'key_status':'failed','error':'worker_queue_unavailable'}):
            run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.result['error_code'], 'received_not_started')
        self.assertEqual(run.result['dispatch_outcome'], 'received_not_started')
        self.assertFalse(run.run_request.get('dispatch_pending_resolution', True))
        self.assertEqual(self.slots(), 0)

    def test_authenticated_rejection_retains_its_actual_backend_reason_and_releases_only_no_work(self):
        with self.remote(lookup_status=200, lookup={'key_status':'failed','error':'backend_rejected:website_connection_changed'}):
            run, error = self.manual()
        self.assertIsNone(error)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.result['error_code'], 'dispatch_rejected')
        self.assertEqual(run.result['dispatch_outcome'], 'rejected')
        self.assertEqual(run.result['backend_code'], 'website_connection_changed')
        self.assertFalse(run.run_request.get('dispatch_pending_resolution', True))
        self.assertEqual(self.slots(), 0)

    def test_mismatching_declared_receipt_identity_never_adopts_a_foreign_child(self):
        with override_settings(ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP=0):
            for field, value in [('client_request_id','foreign-key'),('domain','foreign.synthetic.test'),
                    ('workflow','article_generation'),('endpoint','article')]:
                with self.subTest(field=field), self.remote(lookup_status=200,
                        lookup={'key_status':'dispatched','run_id':'foreign-worker-run',field:value}):
                    run, error = self.manual()
                self.assertIsNone(error)
                self.assertNotEqual(run.run_id, 'foreign-worker-run')
                self.assertIs(run.run_request['dispatch_pending_resolution'], True)

    def test_invalid_accepted_body_remains_uncertain_instead_of_losing_the_local_receipt(self):
        for accepted in ('invalid-json', ['unexpected-list']):
            with self.subTest(body=accepted), self.remote(accepted=accepted):
                run, error = self.manual()
            self.assertIsNone(error)
            self.assertEqual(run.status, 'blocked')
            self.assertIs(run.run_request['dispatch_pending_resolution'], True)
        self.assertEqual(self.slots(), 2)
