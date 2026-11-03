"""Current team consent fences owned hosted publication callbacks and replay."""
from django.db import connections
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from threading import Event
from unittest import skipUnless
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction, close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from founder_tools import startup_team as teams
from roo.models import Ledger, PointsAccount
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import dispatch_outbox, hosted_articles as hosted, no_delivery_refunds as refunds
from .billing_lineage import original_article_spend_for_run
from .hosted_article_models import HostedArticlePublication, HostedArticleSite
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .tests_hosted_articles import article, DIGEST


@override_settings(**{**ALL_FLAGS_ON, 'ARTICLES_HOSTED_SITES_ENABLED': True,
    'MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED': False, 'MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED': False})
class HostedPublishAuthorityTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org, self.owner = self.fixture.organization, self.fixture.owner
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=['email_verified_at'])
        self.member = get_user_model().objects.create_user(email=f'{uuid4().hex}@member.synthetic.test')
        self.member.email_verified_at = timezone.now()
        self.member.save(update_fields=['email_verified_at'])
        notice = patch.object(teams, '_notify')
        notice.start(); self.addCleanup(notice.stop)
        token = teams.issue_join_reference(user=self.member, organization=self.org)
        teams.request_join(user=self.member, join_reference=token)
        roster = self.roster()
        pending = next(row for row in roster['requests'] if row['user_id'] == self.member.pk)
        teams.decide_request(organization=self.org, user=self.owner, request_id=pending['request_id'], decision='approve')
        self.member_id = self.roster()['members'][0]['memberId']
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, rights={'publish': True})
        self.site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state='active')
        self.source = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow='confirmed_topic', status='completed', result={'draft_ready_v1': {'version': 1}})
        self.child = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow='direct_generate', status='queued', run_request={'hosted_site_id': str(self.site.pk),
                'domain': self.org.domain, 'source_run_id': self.source.run_id, 'source_revision': 'original-review-1',
                'approved_body': 'The exact retained edit.', 'approved_body_sha256': 'a'*64,
                'client_request_id': 'child-'+uuid4().hex},
            result={'article_approval_v1': {'version': 1, 'source': 'publish_reviewed',
                'approved_by_user_id': self.member.pk, 'approved_body_sha256': 'a'*64},
                'publish_reviewed_v1': {'source_run_id': self.source.run_id, 'approved_body_sha256': 'a'*64}})

    def roster(self):
        self.fixture.config.refresh_from_db()
        return self.fixture.config.pillar_strategy['startup_team']

    def approve(self, user=None):
        return hosted.request_publication(site=self.site, run=self.child, user=user or self.member, allow_continuation=True)

    def progress(self, **data):
        return hosted.record_progress(domain=self.site.domain, run_id=self.child.run_id, data=data)

    def rendering(self):
        return self.progress(status='rendering', article=article(), revision_digest=DIGEST)

    def snapshot(self):
        self.site.refresh_from_db(); self.child.refresh_from_db(); self.source.refresh_from_db()
        publication = HostedArticlePublication.objects.filter(site=self.site, run_id=self.child.run_id).values().first()
        return deepcopy((publication, HostedArticleSite.objects.filter(pk=self.site.pk).values().get(),
                         ContentFactoryRun.objects.filter(pk=self.child.pk).values().get(),
                         ContentFactoryRun.objects.filter(pk=self.source.pk).values().get(), list(Ledger.objects.values())))

    def assert_refused_unchanged(self, callback):
        before = self.snapshot()
        with self.assertRaises(hosted.HostedPublishError) as denied:
            callback()
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(self.snapshot(), before)

    def test_member_removal_after_approval_prevents_render_and_keeps_pending_child(self):
        self.approve()
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, remove=True)
        self.assertFalse(teams.member_may_publish(self.member, self.org))
        self.assert_refused_unchanged(self.rendering)

    def test_publish_grant_loss_after_render_prevents_late_deployment_and_live_callback(self):
        self.approve(); self.rendering()
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, rights={'publish': False})
        self.assert_refused_unchanged(lambda: self.progress(status='deployed', deployment={'url': 'https://mine.owned.workers.dev'}))
        self.assert_refused_unchanged(lambda: self.progress(status='live', verified_digest=DIGEST))

    def test_inactive_owner_and_frozen_owner_cannot_advance_their_approved_child(self):
        self.child.result['article_approval_v1']['approved_by_user_id'] = self.owner.pk
        self.child.save(update_fields=['result'])
        self.approve(self.owner)
        self.owner.is_active = False; self.owner.save(update_fields=['is_active'])
        self.assert_refused_unchanged(self.rendering)
        self.owner.is_active = True; self.owner.save(update_fields=['is_active'])
        teams.freeze_for_offboard(self.org, self.owner)
        self.assertFalse(teams.member_may_publish(self.owner, self.org))
        self.assert_refused_unchanged(self.rendering)

    def test_stale_user_and_site_instances_do_not_bypass_new_admission(self):
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=False)
        self.assert_refused_unchanged(self.approve)
        get_user_model().objects.filter(pk=self.member.pk).update(is_active=True)
        HostedArticleSite.objects.filter(pk=self.site.pk).update(state='paused')
        with self.assertRaises(hosted.HostedPublishError): self.approve()
        self.assertFalse(HostedArticlePublication.objects.exists())

    def test_another_current_publisher_cannot_adopt_the_original_approvers_child(self):
        self.approve()
        self.assert_refused_unchanged(lambda: self.approve(self.owner))

    def test_cancelled_child_cannot_be_revived_by_late_hosted_callbacks(self):
        self.approve(); self.rendering()
        ContentFactoryRun.objects.filter(pk=self.child.pk).update(status='cancelled')
        with self.assertRaises(hosted.HostedPublishError):
            self.progress(status='live', verified_digest=DIGEST)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'cancelled')

    def test_same_org_site_replacement_or_foreign_run_cannot_borrow_publication(self):
        self.approve()
        self.child.run_request = {**self.child.run_request, 'hosted_site_id': str(uuid4())}
        self.child.save(update_fields=['run_request'])
        with self.assertRaises(hosted.HostedPublishError): self.rendering()
        self.assertEqual(HostedArticlePublication.objects.get().status, 'pending')

    def test_repeated_owned_callback_keeps_body_revision_site_and_original_paid_lineage(self):
        from integrations.services.article_generation import _ledger_reference_id
        key = uuid4().hex
        PointsAccount.objects.update_or_create(user=self.owner, defaults={'balance': 24, 'earned_balance': 24})
        spend, _ = PointsService.spend(user=self.owner, delta=12, source='CONTENT_FACTORY', description='Original article',
            created_by_slack_id='fixture', idempotency_key='content_factory:charge:'+key,
            reference_type='CONTENT_FACTORY', reference_id=_ledger_reference_id(key))
        request = {'client_request_id': key, 'domain': self.org.domain, 'roo_points_billing_status': 'charged',
                   'roo_points_cost': 12, 'roo_points_ledger_id': spend.pk, 'roo_points_billing_user_id': self.owner.pk}
        with transaction.atomic(): dispatch_outbox.reserve_dispatch(organization=self.org, payload=request, billing_user_id=self.owner.pk)
        ContentFactoryRun.objects.filter(pk=self.source.pk).update(run_request=request)
        self.source.refresh_from_db()
        refunds.carry_charge(source_run=self.source, new_run=self.child)
        self.child.refresh_from_db()
        original_request = deepcopy(self.child.run_request)
        original_points = deepcopy(self.child.result['points_v1'])
        ledger_before = list(Ledger.objects.values())
        first, repeated = self.approve(), self.approve()
        self.assertEqual(first.pk, repeated.pk)
        self.rendering(); self.rendering()
        first_live = self.progress(status='live', verified_digest=DIGEST)
        replayed = self.progress(status='live', verified_digest=DIGEST)
        self.assertEqual(first_live.pk, replayed.pk)
        self.assertEqual(replayed.article['body_html'], article()['body_html'])
        self.assertEqual(replayed.revision_digest, DIGEST)
        self.child.refresh_from_db()
        self.assertEqual(self.child.run_request, original_request)
        self.assertEqual(self.child.result['points_v1'], original_points)
        self.assertEqual(original_article_spend_for_run(self.child).pk, spend.pk)
        self.assertEqual(list(Ledger.objects.values()), ledger_before)
        self.assertEqual(HostedArticlePublication.objects.count(), 1)

    def test_lost_render_callback_replay_cannot_change_the_approved_rendered_revision(self):
        self.approve(); self.rendering()
        before = self.snapshot()
        with self.assertRaises(hosted.HostedPublishError):
            self.progress(status='rendering', article=article(body_html='<p>Other text</p>'), revision_digest='e'*64)
        self.assertEqual(self.snapshot(), before)


    def test_revoked_rendering_replay_refuses_but_owned_terminal_live_replay_acks_without_writes(self):
        self.approve(); self.rendering()
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, rights={'publish': False})
        self.assert_refused_unchanged(self.rendering)
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, rights={'publish': True})
        self.progress(status='live', verified_digest=DIGEST)
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, remove=True)
        before = self.snapshot()
        replay = self.progress(status='live', verified_digest=DIGEST)
        self.assertEqual(replay.status, 'live')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(hosted.site_render_state(self.site)['publications'][0]['revisionDigest'], DIGEST)
        self.assert_refused_unchanged(lambda: self.progress(status='live', verified_digest='e'*64))
        self.assert_refused_unchanged(lambda: self.progress(status='live', verified_digest=DIGEST,
            article=article(body_html='<p>Other text</p>'), revision_digest=DIGEST))

    def test_already_live_replay_does_not_revive_subsequently_cancelled_child_or_paused_site(self):
        self.approve(); self.rendering(); self.progress(status='live', verified_digest=DIGEST)
        ContentFactoryRun.objects.filter(pk=self.child.pk).update(status='cancelled')
        HostedArticleSite.objects.filter(pk=self.site.pk).update(state='paused')
        before = self.snapshot()
        self.progress(status='live', verified_digest=DIGEST)
        self.assertEqual(self.snapshot(), before)
        self.child.refresh_from_db(); self.site.refresh_from_db()
        self.assertEqual(self.child.status, 'cancelled')
        self.assertEqual(self.site.state, 'paused')

    def test_frozen_active_owner_cannot_activate_a_previously_approved_proxy(self):
        from . import hosted_auto_register as auto
        run_id = 'proxy-'+uuid4().hex
        self.site.state = 'draft'
        self.site.worker_url = 'https://mine.owned.workers.dev'
        self.site.profile = {'auto': {'proxy_run_id': run_id}}
        self.site.save(update_fields=['state', 'worker_url', 'profile'])
        ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain, run_id=run_id,
            workflow='article_system_setup', result={
                'merge_v1': {'merge_sha': 'e'*40, 'head_sha': 'c'*40, 'approved_by_user_id': self.owner.pk},
                'owner_merge_approval_v1': {'kind': 'proxy', 'head_sha': 'c'*40,
                    'approved_at': timezone.now().isoformat(), 'approved_by_user_id': self.owner.pk}})
        teams.freeze_for_offboard(self.org, self.owner)
        before = self.snapshot()
        with patch.object(auto, 'fetch_live_route', side_effect=[(b'same', {'x-mlai-hosted': '1'}), (b'same', {})]):
            self.assertFalse(auto.activate(self.site))
        self.assertEqual(self.snapshot(), before)


    @skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locking')
    def test_member_removal_serializes_with_current_publication_admission(self):
        self.approve()
        checked, release, removal_started = Event(), Event(), Event()
        current_rights = hosted.require_current_publish_rights

        def admitted(*args):
            user = current_rights(*args)
            checked.set()
            if not release.wait(5):
                raise AssertionError('Admission test did not release its organization lock')
            return user

        def publish():
            close_old_connections()
            try: return self.rendering().status
            finally: connections.close_all()

        def remove():
            close_old_connections()
            try:
                removal_started.set()
                return teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, remove=True)
            finally: connections.close_all()

        with patch.object(hosted, 'require_current_publish_rights', side_effect=admitted), ThreadPoolExecutor(max_workers=2) as pool:
            publisher = pool.submit(publish)
            try:
                self.assertTrue(checked.wait(5))
                removal = pool.submit(remove)
                self.assertTrue(removal_started.wait(5))
                with self.assertRaises(TimeoutError): removal.result(timeout=0.1)
            finally:
                release.set()
            self.assertEqual(publisher.result(timeout=5), 'rendering')
            self.assertEqual(removal.result(timeout=5)['status'], 'removed')
        self.assert_refused_unchanged(lambda: self.progress(status='live', verified_digest=DIGEST))


    def test_finished_build_admission_is_exact_read_only_and_cannot_demote_or_reopen(self):
        self.approve(); self.rendering()
        before = self.snapshot()
        self.progress(status='rendering', revision_digest=DIGEST)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(hosted.HostedPublishError):
            self.progress(status='rendering', revision_digest='e'*64)
        self.assertEqual(self.snapshot(), before)
        self.progress(status='live', verified_digest=DIGEST)
        before = self.snapshot()
        with self.assertRaises(hosted.HostedPublishError):
            self.progress(status='rendering', revision_digest=DIGEST)
        self.assertEqual(self.snapshot(), before)
        ContentFactoryRun.objects.filter(pk=self.child.pk).update(status='cancelled')
        before = self.snapshot()
        with self.assertRaises(hosted.HostedPublishError):
            self.progress(status='rendering', revision_digest=DIGEST)
        self.assertEqual(self.snapshot(), before)

    def test_revocation_after_render_refuses_finished_build_upload_admission(self):
        self.approve(); self.rendering()
        teams.update_member(organization=self.org, user=self.owner, member_id=self.member_id, remove=True)
        self.assert_refused_unchanged(lambda: self.progress(status='rendering', revision_digest=DIGEST))
