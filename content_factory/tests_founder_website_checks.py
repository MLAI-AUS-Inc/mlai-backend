"""Free scheduling, exact-key replay, real owner fences and unchanged manual admission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from unittest import skipUnless
from uuid import uuid4

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from . import founder_website_checks as checks, vibe_marketing_views as views
from .billing import record_free_setup_use
from .models import OrganizationContentConfig, WebsiteBaselineSnapshot
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from workflow_runs.models import ContentFactoryRun

FLAGS = {**ALL_FLAGS_ON, 'MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED': True,
         'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED': True, 'ARTICLES_FREE_SETUP_RESEARCH_ENABLED': True,
         'ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP': 2, 'COMMUNITY_CHAT_SIGNUP_ENABLED': False}


@override_settings(**FLAGS)
class WebsiteCheckTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.built = make_p2_org()
        self.org, self.owner, self.config = self.built.organization, self.built.owner, self.built.config
        self.now = timezone.now()
        self.article = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow='confirmed_topic', status='running')
        self.queued = []
        self.queue_patch = patch.object(views, '_queue_content_factory_run', side_effect=self.queue)
        self.queue_patch.start()
        self.addCleanup(self.queue_patch.stop)

    def queue(self, **kwargs):
        self.assertFalse(__import__('django.db', fromlist=['connection']).connection.in_atomic_block)
        payload = kwargs['payload']
        self.queued.append(payload.copy())
        return ContentFactoryRun.objects.get_or_create(run_id=payload['client_request_id'], defaults={
            'organization':self.org, 'domain':self.org.domain, 'workflow':'website_baseline',
            'status':'queued', 'run_request':payload})[0]

    def sweep(self, **kwargs):
        return checks.run_founder_website_check_sweep(now=kwargs.pop('now', self.now), **kwargs)

    def deliver(self):
        self.article.status = 'awaiting_approval'
        self.article.save(update_fields=['status'])

    def receipt(self):
        config = OrganizationContentConfig.objects.get(pk=self.config.pk)
        return config.pillar_strategy['self_heal'][checks.KEY]

    def test_first_draft_queues_one_free_check_without_points_lookup_and_keeps_clocks(self):
        self.deliver()
        clock = self.config.updated_at
        with patch('roo.services.PointsService.spend') as spend, patch.object(views, '_roo_points_balance_for_user') as balance:
            self.assertEqual(self.sweep()['queued'], 1)
            self.assertEqual(self.sweep()['waiting'], 1)
        spend.assert_not_called(); balance.assert_not_called()
        self.config.refresh_from_db()
        self.assertEqual(self.config.updated_at, clock)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0]['client_request_id'], self.receipt()['key'])
        self.assertEqual(self.receipt()['state'], 'queued')
        self.assertTrue(self.receipt()['usage_reserved'])

    def test_pre_draft_cancelled_consumed_and_disabled_free_flags_never_dispatch(self):
        from .first_article_entitlement import _write
        ContentFactoryRun.objects.filter(organization=self.org).update(status='cancelled')
        _write(organization_id=self.org.pk, suffix='consumed', user=self.owner, reference_id=self.article.run_id)
        self.assertEqual(self.sweep()['queued'], 0)
        with override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False):
            with self.assertNumQueries(0):
                self.assertEqual(self.sweep()['visited'], 0)
        self.assertEqual(self.queued, [])

    def test_existing_manual_free_cap_prevents_new_background_work(self):
        self.deliver()
        for _ in range(2): record_free_setup_use(self.owner, 'website_baseline', now=self.now.timestamp())
        self.assertEqual(self.sweep()['limited'], 1)
        self.assertEqual(self.receipt()['state'], 'limited')
        self.assertEqual(self.queued, [])

    def test_lost_ack_replays_exact_envelope_and_does_not_reserve_another_free_use(self):
        self.deliver()
        real = self.queue
        def lost(**kwargs):
            # Simulate publication accepted before the worker can return a run.
            self.queued.append(kwargs['payload'].copy())
            raise TimeoutError('lost acknowledgement')
        with patch.object(views, '_queue_content_factory_run', side_effect=lost):
            self.assertEqual(self.sweep()['failed'], 1)
        first = self.receipt()
        self.assertTrue(first['usage_reserved'])
        self.assertEqual(self.sweep()['waiting'], 1)
        self.org.seed_keywords = ['changed after original admission']; self.org.save(update_fields=['seed_keywords'])
        with patch('content_factory.founder_website_checks.record_free_setup_use') as usage:
            self.assertEqual(self.sweep(now=self.now+timedelta(minutes=3))['queued'], 1)
            usage.assert_not_called()
        self.assertEqual(self.queued[0], self.queued[1])
        self.assertEqual(first['key'], self.receipt()['key'])

    def test_nightly_respects_local_date_and_never_repeats_a_completed_day(self):
        self.deliver()
        self.config.default_timezone='Australia/Melbourne'; self.config.save(update_fields=['default_timezone'])
        WebsiteBaselineSnapshot.objects.create(organization=self.org, status='completed', collected_at=self.now)
        self.assertEqual(self.sweep()['queued'], 0)
        tomorrow = self.now+timedelta(days=1)
        self.assertEqual(self.sweep(now=tomorrow)['queued'], 1)
        ContentFactoryRun.objects.filter(workflow='website_baseline').update(status='completed')
        self.assertEqual(self.sweep(now=tomorrow+timedelta(minutes=5))['queued'], 0)
        self.assertEqual(len(self.queued),1)

    def test_live_owner_freeze_or_domain_change_refuses_pending_retry(self):
        self.deliver()
        with patch.object(views, '_queue_content_factory_run', side_effect=TimeoutError): self.sweep()
        self.org.domain='changed.synthetic.test'; self.org.save(update_fields=['domain'])
        self.assertEqual(self.sweep(now=self.now+timedelta(minutes=3))['queued'],0)
        self.assertEqual(self.queued,[])

    def test_removed_owner_cannot_resume_a_pending_background_check(self):
        self.deliver()
        with patch.object(views, '_queue_content_factory_run', side_effect=TimeoutError): self.sweep()
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        self.assertEqual(self.sweep(now=self.now+timedelta(minutes=3))['queued'], 0)
        self.assertEqual(self.queued, [])

    def test_uncertain_sends_hold_the_same_key_instead_of_starting_another_day(self):
        self.deliver()
        with patch.object(views, '_queue_content_factory_run', side_effect=TimeoutError) as queue:
            for minute in (0, 3, 6): self.sweep(now=self.now+timedelta(minutes=minute))
            self.assertEqual(self.sweep(now=self.now+timedelta(days=2))['held'], 1)
            self.assertEqual(queue.call_count, 3)
            keys = {call.kwargs['payload']['client_request_id'] for call in queue.call_args_list}
        self.assertEqual(len(keys), 1)
        self.assertEqual(self.receipt()['sends'], 3)

    @skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locks')
    def test_concurrent_claims_publish_one_task_and_keep_one_daily_key(self):
        self.deliver()
        def worker():
            close_old_connections()
            try:return self.sweep()
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:worker(),range(2)))
        self.assertEqual(len(self.queued),1)
        self.assertEqual(self.receipt()['sends'],1)


class WebsiteChecksOffTests(SimpleTestCase):
    @override_settings(MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED=False)
    def test_off_does_not_touch_database_or_providers(self):
        self.assertEqual(checks.run_founder_website_check_sweep()['visited'],0)
