"""Actual stale-article controller and admission, with storage/provider seams."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import httpx
import asyncio
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from . import website_article_refresh as refresh, website_goal as goals, website_setup_consent as consent
from . import website_first_article_billing as billing, website_first_article as article
from . import tests_website_first_article_unit as fixtures
from .article_source_refresh_contract import digest, validate_request
from .website_contract import WebsiteAuthorityError


class ArticleRefreshProvenanceTests(SimpleTestCase):
    def setUp(self):
        from scripts import check_article_source_refresh_contract as checker
        self.checker = checker
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.producer = {}
        rows = []
        for source, local in checker.PAIRS.items():
            raw = (checker.ROOT / local).read_bytes()
            path = self.root / local
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            self.producer[source] = raw
            rows.append({"source_path": source, "local_path": local, "sha256": hashlib.sha256(raw).hexdigest()})
        self.lock = {"schema_version": 1, "source_repository": checker.SOURCE_REPOSITORY,
            "source_revision": "a" * 40, "files": rows}
        self.save_lock()

    def save_lock(self):
        (self.root / "contracts/article-source-refresh-v1.lock.json").write_text(json.dumps(self.lock))

    def test_remote_verification_fetches_only_exact_allowlisted_producer_bytes(self):
        def producer(path, revision, token):
            self.assertEqual(revision, self.lock["source_revision"])
            self.assertEqual(token, "synthetic-read-only-token")
            return self.producer[path]
        fetch = Mock(side_effect=producer)
        self.checker.verify(self.root, remote=True, token="synthetic-read-only-token", fetch=fetch)
        self.assertEqual(fetch.call_count, len(self.checker.PAIRS))
        with self.assertRaises(self.checker.ContractCheckError):
            self.checker.verify(self.root, remote=True, token="synthetic-read-only-token", fetch=lambda *args: b"foreign bytes")

    def test_missing_read_token_and_foreign_path_never_fetch_private_source(self):
        fetch = Mock()
        with self.assertRaises(self.checker.ContractCheckError):
            self.checker.verify(self.root, remote=True, token=None, fetch=fetch)
        self.lock["files"][0]["source_path"] = "backend/unrelated.py"
        self.save_lock()
        with self.assertRaises(self.checker.ContractCheckError):
            self.checker.verify(self.root, remote=True, token="synthetic", fetch=fetch)
        fetch.assert_not_called()

    def test_changed_local_contract_fails_before_private_fetch(self):
        (self.root / self.lock["files"][0]["local_path"]).write_bytes(b"changed bytes")
        fetch = Mock()
        with self.assertRaises(self.checker.ContractCheckError):
            self.checker.verify(self.root, remote=True, token="synthetic", fetch=fetch)
        fetch.assert_not_called()


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, ROO_API_KEY="fixture-service-key")
class ArticleRefreshCoordinatorTests(SimpleTestCase):
    def setUp(self):
        self.source = fixtures.ArticleSourceRefreshIntentTests("runTest")
        self.source.setUp(); self.addCleanup(self.source.doCleanups)
        self.chain = self.source.chain
        self.parent, self.run, self.now = self.chain.op, self.chain.run, self.chain.now
        self.connection = self.chain.connection
        self.parent.pk, self.connection.pk = uuid4(), uuid4()
        self.parent.connection_id = self.connection.pk
        self.chain.config.website_connection_id = self.connection.pk
        answers = deepcopy(self.parent.receipt["goal"]["consent"]["answers"])
        goal = goals.new_goal(str(self.parent.pk), goals.scope_for(self.connection, 9), target="first_article_live", now=self.now)
        goal["consent"] = consent.seal(goal, answers, user_id=4, now=self.now)
        goal["current_source_sha"] = self.connection.verified_sha
        self.parent.receipt = {"goal": goal, "first_article_run_id": self.run.run_id, "current_step": "first-article"}
        key = billing.request_key(goal)
        ledger = self.chain.fixture.ledger
        ledger.reference_id, ledger.idempotency_key = key, "content_factory:charge:" + key
        self.chain.fixture.spend.reset_mock()
        held = self.chain.fixture.reserve()
        self.run.run_request.update(client_request_id=held["client_request_id"], website_connection_id=str(self.connection.pk),
            first_article_goal_id=str(self.parent.pk), first_article_goal_operation_id=str(self.parent.pk))
        self.chain.workflow.payload["first_article_goal_operation_id"] = str(self.parent.pk)
        self.parent._claimed_goal_revision = self.parent.receipt["goal"]["revision"]
        self.operations, self.depth = {}, 0
        stack = self.chain.fixture.stack
        stack.enter_context(patch("content_factory.website_connections.authority_guard", self.guard))
        stack.enter_context(patch("content_factory.website_models.WebsiteConnectionOperation.objects.get_or_create", side_effect=self.reserve_operation))
        stack.enter_context(patch.object(refresh, "_inspection_operation", side_effect=self.operation))
        self.context = stack.enter_context(patch("content_factory.website_prepare._prepare_context",
            return_value=(self.chain.context, self.chain.config, self.chain.fixture.user)))
        stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_remote_config",
            return_value={"enabled": True, "base_url": "https://worker.test"}))
        stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "synthetic-service-key"}))
        self.post = stack.enter_context(patch.object(refresh, "read_limited", side_effect=self.worker))
        self.publication_observer = stack.enter_context(patch(
            "content_factory.website_article_publication_observation.advance", return_value=True))
        self.connection.operations.filter.side_effect = self.operation_rows

    @contextmanager
    def guard(self, *args, **kwargs):
        self.depth += 1
        try: yield self.connection
        finally: self.depth -= 1

    def reserve_operation(self, *, idempotency_key, defaults):
        for operation in self.operations.values():
            if operation.idempotency_key == idempotency_key: return operation, False
        operation = SimpleNamespace(pk=uuid4(), connection=self.connection, connection_id=self.connection.pk,
            generation=self.connection.generation, action="workflow", state="running", payload=deepcopy(defaults["payload"]),
            receipt={}, idempotency_key=idempotency_key, save=Mock())
        self.operations[str(operation.pk)] = operation
        return operation, True

    def operation(self, identifier, *, lock=False):
        return self.operations.get(str(identifier))

    def operation_rows(self, *args, **kwargs):
        if str(kwargs.get("pk")) in self.operations: return fixtures.rows(self.operations[str(kwargs["pk"])] )
        return self.source.operation_rows(*args, **kwargs)

    def query(self, request):
        return {"action": "article_source_refresh_inspect", "operation_id": request["operation_id"],
            "run_id": request["intent"]["proposed_run_id"], "request_digest": request["digest"], "domain": request["domain"]}

    def receipt(self, request):
        root = request["intent"]["binding"]["paid_root"]
        value = {"schema_version": 1, "kind": "article_source_refresh_inspection", "request_digest": request["digest"],
            "intent_id": request["intent"]["id"], "source_run_id": root["run_id"], "source_generation": root["generation"],
            "source_state_version": max(root["state_version"], self.run.result["state_version"]) + 1, "source_status": "completed", "inputs_sha256": "e" * 64,
            "review_inputs_sha256": None, "content_state": "draft_available", "superseded_by_run_id": None, "publications": [], "publication_refs": [],
            "publication_children": [], "observed_at": self.now.isoformat()}
        value["digest"] = digest(value)
        return value

    def worker(self, method, url, *, body, **kwargs):
        self.assertEqual(self.depth, 0, "No remote worker call while holding authority locks")
        self.assertEqual(url, "https://worker.test/api/articles/source-refresh/inspect")
        self.assertEqual(method, "POST"); self.assertEqual(kwargs["deadline"], 45)
        request = validate_request(body)
        first = refresh.admission_receipt(self.query(request))
        second = refresh.admission_receipt(self.query(request))
        self.assertEqual(first, second)
        self.assertEqual(first["permission_mode"], "none"); self.assertEqual(first["capabilities"], {})
        return httpx.Response(200, json=self.receipt(request))

    def advance(self):
        self.parent.state = "running"
        self.chain.now = self.now
        return self.chain.advance()

    def test_real_first_article_controller_journals_and_inspects_stale_paid_root_without_generation(self):
        from . import vibe_marketing_views as views
        with patch.object(views, "start_article_generation") as generate, patch.object(views, "control_article_run") as control:
            self.advance()
            original = deepcopy(self.parent.receipt["article_source_refresh"])
            self.advance()
        self.assertEqual(self.parent.receipt["article_source_refresh"], original)
        self.assertEqual(self.parent.receipt["article_source_refresh_stage"], "inspected")
        self.assertEqual(len(self.operations), 1)
        operation = next(iter(self.operations.values()))
        self.assertEqual(operation.state, "completed")
        self.assertNotIn("article_delivery_v1", operation.payload)
        self.assertEqual(self.chain.fixture.spend.call_count, 1)
        generate.assert_not_called(); control.assert_not_called()

    def test_admission_view_checks_service_auth_and_returns_no_token_or_execution_capability(self):
        from .website_views import WebsiteConnectionAuthorizeView
        operation, request = refresh.reserve_inspection(self.parent, self.chain.context, self.chain.config,
            user=self.chain.fixture.user, now=self.now)
        factory = APIRequestFactory()
        denied = WebsiteConnectionAuthorizeView.as_view()(factory.get("/authorize", self.query(request)))
        self.assertEqual(denied.status_code, 403)
        response = WebsiteConnectionAuthorizeView.as_view()(factory.get("/authorize", self.query(request), HTTP_X_API_KEY="fixture-service-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"allowed": True, "permission_mode": "none", "capabilities": {}, "article_source_refresh": request})
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_inspection_reservation_cannot_enter_generic_token_write_merge_or_cleanup_paths(self):
        from .website_operations import validate_operation
        operation, request = refresh.reserve_inspection(self.parent, self.chain.context, self.chain.config,
            user=self.chain.fixture.user, now=self.now)
        for action in ("read", "publish", "merge", "setup", "cleanup"):
            with self.subTest(action=action), self.assertRaises(WebsiteAuthorityError):
                validate_operation(self.connection, {"operation_id": str(operation.pk), "operation_attempt": 1, "action": action})
        # A renamed workflow or malformed marker cannot turn a read reservation
        # into a generic token capability during a future protocol upgrade.
        operation.payload["workflow"] = "article_generation"
        operation.payload[refresh.READ_ONLY_KEY] = False
        with self.assertRaises(WebsiteAuthorityError):
            validate_operation(self.connection, {"operation_id": str(operation.pk), "operation_attempt": 1})

    def test_lost_inspection_response_reuses_the_same_operation_and_proposed_uuid(self):
        def lost(*args, **kwargs):
            self.worker(*args, **kwargs)
            raise refresh.ServiceUnavailable("private-worker-canary")
        self.post.side_effect = lost
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-worker-canary", str(caught.exception))
        original = deepcopy(self.parent.receipt["article_source_refresh"])
        self.post.side_effect = self.worker
        self.advance()
        self.assertEqual(self.post.call_count, 1, "An uncertain read keeps its pending lease")
        self.now += refresh.READ_LEASE + timedelta(seconds=1)
        self.advance()
        self.assertEqual(self.post.call_count, 2, "The expired read lease must actually retry")
        self.assertEqual(len(self.operations), 1)
        self.assertEqual(self.parent.receipt["article_source_refresh"], original)
        self.assertEqual(self.chain.fixture.spend.call_count, 1)

    def test_owner_cancel_after_worker_read_cannot_commit_observation(self):
        def cancelled(*args, **kwargs):
            result = self.worker(*args, **kwargs)
            self.parent.state = "cancelled"
            return result
        self.post.side_effect = cancelled
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn("article_source_refresh_inspection", self.parent.receipt)

    def test_stale_parent_lease_after_read_cannot_commit_observation(self):
        def expired(*args, **kwargs):
            result = self.worker(*args, **kwargs)
            self.chain.fixture.rows.return_value.filter.return_value.first.return_value = None
            return result
        self.post.side_effect = expired
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn("article_source_refresh_inspection", self.parent.receipt)

    def test_503_remains_transient_and_does_not_become_a_contract_denial(self):
        response = Mock(status_code=503, close=Mock())
        self.post.return_value = response; self.post.side_effect = None
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertTrue(caught.exception.retryable)
        response.close.assert_called_once()

    def test_disabled_rollout_never_dispatches_inspection(self):
        with override_settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            self.assertFalse(refresh.advance(self.parent, self.chain.context, self.chain.config, user=self.chain.fixture.user, now=self.now))
        self.post.assert_not_called(); self.assertEqual(len(self.operations), 0)

    def test_current_owned_merge_does_not_start_a_source_refresh(self):
        self.chain.approve(); self.chain.publish_child(merged=True)
        self.parent.receipt["goal"]["current_source_sha"] = self.connection.verified_sha = "e" * 40
        self.assertFalse(refresh.advance(self.parent, self.chain.context, self.chain.config, user=self.chain.fixture.user, now=self.now))
        self.post.assert_not_called()

    def test_later_usage_observations_do_not_mint_another_inspection(self):
        self.advance()
        original = deepcopy(self.parent.receipt["article_source_refresh"])
        self.run.result["state_version"] += 2
        self.advance()
        self.assertEqual(self.parent.receipt["article_source_refresh"], original)
        self.assertEqual(len(self.operations), 1)

    def test_owner_review_wait_can_be_inspected_without_granting_approval(self):
        self.run.status = "awaiting_approval"
        self.advance()
        self.assertEqual(self.parent.receipt["article_source_refresh_stage"], "inspected")
        self.assertNotIn("first_article_approval", self.parent.receipt)


class ArticleRefreshWireTests(SimpleTestCase):
    def test_actual_producer_examples_validate_without_inventing_execution_authority(self):
        from .article_source_refresh_contract import validate_inspection
        root = Path(__file__).resolve().parents[1] / "contracts"
        request = validate_request(json.loads((root / "article-source-refresh-v1-request.example.json").read_text()))
        receipt = validate_inspection(json.loads((root / "article-source-refresh-v1-inspection.example.json").read_text()), request)
        self.assertEqual(receipt["publication_refs"][0]["number"], 43)
        self.assertEqual(receipt["publication_children"][0]["status"], "queued")
        self.assertNotIn("approved", receipt)

    def test_actual_bounded_httpx_transport_returns_only_detached_metadata(self):
        from . import bounded_service_read as limited
        original = httpx.AsyncClient
        async def peer(request):
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "private-cookie-canary"})
        with patch.object(limited.httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(peer), **kwargs)):
            response = limited.read_limited("GET", "https://metadata.test/read", headers={}, maximum=1024, deadline=1)
        self.assertEqual(response.json(), {"ok": True})
        self.assertTrue(response.is_closed)
        self.assertNotIn("set-cookie", response.headers)

    def test_backend_transport_wall_clock_deadline_closes_a_dribbling_body(self):
        from . import bounded_service_read as limited
        original = httpx.AsyncClient
        class Slow(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.02)
                    yield b"x"
            async def aclose(self): self.closed = True
        stream = Slow()
        async def peer(request):
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
        with patch.object(limited.httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(peer), **kwargs)):
            with self.assertRaises(limited.ServiceUnavailable):
                limited.read_limited("GET", "https://metadata.test/read", headers={}, maximum=1024, deadline=0.03)
        self.assertTrue(stream.closed)
