"""Actual stop reservation/admission/prepare path with only DB/service boundaries mocked."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from . import website_article_source_stop as stop, website_article_source_fence as fences
from . import tests_website_article_source_fence_unit as fixtures
from .article_source_stop_contract import digest, validate_request, validate_receipt, StopContractError
from .website_contract import WebsiteAuthorityError
from . import tests_article_credential_inventory_unit as credential_fixtures


def worker_receipt(request):
    runs = []
    for node in request["source_fence"]["binding"]["runs"]:
        version = node["execution_version"] or [1, 5]
        runs.append({"run_id": node["run_id"], "operation_id": node["operation_id"], "operation_attempt": node["operation_attempt"],
            "origin_generation": version[0], "target_generation": version[0] + 1, "state_version": version[1] + 1,
            "request_sha256": "8" * 64, "activities": "running", "models": "running", "lease": "unknown"})
    value = {"schema_version": 1, "kind": "article_source_stop", "request_digest": request["digest"],
        "source_fence_id": request["source_fence_id"], "source_fence_digest": request["source_fence"]["digest"],
        "local_execution_fenced": True, "quiet": False, "effect_authority": False, "inventory_complete": False,
        "runs": runs, "observed_at": "2026-10-18T00:00:00+00:00"}
    value["digest"] = digest(value)
    return validate_receipt(value, request)


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=True, ROO_API_KEY="fixture-service-key")
class ArticleSourceStopTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.ArticleSourceFenceTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.connection.repository_id = int(self.fixture.connection.repository_id)
        self.fence = self.fixture.record()
        self.fixture.rebound_parent()
        self.parent, self.config, self.now = self.fixture.parent, self.fixture.config, self.fixture.now
        self.stack = self.fixture.fixture.chain.fixture.stack
        self.stack.enter_context(patch.object(stop, "_operation", side_effect=lambda identifier, **kwargs:
            next((row for row in self.fixture.reservations.values() if str(row.pk) == str(identifier) and row.action == stop.ACTION), None)))
        self.rpc = self.stack.enter_context(patch.object(stop, "read_limited", side_effect=self.worker))
        self.responses = []
        self.requests = []
        # ORM/cache/provider seams for the real broker retirement path. Empty
        # legacy history is not a complete-coverage or quiet assertion.
        from . import website_tokens as broker, website_article_credentials as inventory
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from contextlib import nullcontext
        self.credential_rows = credential_fixtures.Operations()
        self.cached = {}
        original_query = WebsiteConnectionOperation.objects.select_for_update()
        def filtered(**fields):
            if fields.get("action") in {inventory.TOKEN_ACTION, inventory.INVENTORY_ACTION, "article-source-publication-read"}:
                return self.credential_rows.filter(**fields)
            return original_query.filter(**fields)
        query = SimpleNamespace(filter=filtered)
        query.select_related = lambda *a: query
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=filtered))
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "create", side_effect=self.credential_rows.create))
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=query))
        self.stack.enter_context(patch.object(WebsiteConnection.objects, "select_for_update", return_value=credential_fixtures.Rows([self.parent.connection])))
        self.stack.enter_context(patch("django.db.transaction.atomic", side_effect=lambda *a, **kw: nullcontext()))
        self.stack.enter_context(patch.object(broker.cache, "get", side_effect=lambda key: self.cached.get(key)))
        self.stack.enter_context(patch.object(broker.cache, "set", side_effect=lambda key, value, **kw: self.cached.__setitem__(key, value)))
        self.stack.enter_context(patch.object(broker.cache, "delete", side_effect=lambda key: self.cached.pop(key, None)))
        self.token_delete = self.stack.enter_context(patch("integrations.http_client.delete", side_effect=AssertionError("No unseeded provider token work")))
        # Empty original mutation inventory is an ORM seam, never a quiet proof.
        self.stack.enter_context(patch("content_factory.website_article_stopped_publications._mutation_rows", return_value=[]))

    def worker(self, method, url, *, body, **kwargs):
        self.assertEqual(self.fixture.fixture.depth, 0, "No site lock over worker HTTP")
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/articles/source-refresh/stop"))
        validate_request(body)
        self.requests.append(deepcopy(body))
        response = SimpleNamespace(status_code=200, content=json.dumps(worker_receipt(body)).encode(), close=Mock())
        self.responses.append(response)
        return response

    def query(self, request):
        return {"action": "article_source_stop", "operation_id": request["operation_id"],
            "run_id": request["source_fence"]["binding"]["root_run_id"], "domain": request["domain"], "request_digest": request["digest"]}

    def test_real_reservation_reuses_one_identity_under_original_spend(self):
        first, request = stop.reserve(self.parent, self.config)
        second, replay = stop.reserve(self.parent, self.config)
        self.assertIs(first, second); self.assertEqual(request, replay)
        self.assertEqual(first.action, stop.ACTION)
        self.assertEqual(request["source_fence"], self.fence.payload)
        self.assertEqual(self.fixture.fixture.chain.fixture.spend.call_count, 1)
        self.rpc.assert_not_called()
        self.assertNotEqual(request["operation_id"], self.fixture.workflow.pk)

    def test_actual_prepare_path_stops_before_journey_or_setup_effects(self):
        from .website_prepare import _advance_claimed_prepare
        with patch("content_factory.website_first_article_billing.reservation_ready", return_value=True), \
                patch("content_factory.website_prepare.journey_for_context", side_effect=AssertionError("No journey before original stop")), \
                patch("content_factory.website_prepare._finish_prepare", return_value=self.parent) as finish:
            result = _advance_claimed_prepare(self.parent, now=self.now, handlers=None)
        self.assertIs(result, self.parent)
        self.assertEqual(self.rpc.call_count, 1)
        self.assertEqual(self.parent.receipt["article_source_stop"], worker_receipt(self.requests[0]))
        self.assertEqual(self.fixture.fixture.chain.fixture.spend.call_count, 1)
        self.assertFalse(self.parent.receipt["article_source_stop"]["quiet"])
        finish.assert_called_once()

    def test_actual_http_admission_has_no_capabilities_and_rechecks_payment(self):
        from .website_views import WebsiteConnectionAuthorizeView
        operation, request = stop.reserve(self.parent, self.config)
        self.assertEqual(stop.admission_receipt(self.query(request)), {"allowed": True, "permission_mode": "none", "capabilities": {},
            "article_source_stop": request})
        response = WebsiteConnectionAuthorizeView.as_view()(APIRequestFactory().get("/authorize", self.query(request), HTTP_X_API_KEY="fixture-service-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["permission_mode"], "none")
        with patch("content_factory.website_first_article_billing.for_generation", side_effect=WebsiteAuthorityError("website_operation_changed", "Original refund changed")):
            with self.assertRaises(WebsiteAuthorityError): stop.admission_receipt(self.query(request))
        self.rpc.assert_not_called()

    def test_owner_stop_and_flag_rollback_refuse_admission_and_no_new_work(self):
        _, request = stop.reserve(self.parent, self.config)
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError): stop.admission_receipt(self.query(request))
            self.assertFalse(stop.advance(self.parent, self.config, now=self.now))
        self.parent.state = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): stop.admission_receipt(self.query(request))
        self.rpc.assert_not_called()

    def test_lost_reply_retry_keeps_identity_and_lease_suppresses_duplicate_http(self):
        self.rpc.side_effect = stop.ServiceUnavailable("Synthetic lost reply")
        with self.assertRaises(WebsiteAuthorityError) as caught: stop.advance(self.parent, self.config, now=self.now)
        self.assertTrue(caught.exception.retryable)
        operation = stop._operation(self.parent.receipt[stop.KEY])
        request = deepcopy(operation.payload["request"])
        self.assertTrue(stop.advance(self.parent, self.config, now=self.now + timedelta(seconds=10)))
        self.assertEqual(self.rpc.call_count, 1)
        self.rpc.side_effect = self.worker
        self.assertTrue(stop.advance(self.parent, self.config, now=self.now + timedelta(seconds=61)))
        self.assertEqual(self.requests, [request])
        self.assertFalse(operation.receipt["remote_confirmed"])
        self.assertNotIn("stop_lease", operation.receipt)

    def test_changed_owner_after_remote_stop_cannot_commit_a_read_or_effect_grant(self):
        original = self.worker
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.parent.state = "cancelled"
            return result
        self.rpc.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): stop.advance(self.parent, self.config, now=self.now)
        self.assertNotIn("article_source_stop", self.parent.receipt)
        self.assertEqual(self.rpc.call_count, 1)

    def test_worker_cannot_claim_quiet_or_enable_replacement(self):
        def forged(*args, **kwargs):
            response = self.worker(*args, **kwargs)
            value = json.loads(response.content)
            value["quiet"] = True
            value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
            response.content = json.dumps(value).encode()
            return response
        self.rpc.side_effect = forged
        with self.assertRaises(WebsiteAuthorityError): stop.advance(self.parent, self.config, now=self.now)
        self.assertNotIn("article_source_stop", self.parent.receipt)
        self.responses[0].close.assert_called_once()

    def test_foreign_query_extra_fields_and_changed_old_request_refuse(self):
        _, request = stop.reserve(self.parent, self.config)
        for changed in ({**self.query(request), "token": "private-canary"}, {**self.query(request), "domain": "foreign.test"}):
            with self.assertRaises(WebsiteAuthorityError) as caught: stop.admission_receipt(changed)
            self.assertNotIn("private-canary", str(caught.exception))
        self.fixture.root.run_request["topic"] = "changed private canary"
        with self.assertRaises(WebsiteAuthorityError): stop.admission_receipt(self.query(request))

    def test_private_stop_action_is_not_a_workflow_execution_capability(self):
        from .website_operations import validate_operation
        operation, request = stop.reserve(self.parent, self.config)
        self.fixture.connection.operations.filter.side_effect = lambda *a, **k: fixtures.rows(operation)
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            for variant in ({}, {"cancellation": True}, {"cancellation_receipt": True}, {"worker_cleanup": True}, {"restoration": True}):
                with self.subTest(variant=variant), self.assertRaises(WebsiteAuthorityError):
                    validate_operation(self.fixture.connection, {"operation_id": str(operation.pk), "operation_attempt": 1}, **variant)

    def test_changed_private_lease_does_not_dispatch_again(self):
        operation, request = stop.reserve(self.parent, self.config)
        operation.receipt = {"stop_lease": {"nonce": "invalid", "expires_at": self.now.isoformat(), "request_digest": request["digest"]}}
        with self.assertRaises(WebsiteAuthorityError): stop.advance(self.parent, self.config, now=self.now)
        self.rpc.assert_not_called()

    def test_spent_goal_retains_a_bounded_cleanup_pass_without_new_work(self):
        with patch("content_factory.website_article_source_history.advance", return_value=None) as seal:
            self.assertTrue(stop.advance(self.parent, self.config, now=self.now + timedelta(hours=49)))
        seal.assert_called_once()
        self.assertEqual(self.rpc.call_count, 1)
        self.assertIn("article_source_stop", self.parent.receipt)
        self.assertEqual(self.parent.state, "pending")
        self.assertEqual(self.parent.receipt["status"], "stopped")
        self.assertEqual(self.parent.receipt["budget_stop_reason"], "wall_time")
        self.assertEqual(self.parent.receipt["article_budget_stop_cleanup"]["state"], "pending")
        self.assertIsNotNone(self.parent.next_attempt_at)
        self.assertFalse(self.parent.receipt["article_source_stop"]["quiet"])

    def test_no_fence_no_effects_or_http(self):
        self.parent.receipt.pop(fences.KEY)
        self.assertFalse(stop.advance(self.parent, self.config, now=self.now))
        self.rpc.assert_not_called()
