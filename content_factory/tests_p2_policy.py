"""Recovery schedules, limits, consent fences and durable idempotency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
import json
import time
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from content_factory import failure_policy_engine as engine, channel_switch
from content_factory.self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_run, make_named_legacy_org
from content_factory.models import OrganizationContentConfig
from workflow_runs.models import ContentFactoryRun


@override_settings(**ALL_FLAGS_ON)
class PolicyTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org = self.fixture.organization
        self.now = timezone.now()

    def run_for(self, code='REVIEW_PROVIDER_UNAVAILABLE', **kwargs):
        result = {'error_code': code, **kwargs.pop('result', {})}
        return make_run(self.org, stamped=True, result=result, **kwargs)

    def seed(self, run, **values):
        receipt = {'version': 1, 'mode': 'policy', 'attempts': 0, 'first_failed_at': self.now.isoformat(),
                   'history': [], **values}
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, 'self_heal_v1': receipt})
        run.refresh_from_db()
        return receipt

    def test_retry_backoff_one_two_five_then_completion(self):
        run = self.run_for()
        original_clock = run.updated_at
        calls = []
        def resume(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            calls.append(kwargs)
            return {'status': 'queued'}
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', side_effect=resume):
            for attempt, minute in enumerate([1, 3, 8], 1):
                early = self.now if attempt == 1 else self.now + timedelta(minutes=[0, 1, 3][attempt - 1])
                run.refresh_from_db()
                decision = engine.decide(run, now=early)
                engine.apply(run, decision, now=early)
                self.assertEqual(len(calls), attempt - 1)
                run.refresh_from_db()
                due = self.now + timedelta(minutes=minute)
                decision = engine.decide(run, now=due)
                self.assertEqual(decision.at, due)
                engine.apply(run, decision, now=due)
                engine.apply(run, decision, now=due)
                self.assertEqual(len(calls), attempt)
                run.refresh_from_db()
                self.assertEqual(run.result['self_heal_v1']['attempts'], attempt)
                self.assertEqual(run.updated_at, original_clock)
        ContentFactoryRun.objects.filter(pk=run.pk).update(status='completed', result={**run.result, 'draft_ready_v1': {'body_sha256': 'a' * 64}})
        run.refresh_from_db()
        self.assertEqual(engine.decide(run, now=self.now + timedelta(minutes=9)).reason, 'delivered')

    def test_repair_and_repin_caps(self):
        run = self.run_for('EDITORIAL_REJECTED')
        self.seed(run, attempts=6)
        self.assertEqual(engine.decide(run, now=self.now).cap, 'repairable_max_attempts')
        run = self.run_for('SOURCE_CHANGED', result={'re_pin_v1': {'count': 2}})
        self.seed(run)
        self.assertEqual(engine.decide(run, now=self.now).action, 'switch_channel')

    def test_transient_and_wall_limits_are_distinct(self):
        run = self.run_for()
        self.seed(run, first_failed_at=(self.now - timedelta(hours=25)).isoformat())
        decision = engine.decide(run, now=self.now)
        self.assertEqual((decision.action, decision.cap), ('stop', 'transient_elapsed'))
        self.seed(run, first_failed_at=(self.now - timedelta(hours=49)).isoformat())
        self.assertEqual(engine.decide(run, now=self.now).cap, 'wall')

    def test_needs_owner_and_waiting_do_not_retry(self):
        for code, action in [('GITHUB_ACCESS_REQUIRED', 'await_owner'), ('HOSTED_PROXY_UNVERIFIED', 'observe')]:
            run = self.run_for(code)
            decision = engine.decide(run, now=self.now)
            self.assertEqual(decision.action, action)
            with patch.object(engine, '_execute_run') as remote:
                engine.apply(run, decision, now=self.now)
            remote.assert_not_called()

    def test_executor_off_observes_instead_of_claiming_an_attempt(self):
        run = self.run_for('SOURCE_CHANGED')
        with override_settings(ARTICLES_SOURCE_REPIN_ENABLED=False):
            self.assertEqual(engine.decide(run, now=self.now).reason, 'executor_off')
        run = self.run_for('EDITORIAL_REJECTED')
        self.seed(run, attempts=6, draft_ready=False)
        # Re-pin exhaustion always selects channel switching, so it exercises
        # the second executor independently of article delivery evidence.
        run = self.run_for('SOURCE_CHANGED', result={'re_pin_v1': {'count': 2}})
        with override_settings(ARTICLES_CHANNEL_SWITCH_ENABLED=False):
            decision = engine.decide(run, now=self.now)
            self.assertEqual((decision.action, decision.reason), ('observe', 'executor_off'))

    def test_strategy_source_and_day_caps(self):
        run = self.run_for('PUBLISHING_ADAPTER_REQUIRED', run_request={'expected_source_sha': 'a' * 40, 'strategy_pin': 'native_collection'})
        self.seed(run)
        config = self.fixture.config
        config.pillar_strategy = {**config.pillar_strategy, 'self_heal': {'version': 1,
            'day': self.now.date().isoformat(), 'strategy_attempts': 2, 'rung_attempts': {'native_collection:' + 'a' * 12: 2}}}
        config.save(update_fields=['pillar_strategy'])
        self.assertEqual(engine.decide(run, now=self.now).cap, 'strategy_rung_source')
        config.pillar_strategy['self_heal']['strategy_attempts'] = 6
        config.save(update_fields=['pillar_strategy'])
        self.assertEqual(engine.decide(run, now=self.now).cap, 'strategy_org_day')
        self.assertEqual(engine.decide(run, now=self.now + timedelta(days=1)).cap, None)

    def test_automatic_pr_fourth_refused_but_explicit_fourth_counted(self):
        for i in range(3):
            run = self.run_for(run_id=f'auto-{i}')
            self.assertTrue(engine.claim_pull_request_slot(self.org, key=f'{run.run_id}:1', now=self.now))
            self.assertTrue(engine.claim_pull_request_slot(self.org, key=f'{run.run_id}:1', now=self.now))
            self.assertEqual(engine.record_pull_request(self.org, pr_url=f'https://github.com/example/site/pull/{i}', run=run, now=self.now), i + 1)
        self.assertFalse(engine.claim_pull_request_slot(self.org, key='auto:4', now=self.now))
        self.assertEqual(engine.record_pull_request(self.org, pr_url='https://github.com/example/site/pull/founder-click', now=self.now), 4)
        self.assertEqual(engine.record_pull_request(self.org, pr_url='https://github.com/example/site/pull/founder-click', now=self.now), 4)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        alerts = list(config.pillar_strategy['ops_alerts'].values())
        self.assertEqual(len([row for row in alerts if row['alert_reason'] == 'pull_request_cap']), 1)
        self.assertTrue(engine.claim_pull_request_slot(self.org, key='tomorrow', now=self.now + timedelta(days=1)))

    @skipUnless(connection.vendor == 'postgresql', 'real row locks require PostgreSQL')
    def test_simultaneous_apply_dispatches_one_attempt(self):
        run = self.run_for()
        self.seed(run, first_failed_at=(self.now - timedelta(minutes=1)).isoformat())
        decision = engine.decide(run, now=self.now)
        calls = []
        def execute(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            calls.append(args[0].run_id)
        def worker():
            close_old_connections()
            try:
                current = ContentFactoryRun.objects.select_related('organization').get(pk=run.pk)
                return engine.apply(current, decision, now=self.now)
            finally:
                close_old_connections()
        with patch.object(engine, '_execute_run', side_effect=execute), ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(calls, [run.run_id])
        self.assertEqual([row['attempts'] for row in rows], [1, 1])

    def test_legacy_dry_run_never_changes_runs_or_calls_providers(self):
        legacy = make_named_legacy_org('pawshare')
        run = make_run(legacy.organization, result={'error_code': 'REVIEW_PROVIDER_UNAVAILABLE'})
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(engine, '_execute_run') as remote:
            decision = engine.decide(run, now=self.now)
            receipt = engine.apply(run, decision, now=self.now)
        self.assertEqual(receipt['mode'], 'observe')
        self.assertEqual(before, ContentFactoryRun.objects.values().get(pk=run.pk))
        remote.assert_not_called()
        out = StringIO()
        call_command('self_heal_report', '--json', stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report[0]['action'], 'retry')
        self.assertEqual(before, ContentFactoryRun.objects.values().get(pk=run.pk))

    def test_copy_ready_switch_is_once_and_preserves_original_authority_and_charge(self):
        run = self.run_for('PUBLISHING_ADAPTER_REQUIRED', run_request={'expected_source_sha': 'a' * 40, 'website_connection_id': 'original'})
        old_request, old_at = dict(run.run_request), run.updated_at
        acknowledgement = {'status': 'queued', 'channel_switch': {'version': 1, 'run_id': run.run_id,
            'key': f'{run.run_id}:copy-ready', 'state': 'applied', 'delivery_mode': 'content_only'}}
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', return_value=acknowledgement) as remote:
            first = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now)
            second = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now)
        self.assertEqual(first, second)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(remote.call_args.kwargs['payload']['channel_switch']['key'], f'{run.run_id}:copy-ready')
        run.refresh_from_db()
        self.assertEqual(run.run_request, old_request)
        self.assertEqual(run.updated_at, old_at)
        self.assertTrue(run.result['channel_v1']['waiting_to_publish'])

    def test_live_or_legacy_worker_without_exact_switch_ack_remains_pending(self):
        run = self.run_for('PUBLISHING_ADAPTER_REQUIRED')
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', side_effect=[
                {'status': 'pending', 'reason': 'article_worker_active'}, {'status': 'queued'}]) as remote:
            for attempt in range(2):
                receipt = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now + timedelta(minutes=3*attempt))
                self.assertEqual(receipt['dispatch_state'], 'pending')
        self.assertEqual(remote.call_count, 2)

    def test_channel_switch_and_start_are_cohort_gated(self):
        for flag in [False, True]:
            with override_settings(ARTICLES_CHANNEL_SWITCH_ENABLED=flag):
                self.assertEqual(channel_switch.article_start_delivery_mode(organization=self.org,
                    request={'code': 'ARTICLE_SYSTEM_SETUP_REQUIRED'}), 'content_only' if flag else None)
        legacy = make_named_legacy_org('pawshare')
        self.assertIsNone(channel_switch.article_start_delivery_mode(organization=legacy.organization, request={'code': 'ARTICLE_SYSTEM_SETUP_REQUIRED'}))

    def test_ladder_alert_once_per_source(self):
        from types import SimpleNamespace
        connection = SimpleNamespace(verified_sha='a' * 40)
        first = channel_switch.ladder_exhausted(organization=self.org, connection=connection, fingerprint={'framework': 'nextjs'})
        second = channel_switch.ladder_exhausted(organization=self.org, connection=connection)
        self.assertEqual(first, second)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertEqual(len(config.pillar_strategy['ops_alerts']), 1)

    def test_uncertain_resume_reuses_reserved_attempt_after_short_lease(self):
        run = self.run_for()
        self.seed(run, first_failed_at=(self.now - timedelta(minutes=1)).isoformat())
        replies = [{'status': 'action_pending', 'content_factory_transport_error': True}, {'status': 'queued'}]
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', side_effect=replies) as remote:
            engine.apply(run, engine.decide(run, now=self.now), now=self.now)
            run.refresh_from_db()
            engine.apply(run, engine.decide(run, now=self.now + timedelta(minutes=1)), now=self.now + timedelta(minutes=1))
            self.assertEqual(remote.call_count, 1)
            run.refresh_from_db()
            later = self.now + timedelta(minutes=2)
            engine.apply(run, engine.decide(run, now=later), now=later)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual([call.kwargs['payload']['self_heal_key'] for call in remote.call_args_list], [f'{run.run_id}:1'] * 2)
        run.refresh_from_db()
        self.assertEqual(run.result['self_heal_v1']['attempts'], 1)
        self.assertEqual(len(run.result['self_heal_v1']['history']), 1)

    def test_observe_batch_of_500_organizations_has_constant_queries_and_no_changes(self):
        from organizations.models import Organization
        from django.test.utils import CaptureQueriesContext
        orgs = Organization.objects.bulk_create([Organization(domain=f'legacy-{i}.synthetic.test', name=f'Legacy {i}') for i in range(500)])
        configs = OrganizationContentConfig.objects.bulk_create([OrganizationContentConfig(organization=org,
            pillar_strategy={'founder_journey': {'version': 1, 'cohort': 'legacy'}}) for org in orgs])
        ContentFactoryRun.objects.bulk_create([ContentFactoryRun(run_id=f'legacy-{org.pk}', organization=org,
            status='failed', workflow='direct_generate', result={'error_code': 'REVIEW_PROVIDER_UNAVAILABLE'}) for org in orgs])
        with patch.object(engine.logger, 'info'), patch.object(engine, '_execute_run') as execute:
            start = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                result = engine.run_policy_sweep(now=self.now, limit=500)
            elapsed = time.perf_counter() - start
        self.assertEqual(result['observed'], 500)
        self.assertLessEqual(len(queries), 3)
        self.assertLess(elapsed, 2)
        execute.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.filter(result__self_heal_v1__isnull=False).exists())

    def goal_operation(self, *, state='running'):
        from content_factory.website_models import WebsiteConnection, WebsiteConnectionOperation
        from content_factory import website_goal as goals, website_strategy as strategy
        website = WebsiteConnection.objects.create(organization=self.org, github_repo='example/site', branch='main',
            verified_sha='a' * 40, repository_id=123)
        operation = WebsiteConnectionOperation.objects.create(connection=website, generation=1, action='prepare',
            state=state, idempotency_key=f'goal:{website.pk}', payload={'company_id': str(self.fixture.company.pk)})
        goal = goals.new_goal(str(operation.pk), goals.scope_for(website, self.fixture.company.pk), now=self.now)
        goal['current_source_sha'] = website.verified_sha
        goal = strategy.set_pin(goal, 1, source=website.verified_sha, fingerprint='f' * 64, now=self.now, reason='inventory_selected')
        operation.receipt = {'goal': goal, 'reason_code': 'PUBLISHING_ADAPTER_REQUIRED', 'failure_class': 'strategy',
            'failure_occurrence_id': 'strategy-failure-one', 'failure': {'code': 'PUBLISHING_ADAPTER_REQUIRED',
                'failure_class': 'strategy', 'failure_fingerprint': 'f' * 64}}
        operation.save(update_fields=['receipt'])
        return operation

    def test_goal_policy_keeps_existing_controller_compare_and_set(self):
        from django.db import transaction
        from content_factory import website_goal_runner as runner, website_prepare as prepare
        operation = self.goal_operation()
        clock = operation.updated_at
        operation._claimed_at = clock
        operation._claimed_goal_revision = operation.receipt['goal']['revision']
        # The prepare observation changed state only in memory. The DB still
        # owns the running lease until its existing CAS commits the outcome.
        operation.state = 'needs_attention'
        with patch('content_factory.website_connections.authority_guard', side_effect=lambda *args, **kwargs: transaction.atomic()):
            runner.finish_goal(operation, now=self.now)
        self.assertEqual(operation.receipt['self_heal_v1']['attempts'], 1)
        self.assertEqual(operation.receipt['goal']['strategy_pin']['rung'], 'deterministic')
        saved = type(operation).objects.get(pk=operation.pk)
        self.assertEqual((saved.state, saved.updated_at), ('running', clock))
        self.assertIs(prepare._persist_prepare_claim(operation, now=self.now + timedelta(seconds=1)), operation)
        saved.refresh_from_db()
        self.assertEqual(saved.state, 'pending')
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertEqual(config.pillar_strategy['self_heal']['strategy_attempts'], 1)
        replay = {**saved.receipt, '_organization': self.org, '_operation': saved, 'run_request': saved.payload}
        self.assertEqual(engine.decide(replay, now=self.now + timedelta(seconds=2)).action, 'none')

    def test_goal_switch_has_no_channel_ask_and_one_ladder_alert(self):
        from content_factory.website_goal_runner import wait_for_channel
        operation = self.goal_operation()
        wait_for_channel(operation, operation.receipt['goal'])
        wait_for_channel(operation, operation.receipt['goal'])
        self.assertIsNone(operation.receipt['user_action'])
        self.assertNotIn('choose_article_channel', json.dumps(operation.receipt))
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertEqual(len(config.pillar_strategy['ops_alerts']), 1)

    def test_hosted_rung_requires_actual_readiness_and_enabled_executor(self):
        from content_factory import website_strategy as strategy
        from content_factory.self_heal_testing import fake_provider
        operation = self.goal_operation()
        goal = operation.receipt['goal']
        with fake_provider('content_factory.hosted_auto_register', hosted_rung_ready=lambda org: True):
            next_goal = strategy.advance(goal, source='a' * 40, now=self.now, reason='strategy_exhausted', organization=self.org)
        self.assertEqual(next_goal['strategy_pin']['rung'], 'hosted')
        with fake_provider('content_factory.hosted_auto_register', hosted_rung_ready=lambda org: False):
            next_goal = strategy.advance(goal, source='a' * 40, now=self.now, reason='strategy_exhausted', organization=self.org)
        self.assertEqual(next_goal['strategy_pin']['rung'], 'deterministic')

    def test_publish_only_recovery_cannot_enter_generic_article_orchestration(self):
        from content_factory.self_heal_testing import fake_provider
        run = self.run_for(result={'publish_reviewed_v1': {'source_run_id': 'saved-source'},
            'article_approval_v1': {'source': 'publish_reviewed', 'approved_body_sha256': 'a' * 64}})
        self.seed(run, first_failed_at=(self.now - timedelta(minutes=1)).isoformat())
        specialized = []
        with fake_provider('content_factory.publish_reviewed_draft', retry_publish=lambda value: specialized.append(value.run_id) or {'status': 'queued'}), \
                patch('content_factory.vibe_marketing_views._call_content_factory_run_action') as generic:
            engine.apply(run, engine.decide(run, now=self.now), now=self.now)
        self.assertEqual(specialized, [run.run_id])
        generic.assert_not_called()
