"""User denial is durable consent withdrawal, never a forged CF terminal state."""
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from .tests_run_event_authority import FLAGS, batch
from .self_heal_testing import make_p2_org
from .run_ownership import (USER_STOP_RESULT_KEY, bind_admitted_run, reserve_admitted_run,
    user_principal, user_stop_intent, public_run_value)
from .run_user_stop import record_user_denial
from workflow_runs.models import ContentFactoryRun


@override_settings(**FLAGS)
class UserStopIntentTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user = fixture.organization, fixture.owner
        self.actor = f"mlai_user:{self.user.pk}"
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_generation",
            payload={"domain": self.org.domain, "client_request_id": "stop-original", "topic": "Original"}), "stop-worker")

    def test_original_command_replays_and_withdraws_consent_without_claiming_execution_stopped(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        command = record_user_denial(self.run, user=self.user, actor_id=self.actor)
        self.assertEqual(record_user_denial(self.run, user=self.user, actor_id=self.actor), command)
        self.run.refresh_from_db()
        self.assertEqual(user_stop_intent(self.run), command)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertNotIn(USER_STOP_RESULT_KEY, public_run_value(self.run.result))
        from .run_execution_control import require_current_principal, append_execution_ticket, ExecutionControlDenied
        with self.assertRaises(ExecutionControlDenied):
            require_current_principal(self.run)
        with transaction.atomic(), self.assertRaises(ExecutionControlDenied):
            append_execution_ticket(self.run, control_key="a" * 64, reason="user_resume")

    def test_only_actual_typed_producer_denial_changes_execution_status(self):
        from .run_event_authority import receive_authoritative_run_events
        record_user_denial(self.run, user=self.user, actor_id=self.actor)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        response = receive_authoritative_run_events(batch(self.run, [{"status": "denied"}]))
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "denied")
        for field in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "approval_state"):
            self.assertEqual(getattr(self.run, field), before[field], field)
        self.assertIsNotNone(user_stop_intent(self.run))
