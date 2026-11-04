"""Actual owned history cannot enter the competing legacy policy repair loop."""
from copy import deepcopy
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.db import transaction
from django.utils import timezone
from workflow_runs.models import ContentFactoryRun
from . import failure_policy_engine as engine
from .run_ownership import reserve_admitted_run, user_principal
from .run_event_authority import receive_authoritative_run_events
from .tests_run_event_authority import batch
from .tests_run_execution_control import FLAGS
from .self_heal_testing import make_p2_org
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .models import OrganizationContentConfig


@override_settings(**FLAGS, ARTICLES_RUN_CONTROL_MODEL_USD='5.00', ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000)
class OwnedPolicyRetirementTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.run = reserve_admitted_run(organization=self.org, workflow='article_generation',
            principal=user_principal(self.user.pk, f'mlai_user:{self.user.pk}'),
            payload={'domain': self.org.domain, 'client_request_id': 'owned-policy-original', 'topic': 'Retained topic'})
        self.assertEqual(receive_authoritative_run_events(batch(self.run, [{'status': 'running'},
            {'type': 'failure', 'failure': {'code': 'REVIEW_PROVIDER_UNAVAILABLE', 'stage': 'article'}},
            {'status': 'failed'}])).status_code, 200)
        self.run.refresh_from_db()
        self.now = timezone.now()
        self.forged = engine.Decision('resume', self.now, 'repair', 1, None, 'policy')

    def test_flags_on_or_off_direct_decision_apply_execute_writer_and_scheduler_preserve_owner(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        config = OrganizationContentConfig.objects.values().get(organization=self.org)
        stale = deepcopy(self.run)
        stale.result = {}  # The fresh locked effect boundary wins over a stale caller.
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=enabled,
                    ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=enabled, ARTICLES_SELF_HEAL_POLICY_ENABLED=enabled), \
                    patch('content_factory.vibe_marketing_views._call_content_factory_run_action') as remote, \
                    patch('requests.get') as get, patch('requests.post') as post:
                self.assertEqual(engine.decide(self.run).reason, 'execution_owned')
                self.assertTrue(engine.owns_recovery(self.run))
                self.assertEqual(engine.apply(stale, self.forged)['status'], 'deferred')
                self.assertEqual(engine._execute_run(stale, self.forged, now=self.now)['status'], 'deferred')
                engine._write_run(stale, {'attempts': 99})
                with transaction.atomic():
                    locked = ContentFactoryRun.objects.select_for_update().get(pk=stale.pk)
                    engine._write_locked_run(locked, {'attempts': 99})
                self.assertEqual(engine._candidates(self.now, 50), [])
                report = engine.run_policy_sweep(now=self.now)
                self.assertEqual(report['checked'], 0)
                remote.assert_not_called(); get.assert_not_called(); post.assert_not_called()
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
            self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), config)

    def test_owned_goal_child_defers_saved_and_direct_operation_before_any_policy_counter_or_provider(self):
        connection = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo='synthetic/policy', repository_id=42, installation_id='1001', branch='main', verified_sha='a'*40)
        operation = WebsiteConnectionOperation.objects.create(connection=connection, generation=1, action='prepare',
            state='needs_attention', idempotency_key='owned-policy-goal',
            payload={'company_id': str(self.fixture.company.pk)},
            receipt={'child_run_id': self.run.run_id, 'failure_occurrence_id': 'retained-failure',
                'self_heal_v1': {'version': 1, 'attempts': 0}})
        before = WebsiteConnectionOperation.objects.values().get(pk=operation.pk)
        run_before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        config = OrganizationContentConfig.objects.values().get(organization=self.org)
        subject = {**operation.receipt, '_operation': operation, '_organization': self.org, 'run_request': operation.payload}
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(ARTICLES_SELF_HEAL_POLICY_ENABLED=enabled), \
                    patch('content_factory.website_goal_runner.retry_goal_action') as retry, \
                    patch('requests.get') as get, patch('requests.post') as post:
                self.assertEqual(engine.decide(subject).reason, 'execution_owned')
                self.assertEqual(engine.apply(subject, self.forged)['status'], 'deferred')
                self.assertEqual(engine._apply_saved_operation(subject, self.forged, now=self.now)['status'], 'deferred')
                self.assertEqual(engine._apply_operation(subject, self.forged, now=self.now)['status'], 'deferred')
                self.assertEqual(engine.run_policy_sweep(now=self.now)['errors'], 0)
                retry.assert_not_called(); get.assert_not_called(); post.assert_not_called()
            self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=operation.pk), before)
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), run_before)
            self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), config)
