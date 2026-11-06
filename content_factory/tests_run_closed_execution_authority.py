"""Actual settled original obligations cannot renew work or provider authority."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory

from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import tests_run_terminal_settlement as terminal_tests
from .tests_run_event_authority import FLAGS
from .run_execution_control import ExecutionControlDenied, append_execution_ticket
from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, current_execution_ticket, owner_receipt, wire_identity


@override_settings(**FLAGS, ROO_API_KEY="synthetic-closed-authority-key")
class ClosedExecutionAuthorityTests(TransactionTestCase):
    setUp = terminal_tests.TerminalSettlementFenceTests.setUp
    paid = terminal_tests.TerminalSettlementFenceTests.paid
    closed = terminal_tests.TerminalSettlementFenceTests.closed
    retain = terminal_tests.TerminalSettlementFenceTests.retain

    def settled(self):
        run, _, spend = self.paid()
        effect, _ = PointsService.refund(user=spend.user, delta=12, source="CONTENT_FACTORY",
            description="Synthetic exact original settlement", idempotency_key="settled-authority-refund",
            created_by_slack_id="system", original_spend_key=spend.idempotency_key)
        self.retain(run, spend, effect)
        return run

    def test_fresh_append_and_user_resume_refuse_actual_refunded_original_without_mutation(self):
        run = self.settled()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with transaction.atomic(), self.assertRaises(ExecutionControlDenied) as denied:
            append_execution_ticket(run, control_key="d" * 64, reason="user_resume")
        self.assertEqual(denied.exception.reason, "execution_obligation_closed")
        from .run_user_resume import user_resume_payload
        with self.assertRaises(ExecutionControlDenied) as denied:
            user_resume_payload(run=run, user=self.payer, command_key="paid-again-requires-new-child", payload={})
        self.assertEqual(denied.exception.reason, "execution_obligation_closed")
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_actual_sdk_http_refuses_settled_original_before_budget_or_remote_reads(self):
        run = self.settled()
        from .run_execution_control_views import RunExecutionTicketView
        request = APIRequestFactory().post("/execution-ticket", {"schema_version": 1, "control_key": "d" * 64,
            "reason": "quality_repair", WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)),
            WIRE_EXECUTION_KEY: current_execution_ticket(run)}, format="json", HTTP_X_API_KEY="synthetic-closed-authority-key")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.get") as get:
            result = RunExecutionTicketView.as_view()(request, run_id=run.run_id)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertEqual(result.data["reason"], "execution_obligation_closed")
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_provider_and_worker_credentials_refuse_closed_work_but_history_reads_keep_their_scope(self):
        run = self.settled()
        from .website_connections import require_run_not_stopped
        from .website_contract import WebsiteAuthorityError
        with self.assertRaises(WebsiteAuthorityError) as denied:
            require_run_not_stopped({"run_id": run.run_id})
        self.assertEqual(denied.exception.code, "run_execution_closed")
        from .website_views import WebsiteConnectionAuthorizeView
        request = APIRequestFactory().get("/authorize", {"run_id": run.run_id, "action": "read"},
            HTTP_X_API_KEY="synthetic-closed-authority-key")
        result = WebsiteConnectionAuthorizeView.as_view()(request)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertEqual(result.data["code"], "run_execution_closed")
        self.assertNotIn("permission_mode", result.data)


    @override_settings(CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-closed-preview")
    def test_preview_post_cannot_renew_settled_original(self):
        run = self.settled()
        from . import vibe_marketing_views as views
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(views.http_client, "post") as post:
            result = views._call_content_factory_live_preview(run_id=run.run_id, method="POST", payload={"force": True})
        self.assertEqual(result["content_factory_status_code"], 409)
        self.assertIn("execution_obligation_closed", result["error"])
        post.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
