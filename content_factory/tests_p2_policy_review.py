"""Independent persisted regressions for goal authority, lease ownership and PR accounting."""
from django.db import connections
from datetime import timedelta
from threading import Event, Thread
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from . import failure_policy_engine as engine
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON
from . import tests_p2_policy as policy_tests
from .website_connections import contract_for


@override_settings(**ALL_FLAGS_ON)
class PolicyReviewTests(TransactionTestCase):
    def setUp(self):
        self.helper = policy_tests.PolicyTests('test_goal_policy_keeps_existing_controller_compare_and_set')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.org, self.now = self.helper.org, self.helper.now

    def test_child_cannot_revive_a_cancelled_owning_goal(self):
        operation = self.helper.goal_operation(state='cancelled')
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED', run_request={**contract_for(operation.connection),
            'operation_id': str(operation.pk), 'expected_source_sha': 'a'*40})
        decision = engine.Decision('advance_rung', None, 'rung', 1, None, 'policy')
        with patch('content_factory.website_connections.authority_guard', side_effect=lambda *a, **k: transaction.atomic()):
            engine._execute_run(run, decision, now=self.now)
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'cancelled')
        self.assertNotIn('self_heal_v1', operation.receipt)

    def test_goal_pr_completes_its_operation_slot_once_without_double_count(self):
        operation = self.helper.goal_operation(state='pending')
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED', run_request={**contract_for(operation.connection),
            'operation_id': str(operation.pk), 'expected_source_sha': 'a'*40})
        run.domain = self.org.domain
        run.save(update_fields=['domain'])
        operation.receipt['setup_run_id'] = run.run_id
        operation.save(update_fields=['receipt'])
        key = f'{operation.pk}:1'
        self.assertTrue(engine.claim_pull_request_slot(self.org, key=key, now=self.now))
        self.assertEqual(engine.record_pull_request(self.org, pr_url='https://github.com/example/site/pull/1', run=run, now=self.now), 1)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertIn(key, config.pillar_strategy['self_heal'].get('pull_request_slot_completions', []))
        self.assertTrue(engine.claim_pull_request_slot(self.org, key='next-run:1', now=self.now))
        self.assertTrue(engine.claim_pull_request_slot(self.org, key='third-run:1', now=self.now))
        self.assertFalse(engine.claim_pull_request_slot(self.org, key='fourth-run:1', now=self.now))

    def test_uncertain_attempt_still_reaches_the_forty_eight_hour_wall(self):
        run = self.helper.run_for()
        self.helper.seed(run, attempts=1, first_failed_at=(self.now-timedelta(hours=49)).isoformat(),
            pending_action={'state': 'uncertain', 'action': 'retry', 'attempt': 1,
                'lease_until': (self.now-timedelta(minutes=1)).isoformat()})
        decision = engine.decide(run, now=self.now)
        self.assertEqual((decision.action, decision.cap), ('stop', 'wall'))

    def test_expired_executor_cannot_complete_a_new_lease_for_the_same_attempt(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Real independent transaction concurrency needs PostgreSQL.')
        run = self.helper.run_for()
        self.helper.seed(run, first_failed_at=(self.now-timedelta(minutes=1)).isoformat())
        first_entered, second_entered, release_first, release_second = Event(), Event(), Event(), Event()
        calls, errors = [], []
        def execute(current, decision, *, now):
            calls.append(decision.attempt)
            if len(calls) == 1:
                first_entered.set()
                release_first.wait(10)
                return {'status': 'queued'}
            second_entered.set()
            release_second.wait(10)
            return {'content_factory_transport_error': True}
        def invoke(now):
            close_old_connections()
            try:
                current = type(run).objects.select_related('organization').get(pk=run.pk)
                engine.apply(current, engine.decide(current, now=now), now=now)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        with patch.object(engine, '_execute_run', side_effect=execute):
            first = Thread(target=invoke, args=(self.now,))
            first.start()
            self.assertTrue(first_entered.wait(10))
            second = Thread(target=invoke, args=(self.now+timedelta(minutes=3),))
            second.start()
            self.assertTrue(second_entered.wait(10))
            release_first.set()
            first.join(10)
            try:
                run.refresh_from_db()
                self.assertEqual(run.result['self_heal_v1']['pending_action']['state'], 'reserved')
                self.assertTrue(run.result['self_heal_v1']['pending_action'].get('claim'))
            finally:
                release_second.set()
                second.join(10)
        self.assertFalse(errors)
        self.assertEqual(calls, [1, 1])
        run.refresh_from_db()
        self.assertEqual(run.result['self_heal_v1']['pending_action']['state'], 'uncertain')

    def test_channel_switch_lost_ack_retransmits_exact_key_after_lease_without_touching_clock(self):
        from . import channel_switch
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED')
        clock, original = run.updated_at, dict(run.run_request)
        responses = [{'status': 'pending'}, {'status': 'queued', 'channel_switch': {'version': 1, 'run_id': run.run_id,
            'key': f'{run.run_id}:copy-ready', 'state': 'applied', 'delivery_mode': 'content_only'}}]
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', side_effect=responses) as remote:
            first = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now)
            second = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now+timedelta(minutes=1))
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(first, second)
            final = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now+timedelta(minutes=3))
        self.assertEqual(final['dispatch_state'], 'accepted')
        self.assertEqual(remote.call_args_list[0].kwargs['payload'], remote.call_args_list[1].kwargs['payload'])
        run.refresh_from_db()
        self.assertEqual(run.run_request, original)
        self.assertEqual(run.updated_at, clock)

    def test_executor_off_keeps_the_legacy_recovery_owner(self):
        run = self.helper.run_for('WEBSITE_SOURCE_CHANGED')
        with override_settings(ARTICLES_SOURCE_REPIN_ENABLED=False):
            self.assertEqual(engine.decide(run).reason, 'executor_off')
            self.assertFalse(engine.owns_recovery(run))
        self.assertTrue(engine.owns_recovery(run))

    def test_no_article_consent_falls_back_to_portable_delivery(self):
        from . import channel_switch
        from .self_heal_testing import fake_provider
        from .publish_reviewed_draft import PublishReviewedError
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED')
        reply = {'channel_switch': {'version': 1, 'run_id': run.run_id, 'key': f'{run.run_id}:copy-ready',
                                   'state': 'applied', 'delivery_mode': 'content_only'}}
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', return_value=reply) as remote:
            receipt = channel_switch.switch_to_copy_ready(run=run, reason='strategy', now=self.now)
        self.assertEqual(receipt['channel'], 'copy_ready')
        self.assertEqual(receipt['dispatch_state'], 'accepted')
        remote.assert_called_once()

    def test_uncertain_transient_attempt_stops_after_twenty_four_hours(self):
        run = self.helper.run_for()
        self.helper.seed(run, attempts=1, first_failed_at=(self.now-timedelta(hours=25)).isoformat(),
            pending_action={'state': 'uncertain', 'action': 'retry', 'attempt': 1,
                            'lease_until': (self.now-timedelta(minutes=1)).isoformat()})
        decision = engine.decide(run, now=self.now)
        self.assertEqual((decision.action, decision.cap), ('stop', 'transient_elapsed'))

    def test_forged_operation_id_does_not_complete_an_unrelated_goal_pr_slot(self):
        operation = self.helper.goal_operation(state='pending')
        operation.receipt['setup_run_id'] = 'another-real-run'
        operation.save(update_fields=['receipt'])
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED', run_request={**contract_for(operation.connection),
            'operation_id': str(operation.pk)})
        key = f'{operation.pk}:1'
        engine.claim_pull_request_slot(self.org, key=key, now=self.now)
        engine.record_pull_request(self.org, pr_url='https://github.com/example/site/pull/1', run=run, now=self.now)
        self.assertNotIn(key, OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy['self_heal'].get('pull_request_slot_completions', []))

    def test_expired_channel_executor_cannot_complete_a_new_channel_lease(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Real independent transaction concurrency needs PostgreSQL.')
        from . import channel_switch
        run = self.helper.run_for('PUBLISHING_ADAPTER_REQUIRED')
        clock = run.updated_at
        one, two, release_one, release_two = Event(), Event(), Event(), Event()
        calls, errors = [], []
        def remote(**kwargs):
            calls.append(kwargs['payload'])
            if len(calls) == 1:
                one.set()
                release_one.wait(10)
                return {'channel_switch': {'version': 1, 'run_id': run.run_id, 'key': f'{run.run_id}:copy-ready', 'state': 'applied', 'delivery_mode': 'content_only'}}
            two.set()
            release_two.wait(10)
            return {'status': 'pending'}
        def invoke(now):
            close_old_connections()
            try:
                current = type(run).objects.select_related('organization').get(pk=run.pk)
                channel_switch.switch_to_copy_ready(run=current, reason='strategy', now=now)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', side_effect=remote):
            first = Thread(target=invoke, args=(self.now,))
            first.start()
            self.assertTrue(one.wait(10))
            second = Thread(target=invoke, args=(self.now+timedelta(minutes=3),))
            second.start()
            self.assertTrue(two.wait(10))
            release_one.set()
            first.join(10)
            try:
                run.refresh_from_db()
                self.assertEqual(run.result['channel_v1']['dispatch_state'], 'pending')
            finally:
                release_two.set()
                second.join(10)
        self.assertFalse(errors)
        self.assertEqual(calls[0], calls[1])
        run.refresh_from_db()
        self.assertEqual(run.updated_at, clock)

    def test_future_worker_clock_cannot_delay_first_retry_or_recovery_wall(self):
        run = self.helper.run_for(result={"first_failed_at": (self.now+timedelta(days=365)).isoformat()})
        decision = engine.decide(run, now=self.now)
        self.assertEqual(decision.at, self.now+timedelta(minutes=1))

    def test_old_or_wrong_cf_ack_keeps_same_policy_attempt_pending(self):
        run = self.helper.run_for()
        from dataclasses import replace
        decision = replace(engine.decide(run, now=self.now), at=self.now)
        ack = {'version': 1, 'run_id': run.run_id, 'key': f'{run.run_id}:1', 'intent': 'resume', 'state': 'accepted'}
        for response in ({'status': 'queued'}, {'status': 'queued', 'self_heal_resume': {**ack, 'key': 'foreign:1'}},
                         {'status': 'queued', 'self_heal_resume': {**ack, 'intent': 'rescan'}}):
            with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', return_value=response):
                self.assertEqual(engine._execute_run(run, decision, now=self.now)['dispatch_state'], 'pending')
        with patch('content_factory.vibe_marketing_views._call_content_factory_run_action', return_value={'status': 'queued', 'self_heal_resume': ack}):
            self.assertEqual(engine._execute_run(run, decision, now=self.now)['self_heal_resume'], ack)

    def test_real_bound_policy_resume_retains_clock_and_operation_identity(self):
        from types import SimpleNamespace
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from . import vibe_marketing_views as views
        website = WebsiteConnection.objects.create(organization=self.org, repository_id=42,
            github_repo='fixture/site', installation_id='99', generation=1, branch='main',
            authorized_by=self.helper.fixture.owner, verified_sha='a'*40)
        self.helper.fixture.config.website_connection = website
        self.helper.fixture.config.github_repo = website.github_repo
        self.helper.fixture.config.save(update_fields=['website_connection', 'github_repo'])
        run = self.helper.run_for(workflow='direct_generate', run_request={**contract_for(website),
            'domain': self.org.domain, 'github_repo': website.github_repo, 'expected_source_sha': 'a'*40})
        operation = WebsiteConnectionOperation.objects.create(connection=website, generation=1, action='workflow',
            state='failed', idempotency_key='policy-clock-operation', payload={'run_id': run.run_id, 'attempt': 1})
        run.domain, run.github_repo = self.org.domain, website.github_repo
        run.run_request.update(operation_id=str(operation.pk), operation_attempt=1, deletion_epoch=0)
        run.save(update_fields=['domain', 'github_repo', 'run_request'])
        clock = run.updated_at
        response = SimpleNamespace(status_code=200, content=b'{}', json=lambda: {'status': 'queued',
            'self_heal_resume': {'version': 1, 'run_id': run.run_id, 'key': f'{run.run_id}:1', 'intent': 'resume', 'state': 'accepted'}})
        with patch.object(views, '_content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://fixture.invalid'}), \
                patch.object(views, '_content_factory_headers', return_value={}), \
                patch.object(views.http_client, 'post', return_value=response) as remote, \
                patch('content_factory.website_connections.read_repository_native_target', return_value={'id': 42, 'full_name': website.github_repo, 'default_branch': 'main'}):
            outcome = engine._execute_run(run, engine.Decision('retry', None, None, 1, None, 'policy'), now=self.now)
            self.assertIn('self_heal_resume', outcome, outcome)
            self.assertEqual(outcome['self_heal_resume']['state'], 'accepted')
            operation.refresh_from_db(); run.refresh_from_db()
            self.assertEqual(operation.payload['attempt'], 2)
            self.assertEqual(run.run_request['operation_attempt'], 2)
            self.assertEqual(run.updated_at, clock)
            self.assertEqual(remote.call_args.kwargs['json']['self_heal_key'], f'{run.run_id}:1')
            self.assertEqual(remote.call_args.kwargs['json']['website_connection_id'], str(website.pk))
            # A retransmission reserves no third execution or clock update.
            engine._execute_run(run, engine.Decision('retry', None, None, 1, None, 'policy'), now=self.now)
            operation.refresh_from_db(); run.refresh_from_db()
            self.assertEqual(operation.payload['attempt'], 2)
            self.assertEqual(run.updated_at, clock)
            # Revocation is still checked before dispatching the original tuple.
            website.state = 'disconnected'; website.save(update_fields=['state'])
            refused = engine._execute_run(run, engine.Decision('retry', None, None, 1, None, 'policy'), now=self.now)
            self.assertEqual(refused['dispatch_state'], 'pending')
            self.assertEqual(remote.call_count, 2)
