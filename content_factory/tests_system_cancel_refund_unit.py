"""A system cancellation refunds automatically (founder-proof plan, workstream 6).

The stall sweep stops an article whose worker heartbeat was lost. Content
Factory seals the stop with an ``article_cancellation_v1`` receipt. When that
receipt says no value was delivered, the backend's existing refund path must
run without a manual step: the receipt queues the terminal refund, and the
reconciliation refund pass credits the original charge. An unknown delivered
value (what Content Factory sealed for a packaged but undelivered article before
CONTENT_FACTORY_SYSTEM_CANCEL_NO_VALUE_RECEIPT) stays with reconciliation.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import service_views
from . import tests_article_publication_evidence_unit as evidence


class HeartbeatLostRefundTests(SimpleTestCase):
    # Same saved paid article and sealed no-value receipt as the settlement tests.
    setUp = evidence.ArticleCancellationSettlementTests.setUp
    reseal = evidence.ArticleCancellationSettlementTests.reseal

    def stop_after_heartbeat_loss(self, receipt):
        from . import reconciliation
        self.run.status = "running"
        self.run.updated_at = timezone.now() - timedelta(minutes=20)
        self.run.result["reconciliation_recovery"] = {"requested_at": (timezone.now() - timedelta(minutes=10)).isoformat()}
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(reconciliation.requests, "post", return_value=SimpleNamespace(status_code=202,
                    json=lambda: {"article_cancellation_v1": receipt})) as remote, \
                patch.object(reconciliation, "_finalize_run_failure") as fail, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            locked.return_value.filter.return_value.first.return_value = self.run
            rows.return_value.update.return_value = 1
            outcome = reconciliation.recover_stalled_run(self.run, base_url="https://worker.fixture.test",
                                                         headers={}, now=timezone.now())
        fail.assert_not_called()
        body = remote.call_args.kwargs["json"]
        self.assertEqual((body["cancellation_reason"], body["cancelled_by"]), ("worker_heartbeat_lost", "system"))
        return outcome

    def refund_pass(self):
        from . import reconciliation
        ledger = SimpleNamespace(user=SimpleNamespace(pk=4), created_by_slack_id="",
                                 idempotency_key="content_factory:charge:original-charge")
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=ledger), \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user",
                      return_value=SimpleNamespace(pk=555)) as refund, \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as rows:
            rows.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="worker_heartbeat_lost")
        return refund

    def test_heartbeat_loss_with_a_no_value_receipt_refunds_without_a_manual_step(self):
        outcome = self.stop_after_heartbeat_loss(self.receipt)
        self.assertEqual(outcome, "cancelled_stalled")
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual((self.run.result["cancellation_reason"], self.run.result["cancelled_by"]),
                         ("worker_heartbeat_lost", "system"))
        self.assertFalse(self.run.result["reconciliation_required"])
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "pending")

        refund = self.refund_pass()
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["article_request"]["client_request_id"], "original-charge")
        self.assertEqual(refund.call_args.kwargs["reason"], "worker_heartbeat_lost")
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "refunded")
        self.assertFalse(self.run.result["reconciliation_refund_pending"])

    def test_an_unknown_delivered_value_is_left_to_reconciliation(self):
        unknown = self.reseal({**self.receipt, "delivered_value": None})
        self.assertEqual(self.stop_after_heartbeat_loss(unknown), "cancelled_stalled")
        self.assertNotIn("reconciliation_refund_pending", self.run.result)
        self.assertNotIn("article_terminal_refund", self.run.result)
