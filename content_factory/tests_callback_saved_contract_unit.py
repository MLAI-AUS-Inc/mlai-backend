"""Callback consent fallback and in-review callback refusals, without a database."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase


CONNECTION_ID = "96ce56de-8970-4939-9a7a-173303fdd119"
SAVED_REQUEST = {
    "website_connection_id": CONNECTION_ID, "connection_generation": 11,
    "connection_target_id": "registry_driven_seo:src/lib/articles.ts:articles", "repository_id": 1411383517,
    "operation_id": "ecebb832-c344-4585-b2d3-a8e2e76215c6", "operation_attempt": 1, "deletion_epoch": 12,
    "domain": "example.test", "github_repo": "fixture/site",
}


def saved_run(request=None):
    return SimpleNamespace(run_id="run-1", domain="example.test", github_repo="fixture/site",
                           run_request=dict(SAVED_REQUEST if request is None else request))


class SavedRunContractTests(SimpleTestCase):
    def call(self, payload, run, *, authoritative=False):
        from . import website_connections
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = run
        with mock.patch("workflow_runs.models.ContentFactoryRun.objects", manager), \
                mock.patch("content_factory.run_reconciliation.authoritative_run", return_value=authoritative), \
                mock.patch("content_factory.run_ownership.owner_receipt", return_value=None):
            return website_connections.with_saved_run_contract(payload, run_id=None)

    def test_contractless_callback_uses_the_runs_own_saved_consent(self):
        from .website_contract import connection_contract
        payload = {"event_type": "article_review_ready", "job_id": "run-1", "publish_targets": []}
        filled = self.call(payload, saved_run())
        self.assertEqual(connection_contract(filled), connection_contract(SAVED_REQUEST))
        self.assertEqual(filled["operation_id"], SAVED_REQUEST["operation_id"])
        self.assertEqual(filled["deletion_epoch"], 12)
        self.assertEqual(filled["publish_targets"], [])
        self.assertNotIn("website_connection_id", payload)

    def test_payload_contract_is_never_overwritten(self):
        payload = {"job_id": "run-1", "website_connection_id": CONNECTION_ID, "connection_generation": 10}
        self.assertIs(self.call(payload, saved_run()), payload)

    def test_partial_payload_tuple_still_fails_closed(self):
        payload = {"job_id": "run-1", "repository_id": 1411383517}
        self.assertIs(self.call(payload, saved_run()), payload)

    def test_no_fallback_without_a_saved_contract_or_for_owned_runs(self):
        payload = {"job_id": "run-1", "event_type": "article_progress"}
        self.assertIs(self.call(payload, None), payload)
        self.assertIs(self.call(payload, saved_run({"domain": "example.test"})), payload)
        self.assertIs(self.call(payload, saved_run(), authoritative=True), payload)
        no_run_id = {"event_type": "article_progress"}
        self.assertIs(self.call(no_run_id, saved_run()), no_run_id)


class ReviewCallbackRefusalTests(SimpleTestCase):
    def refused_result(self):
        failure = {"code": "website_connection_required", "stage": "await_review", "retryable": False,
                   "requires_user_action": True, "next_action": "inspect_failure_report", "version": 1}
        return {"status": "fallback_ready", "review_required": True, "failure": failure,
                "error_code": "website_connection_required", "retryable": False, "requires_user_action": True,
                "next_action": "inspect_failure_report",
                "callback_rejection": {"code": "website_connection_required", "message": "Reconnect this website to continue."}}

    def test_refused_callback_does_not_fail_a_run_awaiting_review(self):
        from .run_state import clear_review_callback_refusal
        cleaned = clear_review_callback_refusal(self.refused_result(), status="approval_required")
        for key in ("failure", "error_code", "callback_rejection", "retryable", "requires_user_action", "next_action"):
            self.assertNotIn(key, cleaned)
        self.assertEqual(cleaned["cleared_callback_refusal"], {"code": "website_connection_required", "stage": "await_review"})
        self.assertTrue(cleaned["review_required"])
        self.assertEqual(cleaned["status"], "fallback_ready")

    def test_terminal_runs_and_unrelated_failures_are_untouched(self):
        from .run_state import clear_review_callback_refusal
        result = self.refused_result()
        for status in ("failed", "blocked", "running", ""):
            with self.subTest(status=status):
                self.assertIs(clear_review_callback_refusal(result, status=status), result)
        no_rejection = {key: value for key, value in result.items() if key != "callback_rejection"}
        self.assertIs(clear_review_callback_refusal(no_rejection, status="approval_required"), no_rejection)

    def test_a_different_real_failure_is_kept(self):
        from .run_state import clear_review_callback_refusal
        result = {**self.refused_result(), "failure": {"code": "PREVIEW_BUILD_FAILED", "retryable": True},
                  "error_code": "PREVIEW_BUILD_FAILED"}
        cleaned = clear_review_callback_refusal(result, status="awaiting_approval")
        self.assertEqual(cleaned["failure"]["code"], "PREVIEW_BUILD_FAILED")
        self.assertEqual(cleaned["error_code"], "PREVIEW_BUILD_FAILED")
        self.assertNotIn("callback_rejection", cleaned)
