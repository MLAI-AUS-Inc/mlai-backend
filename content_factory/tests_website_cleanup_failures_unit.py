"""Cleanup failures preserve service work and never acquire newer repo authority."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
import requests

from .website_cleanup_failures import record_failure
from .website_disconnect import advance_disconnect_steps


class CleanupFailureTests(SimpleTestCase):
    def operation(self, *, generation=2):
        return SimpleNamespace(generation=generation, connection=SimpleNamespace(generation=3, state="connected"),
            payload={"remove_setup": True}, receipt={}, state="pending", next_attempt_at=None)

    def test_superseded_disconnect_refunds_original_work_without_repository_calls(self):
        op = self.operation()
        with patch("content_factory.website_disconnect.refund_cancelled_runs", return_value=True), \
                patch("content_factory.website_disconnect.close_owned_repository_refs") as refs, \
                patch("content_factory.website_disconnect._open_cleanup") as removal:
            advance_disconnect_steps(op)
        refs.assert_not_called()
        removal.assert_not_called()
        self.assertEqual(op.receipt["repository_cleanup"]["status"], "superseded")
        self.assertEqual(op.receipt["completed_steps"], ["refunds"])
        self.assertFalse(op.receipt["disconnect_steps_pending"])

    def test_permanent_provider_error_has_a_safe_step_and_stops_retries(self):
        op = self.operation()
        response = requests.Response()
        response.status_code = 403
        error = requests.HTTPError("Bearer secret-token private-response", response=response)
        record_failure(op, error, step="repository_refs")
        self.assertEqual(op.state, "needs_attention")
        self.assertNotIn("secret", str(op.receipt))
        self.assertEqual(op.receipt["cleanup_step_failures"]["repository_refs"]["http_status"], 403)

    def test_refund_failure_keeps_it_pending_without_blocking_supersession(self):
        op = self.operation()
        with patch("content_factory.website_disconnect.refund_cancelled_runs", side_effect=RuntimeError("private")):
            advance_disconnect_steps(op)
        self.assertTrue(op.receipt["disconnect_steps_pending"])
        self.assertEqual(op.receipt["repository_cleanup"]["status"], "superseded")
        self.assertNotIn("private", str(op.receipt))

    def test_missing_installation_keeps_http_status_without_provider_message(self):
        from integrations.services.github_app import GitHubAppTokenError
        op = self.operation()
        failure = record_failure(op, GitHubAppTokenError("private provider body", status_code=404), step="repository_refs")
        self.assertEqual(failure["http_status"], 404)
        self.assertFalse(failure["retryable"])
        self.assertNotIn("private", str(op.receipt))

    def test_history_keeps_old_cleanup_visible_without_copying_private_receipts(self):
        from .website_journey import cleanup_history
        rows = [{"id": "old", "action": "disconnect", "state": "needs_attention", "generation": 2,
            "receipt": {"remote_cleanup_pending": True, "provider_body": "secret"}},
            {"id": "finished", "action": "purge", "state": "completed", "generation": 1,
            "receipt": {"remote_cleanup_pending": True}}]
        result = cleanup_history(rows)
        self.assertEqual([row["id"] for row in result], ["old"])
        self.assertTrue(result[0]["needsAttention"])
        self.assertNotIn("secret", str(result))

    def test_history_preserves_retained_removal_after_service_cleanup_finishes(self):
        from .website_journey import cleanup_history
        rows = [{"id": "retained", "action": "disconnect", "state": "completed", "generation": 2,
            "receipt": {"remote_cleanup_pending": False,
                "repository_cleanup": {"status": "superseded", "retained": True}, "provider_body": "secret"}}]
        result = cleanup_history(rows)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["repositoryRemovalRetained"])
        self.assertFalse(result[0]["pending"])
        self.assertFalse(result[0]["needsAttention"])
        self.assertNotIn("secret", str(result))

    def test_history_explains_known_unsettled_steps_without_reviving_superseded_access(self):
        from .website_journey import cleanup_history
        receipt = {"remote_cleanup_pending": True,
            "repository_cleanup": {"status": "superseded", "retained": True},
            "cleanup_stop_reason": "preview_cleanup_abandoned",
            "cleanup_step_failures": {"repository_refs": {"code": "GITHUB_ACCESS_REQUIRED", "http_status": 403},
                "refunds": {"code": "worker_unavailable", "provider_body": "secret"}}}
        row = {"id": "old", "action": "disconnect", "state": "needs_attention", "generation": 2, "receipt": receipt}
        result = cleanup_history([row])[0]
        self.assertTrue(result["previewCleanupStopped"])
        self.assertTrue(result["refundPending"])
        self.assertFalse(result["repositoryAccessRequired"])
        self.assertFalse(result["repositoryServiceCheckRequired"])
        self.assertNotIn("secret", str(result))
        receipt["repository_cleanup"] = {}
        result = cleanup_history([row])[0]
        self.assertTrue(result["repositoryAccessRequired"])
        receipt["completed_steps"] = ["refunds", "repository_refs"]
        result = cleanup_history([row])[0]
        self.assertFalse(result["repositoryAccessRequired"])
        self.assertFalse(result["refundPending"])

    def test_blocked_repository_does_not_repeat_calls_or_stop_original_refunds(self):
        from .website_cleanup_failures import settle_failure_state
        op = self.operation(generation=3)
        op.connection.state = "disconnected"
        op.receipt = {"cleanup_step_failures": {"repository_refs": {"retryable": False}},
            "refund_pending_run_ids": ["original-spend"]}
        with patch("content_factory.website_disconnect.refund_cancelled_runs", return_value=False) as refunds, \
                patch("content_factory.website_disconnect.close_owned_repository_refs") as refs:
            advance_disconnect_steps(op)
        refunds.assert_called_once_with(op)
        refs.assert_not_called()
        settle_failure_state(op)
        self.assertEqual(op.state, "pending")
        op.receipt["refund_pending_run_ids"] = []
        settle_failure_state(op)
        self.assertEqual(op.state, "needs_attention")

    def test_explicit_retry_rechecks_access_without_erasing_history_or_resetting_deadline(self):
        from .website_cleanup_failures import retry_failure_steps
        receipt = {"cleanup_step_failures": {"repository_refs": {"retryable": False, "http_status": 403}},
            "preview_cleanup_budget": {"deadline": "original-deadline"}}
        retried = retry_failure_steps(receipt)
        self.assertEqual(retried["cleanup_step_failures"], {})
        self.assertEqual(retried["cleanup_failure_history"], [receipt["cleanup_step_failures"]])
        self.assertEqual(retried["preview_cleanup_budget"], receipt["preview_cleanup_budget"])

    def test_deadline_remains_stopped_even_with_a_blocked_step_and_pending_preview(self):
        from .website_cleanup_failures import settle_failure_state
        for reason in ["preview_cleanup_abandoned", "preview_cleanup_budget_invalid"]:
            op = self.operation()
            op.state = "needs_attention"
            op.payload["stop_preview_run_ids"] = ["original-preview"]
            op.receipt = {"cleanup_stop_reason": reason,
                "cleanup_step_failures": {"repository_refs": {"retryable": False}}}
            settle_failure_state(op)
            self.assertEqual(op.state, "needs_attention")
