"""Real manual/auto admission shares one original rolling free allowance."""
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import Throttled

from founder_tools.services import FounderCompanyContext
from workflow_runs.models import ContentFactoryRun
from . import billing, founder_website_checks as checks, vibe_marketing_views as views
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org

FLAGS = {**ALL_FLAGS_ON, 'MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED': True,
         'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED': True, 'ARTICLES_FREE_SETUP_RESEARCH_ENABLED': True,
         'ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP': 1, 'COMMUNITY_CHAT_SIGNUP_ENABLED': False}


@override_settings(**FLAGS)
class FreeBaselineReservationsTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.owner, self.org, self.config = self.fixture.owner, self.fixture.organization, self.fixture.config
        self.context = FounderCompanyContext(self.fixture.company.profile, self.fixture.company, self.org)
        ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow='confirmed_topic', status='awaiting_approval')
        self.enterContext(patch.object(views, '_roo_points_balance_for_user', return_value=0))
        self.enterContext(patch.object(views, 'get_content_factory_ai_agent_required_points', return_value=6))

    def manual(self):
        return views._start_website_baseline(context=self.context, config=self.config, user=self.owner)

    def queued(self, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        payload=kwargs['payload']
        return ContentFactoryRun.objects.create(run_id=payload['client_request_id'], organization=self.org,
            domain=self.org.domain, workflow='website_baseline', status='queued', run_request=payload)

    def test_reservation_replay_is_one_use_and_release_removes_only_its_original_slot(self):
        first=billing.reserve_free_setup_use(self.owner,'website_baseline','first')
        self.assertEqual(billing.reserve_free_setup_use(self.owner,'website_baseline','first'),first)
        with self.assertRaises(Throttled):billing.reserve_free_setup_use(self.owner,'website_baseline','second')
        self.assertEqual(len(billing._free_setup_uses(self.owner,'website_baseline',None)[3]),1)
        billing.release_free_setup_use(self.owner,'website_baseline',first)
        self.assertIsNotNone(billing.reserve_free_setup_use(self.owner,'website_baseline','second'))

    @override_settings(ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP=3)
    def test_new_reservations_count_legacy_uses_without_copying_them_twice(self):
        billing.record_free_setup_use(self.owner,'website_baseline')
        billing.reserve_free_setup_use(self.owner,'website_baseline','a')
        billing.reserve_free_setup_use(self.owner,'website_baseline','b')
        with self.assertRaises(Throttled):billing.check_free_setup_daily_cap(self.owner,'website_baseline')
        self.assertEqual(len(billing._free_setup_uses(self.owner,'website_baseline',None)[3]),3)
        self.assertEqual(len(billing._free_setup_uses(self.owner,'website_baseline',None,include_reservations=False)[3]),1)

    def test_confirmed_failed_manual_start_releases_but_unknown_dispatch_retains_slot(self):
        def blocked(**kwargs):
            run=self.queued(**kwargs);run.status='blocked';run.save(update_fields=['status']);return run
        with patch.object(views,'_queue_content_factory_run',side_effect=blocked):self.manual()
        self.assertEqual(len(billing._free_setup_uses(self.owner,'website_baseline',None)[3]),0)
        def unknown(**kwargs):
            run=blocked(**kwargs);run.run_request={**run.run_request,'dispatch_pending_resolution':True}
            run.save(update_fields=['run_request']);return run
        with patch.object(views,'_queue_content_factory_run',side_effect=unknown):self.manual()
        with self.assertRaises(Throttled):billing.check_free_setup_daily_cap(self.owner,'website_baseline')

    def test_funded_manual_path_keeps_legacy_uncapped_admission(self):
        billing.reserve_free_setup_use(self.owner,'website_baseline','full')
        with patch.object(views,'_roo_points_balance_for_user',return_value=100), \
                patch.object(views,'_queue_content_factory_run',side_effect=self.queued), \
                patch('roo.services.PointsService.spend') as spend:
            run,error=self.manual()
        self.assertIsNone(error);self.assertEqual(run.status,'queued');spend.assert_not_called()

    @skipUnless(connection.vendor=='postgresql','Requires real PostgreSQL row locks')
    def test_inflight_manual_start_excludes_auto_before_its_remote_ack(self):
        entered,release=Event(),Event()
        calls=[]
        def queue(**kwargs):
            calls.append(kwargs['payload']['client_request_id']);entered.set()
            self.assertTrue(release.wait(10));return self.queued(**kwargs)
        def manual():
            close_old_connections()
            try:return self.manual()
            finally:connections.close_all()
        with patch.object(views,'_queue_content_factory_run',side_effect=queue):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(manual)
                try:
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(checks.run_founder_website_check_sweep()['limited'],1)
                finally:release.set()
                self.assertIsNone(future.result()[1])
        self.assertEqual(len(calls),1)

    @skipUnless(connection.vendor=='postgresql','Requires real PostgreSQL row locks')
    def test_inflight_auto_start_excludes_manual_before_its_remote_ack(self):
        entered,release=Event(),Event()
        calls=[]
        def queue(**kwargs):
            calls.append(kwargs['payload']['client_request_id']);entered.set()
            self.assertTrue(release.wait(10));return self.queued(**kwargs)
        def automatic():
            close_old_connections()
            try:return checks.run_founder_website_check_sweep()
            finally:connections.close_all()
        with patch.object(views,'_queue_content_factory_run',side_effect=queue):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(automatic)
                try:
                    self.assertTrue(entered.wait(10))
                    with self.assertRaises(Throttled):self.manual()
                finally:release.set()
                self.assertEqual(future.result()['queued'],1)
        self.assertEqual(len(calls),1)
