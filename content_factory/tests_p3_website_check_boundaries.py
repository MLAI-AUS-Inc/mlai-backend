"""Actual dispatch/runner composition for optional website-check review boundaries.

Only HTTP is mocked. Admission, local BLOCKED receipts, exact-key replay,
current nonce/cohort checks and the scheduler's status contract remain real.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from requests.exceptions import ReadTimeout

from core.scheduling import run_runners
from roo.models import ScheduledDiscoveryHeartbeat
from workflow_runs.models import ContentFactoryRun
from . import founder_website_checks as checks, vibe_marketing_views as views
from .models import OrganizationContentConfig
from .p2_foundation import p2_applies
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org

FLAGS = {**ALL_FLAGS_ON, 'MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED': True,
    'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED': True, 'ARTICLES_FREE_SETUP_RESEARCH_ENABLED': True,
    'ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP': 2, 'COMMUNITY_CHAT_SIGNUP_ENABLED': False}
REMOTE = {'enabled': True, 'base_url': 'https://website-check.synthetic.test',
          'api_key_configured': True, 'is_local_env': False}


@override_settings(**FLAGS)
class WebsiteCheckBoundaryTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        built = make_p2_org()
        self.org, self.owner, self.config = built.organization, built.owner, built.config
        self.now = timezone.now()
        ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=self.org,
            domain=self.org.domain, workflow='confirmed_topic', status='awaiting_approval')

    def receipt(self):
        config = OrganizationContentConfig.objects.get(pk=self.config.pk)
        return deepcopy(config.pillar_strategy['self_heal'][checks.KEY])

    def uncertain_http(self, stack):
        def lost(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block, 'HTTP publication under a database transaction')
            raise ReadTimeout('synthetic lost acknowledgement')
        def unknown(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block, 'HTTP lookup under a database transaction')
            response = Mock(status_code=503, content=b'lookup unavailable')
            return response
        stack.enter_context(patch.object(views, '_content_factory_remote_config', return_value=REMOTE))
        stack.enter_context(patch.object(views, '_content_factory_headers', return_value={'X-API-Key': 'synthetic'}))
        post = stack.enter_context(patch.object(views.http_client, 'post', side_effect=lost))
        lookup = stack.enter_context(patch.object(views.http_client, 'get', side_effect=unknown))
        return post, lookup

    def test_real_blocked_unknown_dispatch_keeps_pending_key_into_next_local_date(self):
        original_clock = self.config.updated_at
        observed = []
        actual_start = views._start_website_baseline
        def start(**kwargs):
            answer = actual_start(**kwargs)
            observed.append(answer)
            return answer
        with ExitStack() as stack:
            post, lookup = self.uncertain_http(stack)
            stack.enter_context(patch.object(views, '_start_website_baseline', side_effect=start))
            first = checks.run_founder_website_check_sweep(now=self.now)
            self.assertEqual(first['waiting'], 1)
            self.assertEqual(first['failed'], 0)
            run, error = observed[0]
            self.assertIsNone(error)
            self.assertEqual(run.status, 'blocked')
            self.assertIs(run.run_request['dispatch_pending_resolution'], True)
            saved = self.receipt()
            self.assertEqual(saved['state'], 'pending')
            self.assertEqual(saved['reason'], 'dispatch_pending_resolution')
            self.assertEqual(saved['key'], run.run_request['client_request_id'])
            self.assertTrue(saved['usage_reserved'])
            first_posts = post.call_count
            self.assertEqual(first_posts, views.CONTENT_FACTORY_DISPATCH_MAX_POST_ATTEMPTS)
            self.org.seed_keywords = ['changed after the original dispatch']
            self.org.save(update_fields=['seed_keywords'])
            again = checks.run_founder_website_check_sweep(now=self.now + timedelta(days=2))
            self.assertEqual(again['waiting'], 1)
            self.assertEqual(again['queued'], 0)
            replay = self.receipt()
            self.assertEqual(replay['state'], 'pending')
            self.assertEqual(replay['key'], saved['key'])
            self.assertEqual(replay['date'], saved['date'])
            self.assertEqual(replay['payload'], saved['payload'])
            self.assertEqual(post.call_count, first_posts * 2)
            self.assertEqual(lookup.call_count, post.call_count)
            payloads = [call.kwargs['json'] for call in post.call_args_list]
            self.assertTrue(all(payload == payloads[0] for payload in payloads))
            self.assertTrue(all(call.kwargs['params']['client_request_id'] == saved['key'] for call in lookup.call_args_list))
        self.assertEqual(ContentFactoryRun.objects.filter(organization=self.org, workflow='website_baseline').count(), 1)
        self.config.refresh_from_db()
        self.assertEqual(self.config.updated_at, original_clock)

    def test_nonce_stolen_after_quota_reservation_stops_before_any_http(self):
        state, claim = checks._claim(self.config.pk, self.now)
        self.assertEqual(state, 'claimed')
        actual_context = checks._context
        def current_owner_then_stolen_nonce(org):
            context = actual_context(org)
            config = OrganizationContentConfig.objects.get(pk=self.config.pk)
            current = deepcopy(checks._receipt(config))
            self.assertTrue(current['usage_reserved'])
            current['nonce'] = 'new-worker-owns-this-claim'
            checks._save(config, current)
            return context
        with patch.object(checks, '_context', side_effect=current_owner_then_stolen_nonce), \
                patch.object(views.http_client, 'post') as post, patch.object(views.http_client, 'get') as get:
            self.assertEqual(checks._dispatch(claim, self.now), 'waiting')
            post.assert_not_called(); get.assert_not_called()
        self.assertEqual(self.receipt()['nonce'], 'new-worker-owns-this-claim')
        self.assertEqual(ContentFactoryRun.objects.filter(organization=self.org, workflow='website_baseline').count(), 0)

    def test_per_organization_kill_overrides_legacy_opt_in_at_claim_and_dispatch(self):
        with override_settings(ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS=str(self.org.pk)):
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=str(self.org.pk)):
                self.assertTrue(p2_applies(self.org), 'The explicit opt-in remains P2; the kill must be checked separately')
                self.assertEqual(checks._claim(self.config.pk, self.now), ('skipped', None))
            state, claim = checks._claim(self.config.pk, self.now)
            self.assertEqual(state, 'claimed')
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=self.org.domain), \
                    patch.object(views.http_client, 'post') as post, patch.object(views.http_client, 'get') as get:
                self.assertEqual(checks._dispatch(claim, self.now), 'skipped')
                post.assert_not_called(); get.assert_not_called()
            self.assertEqual(self.receipt()['state'], 'cancelled')

    @override_settings(MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED=False)
    def test_registered_command_runner_is_healthy_and_skipped_when_off(self):
        from core.management.commands import run_scheduled_discovery as command
        selected = []
        def only_website_runner(runners):
            registered = dict(runners)
            self.assertIs(registered['founder_website_checks'], checks.scheduled_founder_website_checks)
            selected.append('founder_website_checks')
            with self.assertNumQueries(0):
                return run_runners((('founder_website_checks', registered['founder_website_checks']),))
        output = StringIO()
        with patch.object(command, 'run_runners', side_effect=only_website_runner), \
                patch.object(views.http_client, 'post') as post, patch.object(views.http_client, 'get') as get:
            call_command('run_scheduled_discovery', stdout=output)
            post.assert_not_called(); get.assert_not_called()
        self.assertEqual(selected, ['founder_website_checks'])
        self.assertEqual(json.loads(output.getvalue())['founder_website_checks']['status'], 'skipped')
        heartbeat = ScheduledDiscoveryHeartbeat.objects.get(name='scheduled_discovery')
        self.assertEqual(heartbeat.last_error, '')
        self.assertIsNotNone(heartbeat.last_succeeded_at)
        self.assertIsNone(heartbeat.last_failed_at)
