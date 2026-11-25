"""Isolated receipt and mutation tests. The database and Linear transport are mocked."""
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services import linear_meeting_actions as linear


@override_settings(LINEAR_API_KEY="local-test")
class ProjectUpdateTests(SimpleTestCase):
    def setUp(self):
        self.key = "a" * 64
        self.payload = {"project_id": "p1", "body": "Search is ready to test.", "health": "atRisk"}
        self.input = {"projectId": "p1", "body": self.payload["body"], "health": "atRisk"}
        self.receipt = linear.LinearIssueCreationReceipt(
            pk=1, request_payload=self.input, updated_at=timezone.now()
        )
        self.receipt.save = Mock()
        self.manager = self.enterContext(patch.object(linear.LinearIssueCreationReceipt, "objects"))
        self.enterContext(patch.object(linear.transaction, "atomic", return_value=nullcontext()))
        self.create = self.manager.select_for_update.return_value.get_or_create
        self.create.return_value = (self.receipt, True)
        self.published = {"id": "u1", "url": "https://linear.app/p1/u1", "project": {"id": "p1"}}
        self.write = self.enterContext(patch.object(linear.http_requests, "post"))
        self.write.return_value.status_code = 200
        self.write.return_value.json.return_value = {
            "data": {"projectUpdateCreate": {"success": True, "projectUpdate": self.published}}
        }
        self.history = self.enterContext(patch.object(linear, "get_linear_project_update_page"))
        self.history.return_value = self.page([])

    def page(self, updates):
        return {"project": {"id": "p1", "name": "Search"}, "nodes": updates, "pageInfo": {"hasNextPage": False}}

    def existing(self, **changes):
        return {
            "id": "u1", "url": self.published["url"], "body": self.input["body"],
            "health": "atRisk", "createdAt": self.receipt.updated_at.isoformat(), **changes,
        }

    def call(self, **changes):
        return linear.create_linear_meeting_project_update({**self.payload, "idempotency_key": self.key, **changes})

    def test_new_write_and_legacy_unkeyed_write(self):
        self.assertEqual(self.call(), self.published)
        self.assertRegex(self.create.call_args.kwargs["idempotency_key"], r"^u\.[a-f0-9]{62}$")
        self.manager.filter.return_value.update.assert_called_once()
        self.assertEqual(linear.create_linear_meeting_project_update(self.payload), self.published)
        self.assertEqual(self.write.call_count, 2)
        self.create.assert_called_once()

    def test_explicit_health_and_valid_key_required(self):
        for value in ("", None, "blocked"):
            with self.assertRaisesRegex(ValueError, "health"):
                self.call(health=value)
        for key in ("", None, "not-a-hash"):
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                self.call(idempotency_key=key)
        self.write.assert_not_called()
        self.create.assert_not_called()

    def test_completed_replay_reuses_original_even_if_prose_changed(self):
        self.create.return_value = (self.receipt, False)
        self.receipt.status = self.receipt.Status.COMPLETED
        self.receipt.linear_issue_payload = self.published
        result = self.call(body="New generated wording")
        self.assertEqual(result, {**self.published, "idempotentReplay": True})
        self.write.assert_not_called()

    def test_safe_retry_uses_original_body_and_health(self):
        self.create.return_value = (self.receipt, False)
        self.receipt.status = self.receipt.Status.FAILED
        self.call(body="New generated wording", health="offTrack")
        self.assertEqual(self.write.call_args.kwargs["json"]["variables"]["input"], self.input)
        self.receipt.save.assert_called_once()

    def test_http_timeout_recovers_unique_matching_publication_without_resending(self):
        self.write.side_effect = linear.http_requests.RequestException("timeout")
        self.history.return_value = self.page([self.existing()])
        result = self.call()
        self.assertEqual(result["id"], "u1")
        self.assertTrue(result["idempotentReplay"])
        self.write.assert_called_once()
        self.manager.filter.return_value.update.assert_called_once()

    def test_concurrent_or_stale_pending_retries_do_not_send(self):
        self.create.return_value = (self.receipt, False)
        for history in (
            [],
            [self.existing(body="Different")],
            [self.existing(health="onTrack")],
            [self.existing(createdAt=(self.receipt.updated_at - timedelta(days=1)).isoformat())],
            [self.existing(), self.existing(id="u2")],
        ):
            self.history.return_value = self.page(history)
            with self.assertRaises(linear.LinearMeetingIdempotencyConflictError):
                self.call()
        self.write.assert_not_called()
        self.manager.filter.assert_not_called()

    def test_dubious_response_keeps_receipt_pending(self):
        self.write.return_value.json.return_value = {"data": {}}
        with self.assertRaises(linear.LinearMeetingIdempotencyConflictError):
            self.call()
        self.manager.filter.assert_not_called()

    def test_definitive_rejection_is_retryable(self):
        self.write.return_value.status_code = 429
        self.write.return_value.headers = {"Retry-After": "1"}
        with self.assertRaises(linear.LinearMeetingRateLimitError):
            self.call()
        self.assertEqual(self.manager.filter.return_value.update.call_args.kwargs["status"], "failed")
        self.history.assert_not_called()

    def test_key_cannot_be_replayed_against_another_project(self):
        self.create.return_value = (self.receipt, False)
        with self.assertRaisesRegex(linear.LinearMeetingIdempotencyConflictError, "different project"):
            self.call(project_id="p2")
        self.write.assert_not_called()
