"""Observed dispatch outcomes and honest preparation receipts, without DB/HTTP."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import dispatch_outbox, reconciliation, website_operations, website_prepare
from . import vibe_marketing_views as views
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation
from .dispatch_models import ContentFactoryDispatchOutbox


def rows(*, first=None, items=()):
    query = MagicMock()
    for method in ("filter", "exclude", "select_related", "select_for_update", "order_by"):
        getattr(query, method).return_value = query
    query.first.return_value = first
    query.__iter__.side_effect = lambda: iter(items)
    query.__getitem__.return_value = list(items)
    query.exists.return_value = False
    query.update.return_value = 1
    return query


class PrepareRetryReceiptTests(SimpleTestCase):
    def operation(self):
        return SimpleNamespace(pk="prepare", action="prepare", state="running", payload={},
            connection=SimpleNamespace(organization_id=42, capabilities={}),
            receipt={"current_step": "setup"}, next_attempt_at=None, updated_at=timezone.now(), save=Mock())

    def test_transient_budget_exhaustion_stops_promising_automatic_retry(self):
        op = self.operation()
        error = WebsiteAuthorityError("website_prepare_unavailable",
            "Website preparation is temporarily unavailable. It will retry automatically.", retryable=True)
        now = timezone.now()
        for attempt in range(1, 5):
            website_prepare.prepare_failure(op, "setup", error, now=now if attempt < 4 else now + timedelta(hours=24))
            self.assertEqual(op.state, "pending" if attempt < 4 else "needs_attention")
            if attempt < 4:
                self.assertIsNotNone(op.next_attempt_at)
                self.assertIn("retry automatically", op.receipt["message"])
            else:
                self.assertIsNone(op.next_attempt_at)
                self.assertNotIn("automatically", op.receipt["message"])
                self.assertIn("4 attempts", op.receipt["message"])
                self.assertIn("Try again", op.receipt["message"])

    def test_every_terminal_failure_transition_has_no_retry_promise(self):
        for code, step, retryable, limit in (("deployment_source_unverified", "verify", True, 6),
                ("worker_unavailable", "setup", False, 1), ("worker_unavailable", "scan", True, 3)):
            op = self.operation()
            now = timezone.now()
            op.receipt.update(transient_started_at=(now - timedelta(hours=24)).isoformat(),
                deployment_wait_started_at=(now - timedelta(hours=24)).isoformat())
            error = WebsiteAuthorityError(code, "Provider unavailable. It will retry automatically.", retryable=retryable)
            for _ in range(limit):
                website_prepare.prepare_failure(op, step, error, now=now)
            with self.subTest(code=code, retryable=retryable):
                self.assertEqual(op.state, "needs_user" if code == "deployment_source_unverified" else "needs_attention")
                if code == "deployment_source_unverified":
                    self.assertEqual(op.next_attempt_at, now + timedelta(hours=2))
                else:
                    self.assertIsNone(op.next_attempt_at)
                self.assertNotIn("automatically", op.receipt["message"])

    def test_old_terminal_and_unscheduled_receipts_are_sanitized_without_row_writes(self):
        for state in ("needs_attention", "stopped", "cancelled", "running", "pending"):
            op = self.operation()
            op.state = state
            op.receipt = {"current_step": "setup", "status": "working", "step_failures": {"setup": 3},
                "message": "Website preparation is temporarily unavailable. It will retry automatically."}
            stored = deepcopy(op.receipt)
            with self.subTest(state=state):
                summary = website_operations.operation_summary(op)
                self.assertNotIn("automatically", summary["receipt"]["message"])
                self.assertIsNone(summary["nextAttemptAt"])
                self.assertEqual(op.receipt, stored)
                op.save.assert_not_called()

    def test_scheduled_pending_retry_keeps_its_promise(self):
        op = self.operation()
        op.state = "pending"
        op.next_attempt_at = timezone.now() + timedelta(minutes=1)
        op.receipt = {"status": "working", "message": "It will retry automatically."}
        summary = website_operations.operation_summary(op)
        self.assertEqual(summary["receipt"], op.receipt)
        self.assertIsNotNone(summary["nextAttemptAt"])

    def test_terminal_receipt_hides_a_stale_schedule_without_changing_it(self):
        op = self.operation()
        op.state = "needs_attention"
        op.next_attempt_at = timezone.now() + timedelta(minutes=1)
        op.receipt = {"message": "Provider unavailable. It will retry later.", "step_failures": {"setup": "bad"},
            "deployment_attempts": None}
        stored = deepcopy(op.receipt)
        summary = website_operations.operation_summary(op)
        self.assertIsNone(summary["nextAttemptAt"])
        self.assertNotIn("will retry", summary["receipt"]["message"])
        self.assertIsNotNone(op.next_attempt_at)
        self.assertEqual(op.receipt, stored)
        op.save.assert_not_called()


@override_settings(CONTENT_FACTORY_URL="https://worker.example.test", CONTENT_FACTORY_API_KEY="synthetic", IS_LOCAL_ENV=False)
class DispatchObservationTests(SimpleTestCase):
    @staticmethod
    def response(status, body):
        return SimpleNamespace(status_code=status, content=b"synthetic", json=lambda: body)

    def pending_run(self):
        now = timezone.now()
        return SimpleNamespace(pk=12, run_id="saved-key", workflow="article_generation", status="blocked",
            domain="fixture.test", github_repo="fixture/site", organization_id=7, created_at=now - timedelta(minutes=5),
            result={}, run_request={"client_request_id": "saved-key", "dispatch_pending_resolution": True,
                "pending_billing_refund": {"charged_user_id": 4, "actor_id": "mlai_user:4",
                    "article_request": {"client_request_id": "saved-key"}}}, save=Mock())

    def test_real_lookup_maps_each_observed_response(self):
        cases = ((404, {"detail": "client_request_id not found"}, "not_received"),
            (404, {"detail": "Run not found"}, "unknown"),
            (200, {"key_status": "failed", "error": "worker_queue_unavailable"}, "received_not_started"),
            (200, {"key_status": "failed", "error": "backend_rejected:run_cancelled"}, "rejected"),
            (200, {"key_status": "dispatched", "run_id": "actual-child"}, "started"),
            (200, ["malformed"], "unknown"),
            (200, {"key_status": "claimed"}, "unknown"), (403, {}, "unknown"),
            (422, {}, "unknown"), (500, {}, "unknown"))
        for status, body, expected in cases:
            with self.subTest(status=status, body=body), patch.object(views.http_client, "get", return_value=self.response(status, body)):
                outcome, observed = views._lookup_content_factory_dispatch_by_key(views._content_factory_remote_config(), "saved-key")
            self.assertEqual(outcome, expected)
            if status == 200 and isinstance(body, dict) and body.get("error"):
                self.assertEqual(observed["error"], body["error"])

    def test_real_poll_lookup_failure_and_refund_chain_preserves_original_spend(self):
        from .models import ContentFactoryJob
        from roo.models import Ledger
        from roo.services import PointsService
        for status, body, outcome, code in (
            (404, {"detail": "client_request_id not found"}, "not_received", "dispatch_child_missing"),
            (200, {"key_status": "failed", "error": "worker_queue_unavailable"}, "received_not_started", "received_not_started")):
            run = self.pending_run()
            user = get_user_model()(pk=4, is_active=True)
            spend = SimpleNamespace(id=20, delta_microroo=-6_000_000)
            with self.subTest(outcome=outcome), ExitStack() as stack:
                stack.enter_context(patch.object(views.http_client, "get", return_value=self.response(status, body)))
                stack.enter_context(patch.object(views.transaction, "atomic", side_effect=lambda *a, **kw: nullcontext()))
                stack.enter_context(patch.object(views.ContentFactoryRun.objects, "select_for_update", return_value=rows(first=run)))
                stack.enter_context(patch.object(views.ContentFactoryRun.objects, "filter", return_value=rows(items=[run])))
                stack.enter_context(patch.object(get_user_model().objects, "filter", return_value=rows(first=user)))
                stack.enter_context(patch.object(Ledger.objects, "filter", return_value=rows(first=spend)))
                stack.enter_context(patch.object(ContentFactoryJob.objects, "filter", return_value=rows()))
                refund = stack.enter_context(patch.object(PointsService, "refund", return_value=(SimpleNamespace(id=21), True)))
                self.assertIs(views._resolve_dispatch_token_run(run), run)
                self.assertIs(views._resolve_dispatch_token_run(run), run)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.result.get("dispatch_outcome"), outcome)
            self.assertEqual(run.result["error_code"], code)
            self.assertEqual(run.result["dispatch_confirmed_absent"], outcome == "not_received")
            self.assertFalse(run.run_request["dispatch_pending_resolution"])
            self.assertTrue(run.result["dispatch_refund_processed"])
            self.assertEqual(run.run_request["roo_points_billing_status"], "refunded")
            refund.assert_called_once()
            self.assertEqual(refund.call_args.kwargs["delta"], 6)
            self.assertEqual(refund.call_args.kwargs["idempotency_key"], "content_factory:refund:saved-key")
            if outcome == "received_not_started":
                self.assertEqual(run.error, "Content Factory received this request but couldn't start it (worker_queue_unavailable). Any Roo points charged are being refunded — try again when ready.")
                self.assertNotIn("never received", run.error)
            else:
                self.assertIn("did not receive", run.error)

    def test_unknown_lookup_never_fails_the_run_or_releases_a_refund(self):
        for status, body in ((404, {"detail": "Run not found"}), (403, {}), (422, {}), (500, {}),
                (200, {"key_status": "claimed"})):
            run = self.pending_run()
            with self.subTest(status=status, body=body), patch.object(views.http_client, "get", return_value=self.response(status, body)):
                self.assertIsNone(views._resolve_dispatch_token_run(run))
            run.save.assert_not_called()
            self.assertEqual(run.status, "blocked")
            self.assertTrue(run.run_request["dispatch_pending_resolution"])

    def test_no_work_receipts_keep_the_existing_grace_window(self):
        for status, body in ((404, {"detail": "client_request_id not found"}),
                (200, {"key_status": "failed", "error": "worker_queue_unavailable"})):
            run = self.pending_run()
            run.created_at = timezone.now()
            with self.subTest(status=status), patch.object(views.http_client, "get", return_value=self.response(status, body)):
                self.assertIsNone(views._resolve_dispatch_token_run(run))
            run.save.assert_not_called()

    def test_restart_reconciliation_keeps_the_actual_failure_outcome(self):
        for status, body, outcome, code in (
            (404, {"detail": "client_request_id not found"}, "not_received", "dispatch_child_missing"),
            (200, {"key_status": "failed", "error": "worker_queue_unavailable"}, "received_not_started", "received_not_started"),
            (200, {"key_status": "failed", "error": "backend_rejected:run_cancelled"}, "rejected", "dispatch_rejected")):
            original = self.pending_run()
            original.result = {"restart_receipt": {"state": "pending", "client_request_id": "saved-key"}}
            def query(**kwargs):
                return rows(items=[original]) if "result__restart_receipt__state" in kwargs else rows()
            with self.subTest(outcome=outcome), patch.object(views.http_client, "get", return_value=self.response(status, body)), \
                    patch.object(reconciliation.ContentFactoryRun.objects, "filter", side_effect=query), \
                    patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update", return_value=rows(first=original)), \
                    patch.object(reconciliation.transaction, "atomic", side_effect=lambda *a, **kw: nullcontext()):
                self.assertEqual(reconciliation._reconcile_restart_receipts(limit=20, now=timezone.now()), 1)
            receipt = original.result["restart_receipt"]
            self.assertEqual(receipt["state"], "failed")
            self.assertEqual(receipt["reused_authorization"], "released")
            self.assertFalse(receipt["charged"])
            self.assertEqual(receipt["error_code"], code)
            self.assertEqual(receipt.get("dispatch_outcome"), outcome)
            self.assertNotIn("refund", receipt["message"])
            if outcome == "received_not_started":
                self.assertIn("received this request but couldn't start", receipt["message"])
                self.assertIn("worker_queue_unavailable", receipt["message"])

    def test_missing_request_identity_is_unknown(self):
        with patch.object(views.http_client, "get") as network:
            self.assertEqual(reconciliation._lookup_orphan_child(SimpleNamespace(payload={}))[0], "unknown")
        network.assert_not_called()

    def test_received_failure_reason_uses_existing_bounded_secret_redaction(self):
        error = "Queue unavailable\nBearer sensitive-fixture-token token=sensitive-fixture-value " + "x" * 400
        code, message = dispatch_outbox.dispatch_failure_details("received_not_started", {"error": error})
        self.assertEqual(code, "received_not_started")
        self.assertIn("Queue unavailable", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn("sensitive-fixture-token", message)
        self.assertNotIn("sensitive-fixture-value", message)
        self.assertLess(len(message), 450)

    def test_orphan_reconciliation_records_received_failure_and_refunds_original_spend(self):
        from .models import ContentFactoryJob
        from roo.models import Ledger
        from roo.services import PointsService
        user = get_user_model()(pk=4, is_active=True)
        organization = SimpleNamespace(pk=7, domain="fixture.test")
        op = SimpleNamespace(pk="operation", state="running", updated_at=timezone.now(), receipt={},
            payload={"client_request_id": "saved-key"},
            connection=SimpleNamespace(organization_id=7, organization=organization), save=Mock())
        spend = SimpleNamespace(id=20, user=user, delta_microroo=-6_000_000, created_by_slack_id="mlai_user:4")
        writes = []
        def update(**values):
            writes.append(values)
            op.state, op.receipt = values.get("state", op.state), values.get("receipt", op.receipt)
            return 1
        changed = rows()
        changed.update.side_effect = update
        def operations(**kwargs):
            if "pk" in kwargs:
                return changed
            return rows(items=[op]) if "state__in" in kwargs else rows()
        body = {"key_status": "failed", "error": "worker_queue_unavailable"}
        with ExitStack() as stack:
            stack.enter_context(patch.object(views.http_client, "get", return_value=self.response(200, body)))
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda *a, **kw: nullcontext()))
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=operations))
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=rows(first=op)))
            stack.enter_context(patch.object(ContentFactoryDispatchOutbox.objects, "filter", return_value=rows()))
            stack.enter_context(patch.object(views.ContentFactoryRun.objects, "filter", return_value=rows()))
            stack.enter_context(patch.object(Ledger.objects, "filter", return_value=rows(first=spend)))
            stack.enter_context(patch.object(ContentFactoryJob.objects, "filter", return_value=rows()))
            refund = stack.enter_context(patch.object(PointsService, "refund", return_value=(SimpleNamespace(id=21), True)))
            self.assertEqual(reconciliation._reconcile_orphan_operations(limit=20, now=timezone.now()), {"failed_orphan_receipts": 1})
        self.assertEqual(writes[0]["receipt"]["reason_code"], "received_not_started")
        self.assertEqual(op.receipt["dispatch_outcome"], "received_not_started")
        self.assertIn("worker_queue_unavailable", op.receipt["message"])
        self.assertNotIn("never received", op.receipt["message"])
        self.assertFalse(op.receipt["refund_pending"])
        self.assertTrue(op.receipt["refund_settled"])
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["delta"], 6)

    def test_exhausted_outbox_keeps_distinct_no_work_outcomes(self):
        row = SimpleNamespace(client_request_id="saved-key", payload={"request": {}})
        for status, body, code in (
            (404, {"detail": "client_request_id not found"}, "dispatch_never_received"),
            (200, {"key_status": "failed", "error": "worker_queue_unavailable"}, "received_not_started"),
            (200, {"key_status": "failed", "error": "backend_rejected:run_cancelled"}, "dispatch_rejected")):
            with self.subTest(code=code), patch.object(views.http_client, "get", return_value=self.response(status, body)):
                self.assertEqual(dispatch_outbox._resolve_exhausted_dispatch(row), ("failed", code, ""))
