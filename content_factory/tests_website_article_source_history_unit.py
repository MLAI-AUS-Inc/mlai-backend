"""Actual current-purpose outbox/controller; DB and service boundaries only."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from . import website_article_source_history as history, website_article_source_stop as stops
from . import tests_website_article_source_stop_unit as fixtures
from .article_source_history_contract import digest, validate_request, validate_receipt, HistoryContractError
from .website_contract import WebsiteAuthorityError


def worker_receipt(request):
    value = {"schema_version": 1, "kind": "article_source_history_seal", "request_digest": request["digest"],
        "source_fence_id": request["stop_request"]["source_fence_id"],
        "source_run_id": request["stop_request"]["source_fence"]["binding"]["root_run_id"],
        "seal_sha256": "9" * 64, "content_state": "draft_available", "file_count": 1,
        "quiet": False, "effect_authority": False, "approval_carried": False, "sealed_at": "2026-10-18T00:00:00+00:00"}
    value["digest"] = digest(value)
    return validate_receipt(value, request)


@override_settings(**fixtures.ArticleSourceStopTests._overridden_settings)
class ArticleSourceHistoryTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.ArticleSourceStopTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.parent, self.config, self.now = self.fixture.parent, self.fixture.config, self.fixture.now
        self.stack = self.fixture.stack
        stops.advance(self.parent, self.config, now=self.now)
        self.stack.enter_context(patch.object(history, "_operation", side_effect=lambda identifier, **kw:
            next((row for row in self.fixture.fixture.reservations.values() if str(row.pk) == str(identifier) and row.action == history.ACTION), None)))
        self.requests, self.responses = [], []
        self.rpc = self.stack.enter_context(patch.object(history, "read_limited", side_effect=self.worker))

    def worker(self, method, url, *, body, **kw):
        self.assertEqual(self.fixture.fixture.fixture.depth, 0, "No site lock across history HTTP")
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/articles/source-refresh/history-seal"))
        validate_request(body)
        self.requests.append(deepcopy(body))
        result = SimpleNamespace(status_code=200, content=json.dumps(worker_receipt(body)).encode(), close=Mock())
        self.responses.append(result)
        return result

    def query(self, request):
        return {"action": "article_source_history_seal", "operation_id": request["operation_id"], "request_digest": request["digest"],
            "run_id": request["stop_request"]["source_fence"]["binding"]["root_run_id"], "domain": request["stop_request"]["domain"]}

    def test_actual_stop_controller_advances_distinct_history_purpose_under_original_fee(self):
        self.assertTrue(stops.advance(self.parent, self.config, now=self.now + timedelta(seconds=61)))
        self.assertEqual(self.rpc.call_count, 1)
        self.assertEqual(self.fixture.rpc.call_count, 1)
        receipt = self.parent.receipt["article_source_history"]
        self.assertFalse(receipt["quiet"]); self.assertFalse(receipt["effect_authority"]); self.assertFalse(receipt["approval_carried"])
        self.assertEqual(self.fixture.fixture.fixture.chain.fixture.spend.call_count, 1)
        self.assertNotEqual(self.requests[0]["operation_id"], self.parent.receipt[stops.KEY])
        self.assertEqual(self.requests[0]["stop_request"]["source_fence"], self.fixture.fence.payload)

    def test_reservation_reuses_current_read_without_redebit_or_repeat_http(self):
        first = history.advance(self.parent, self.config, now=self.now)
        second = history.advance(self.parent, self.config, now=self.now + timedelta(seconds=61))
        self.assertEqual(first, second); self.assertEqual(self.rpc.call_count, 1)
        self.assertTrue(self.responses[0].close.called)

    def test_actual_private_admission_is_credential_free_and_rechecks_billing(self):
        from .website_views import WebsiteConnectionAuthorizeView
        _, request = history.reserve(self.parent, self.config)
        response = WebsiteConnectionAuthorizeView.as_view()(APIRequestFactory().get("/authorize", self.query(request), HTTP_X_API_KEY="fixture-service-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"allowed": True, "permission_mode": "none", "capabilities": {}, "article_source_history": request})
        with patch("content_factory.website_first_article_billing.for_generation", side_effect=WebsiteAuthorityError("website_operation_changed", "Refund changed")):
            with self.assertRaises(WebsiteAuthorityError): history.admission_receipt(self.query(request))
        self.rpc.assert_not_called()

    def test_owner_stop_source_change_and_rollback_revoke_history(self):
        _, request = history.reserve(self.parent, self.config)
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError): history.admission_receipt(self.query(request))
        self.parent.connection.verified_sha = "e" * 40
        with self.assertRaises(WebsiteAuthorityError): history.admission_receipt(self.query(request))
        self.parent.state = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): history.advance(self.parent, self.config, now=self.now)
        self.rpc.assert_not_called()

    def test_lost_reply_retries_same_request_after_lease_without_redebit(self):
        self.rpc.side_effect = history.ServiceUnavailable("Synthetic lost history reply")
        with self.assertRaises(WebsiteAuthorityError): history.advance(self.parent, self.config, now=self.now)
        row = history._operation(self.parent.receipt[history.KEY])
        saved = deepcopy(row.payload["request"])
        self.assertIsNone(history.advance(self.parent, self.config, now=self.now + timedelta(seconds=10)))
        self.assertEqual(self.rpc.call_count, 1)
        self.rpc.side_effect = self.worker
        history.advance(self.parent, self.config, now=self.now + timedelta(seconds=61))
        self.assertEqual(self.requests, [saved])

    def test_owner_revocation_after_http_cannot_commit_content_receipt(self):
        worker = self.worker
        def revoked(*a, **kw):
            result = worker(*a, **kw)
            self.parent.state = "cancelled"
            return result
        self.rpc.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): history.advance(self.parent, self.config, now=self.now)
        self.assertNotIn("article_source_history", self.parent.receipt)

    def test_private_history_operation_permanently_denied_by_generic_authority(self):
        from .website_operations import validate_operation
        row, _ = history.reserve(self.parent, self.config)
        self.parent.connection.operations.filter.side_effect = lambda **kw: SimpleNamespace(first=lambda: row)
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            for options in ({}, {"cancellation": True}, {"worker_cleanup": True}):
                with self.assertRaises(WebsiteAuthorityError):
                    validate_operation(self.parent.connection, {"operation_id": str(row.pk), "operation_attempt": 1}, **options)

    def test_forged_approval_receipt_refuses_commit(self):
        worker = self.worker
        def forged(*a, **kw):
            result = worker(*a, **kw)
            value = json.loads(result.content); value["approval_carried"] = True
            value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
            result.content = json.dumps(value).encode()
            return result
        self.rpc.side_effect = forged
        with self.assertRaises(WebsiteAuthorityError): history.advance(self.parent, self.config, now=self.now)
        self.assertNotIn("article_source_history", self.parent.receipt)


class ArticleSourceHistoryProducerExamplesTests(SimpleTestCase):
    def test_actual_worker_examples_bind_original_stop_and_have_no_effect_or_approval_grant(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        request = validate_request(json.loads((root / "contracts/article-source-history-v1-request.example.json").read_bytes()))
        receipt = validate_receipt(json.loads((root / "contracts/article-source-history-v1-receipt.example.json").read_bytes()), request)
        original = json.loads((root / "contracts/article-source-stop-v1-request.example.json").read_bytes())
        self.assertEqual(request["stop_request"], original)
        self.assertFalse(receipt["approval_carried"]); self.assertFalse(receipt["effect_authority"]); self.assertFalse(receipt["quiet"])
        self.assertNotIn("synthetic-original-copy-canary", json.dumps(receipt))
        self.assertEqual(receipt["content_state"], "draft_available")
