"""Database-free article MCP service admission, replay and founder-review tests."""
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from content_factory import mcp_operations as operations
from content_factory.article_publish_approval import make_article_publish_approval_receipt, RECEIPT_KEY


@override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test")
class ArticleMCPServiceTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=7)
        self.company = SimpleNamespace(pk=uuid4(), name="Synthetic startup", profile=SimpleNamespace(user_id=7, role="founder", ROLE_FOUNDER="founder"),
                                       organization=SimpleNamespace(pk=1, id=1, domain="example.test"))
        self.context = operations.context_for(self.user, self.company)
        self.manager = MagicMock()
        self.manager.filter.return_value.order_by.return_value.first.return_value = None
        self.model = SimpleNamespace(objects=self.manager)
        self.args = {"companyId": str(self.company.pk), "topic": "Synthetic topic", "expectedCostPoints": 6,
                     "idempotencyKey": str(uuid4()), "websiteConnection": {"website_connection_id": str(uuid4()), "connection_generation": 2, "connection_target_id": "articles"}}
        self.run = SimpleNamespace(pk=1, run_id="article-1", workflow="article_generation", status="awaiting_approval",
            approval_state="approval_required", run_request={}, organization_id=1, github_repo="fixture/site", result={
                "generation": 0, "livePreview": {"previewUrl": "https://preview.example.test/article-1", "exactRender": True,
                    "resumeGeneration": 0, "proof": {"commitSha": "a" * 40}},
                "article_preview_quality": {"status": "passed", "preview_url": "https://preview.example.test/article-1",
                    "resume_generation": 0, "inputs_sha256": "b" * 64}})
        self.config = SimpleNamespace(github_repo="fixture/site", default_publish_target_id="articles")
        self.dispatch_manager = MagicMock()
        self.dispatch_manager.filter.return_value.first.return_value = None
        dispatch_patch = patch.object(operations, "ContentFactoryDispatchOutbox", SimpleNamespace(objects=self.dispatch_manager))
        dispatch_patch.start()
        self.addCleanup(dispatch_patch.stop)

    def test_nonfounder_and_changed_owner_never_reach_remote(self):
        for field, value in (("user_id", 8), ("role", "investor")):
            with self.subTest(field=field):
                old = getattr(self.company.profile, field)
                setattr(self.company.profile, field, value)
                with patch.object(operations.views, "_get_config") as config, self.assertRaises(PermissionDenied):
                    operations.capabilities(self.user, self.company)
                config.assert_not_called()
                setattr(self.company.profile, field, old)

    def test_foreign_or_nonarticle_run_never_reaches_worker(self):
        self.manager.prefetch_related.return_value.filter.return_value.first.return_value = self.run
        for change in ({"organization_id": 2}, {"workflow": "startup_autofill"}):
            previous = {key: getattr(self.run, key) for key in change}
            for key, value in change.items():
                setattr(self.run, key, value)
            with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations.views, "_call_content_factory_run_status") as remote, self.assertRaises(PermissionDenied):
                operations.run_status(self.user, self.company, "article-1")
            remote.assert_not_called()
            for key, value in previous.items():
                setattr(self.run, key, value)

    def test_generation_uses_shared_service_and_immutable_website_tuple(self):
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "_get_config", return_value=self.config), patch.object(operations, "authority_guard", return_value=nullcontext()) as authority, \
             patch.object(operations, "owner_operation_scope", return_value=nullcontext()), \
             patch.object(operations.views, "start_article_generation", return_value=Response({"runId": "new-1", "status": "queued"}, status=202)) as start:
            result = operations.start(self.user, self.company, self.args)
        self.assertFalse(result["replayed"])
        self.assertEqual(authority.call_args.kwargs["action"], "read")
        payload = start.call_args.kwargs["data"]
        self.assertEqual(payload["expectedCostPoints"], 6)
        self.assertEqual(payload["website_connection_id"], self.args["websiteConnection"]["website_connection_id"])
        self.assertEqual(len(start.call_args.kwargs["request_hash"]), 64)
        self.assertLessEqual(len(payload["clientRequestId"]), 100)

    def test_research_needs_no_website_and_keeps_bounded_quote_and_identity(self):
        value = {"companyId": str(self.company.pk), "topic": "Synthetic", "requestedTopicCount": 3, "expectedCostPoints": 3, "idempotencyKey": str(uuid4())}
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "start_article_research", return_value=Response({"runId": "research-1"}, status=202)) as start, \
             patch.object(operations, "authority_guard") as authority:
            operations.start(self.user, self.company, value, research=True)
        authority.assert_not_called()
        data = start.call_args.kwargs["data"]
        self.assertEqual((data["requestedTopicCount"], data["expectedCostPoints"], data["customTopicTitle"]), (3, 3, "Synthetic"))
        self.assertNotIn("website_connection_id", data)

    def test_paid_transport_retry_returns_durable_run_without_charge_or_dispatch(self):
        self.run.run_request["mcp_request_hash"] = hashlib.sha256(json.dumps(self.args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.manager.filter.return_value.order_by.return_value.first.return_value = self.run
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "_run_start_payload", return_value={"runId": "article-1"}), patch.object(operations.views, "start_article_generation") as start:
            result = operations.start(self.user, self.company, self.args)
        self.assertTrue(result["replayed"])
        start.assert_not_called()

    def test_key_reuse_for_different_payload_is_rejected_before_billing(self):
        self.run.run_request["mcp_request_hash"] = "different"
        self.manager.filter.return_value.order_by.return_value.first.return_value = self.run
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "start_article_generation") as start, self.assertRaises(operations.ArticleOperationError):
            operations.start(self.user, self.company, self.args)
        start.assert_not_called()

    def test_crash_after_charge_reuses_original_outbox_without_new_run_or_analytics(self):
        request_hash = hashlib.sha256(json.dumps(self.args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        dispatch = SimpleNamespace(run_id="", client_request_id="reserved-1", state="pending",
            payload={"request": {"roo_points_requested_by_user_id": self.user.pk, "mcp_request_hash": request_hash, "analytics_article_id": "original-analytics"}})
        self.dispatch_manager.filter.return_value.first.return_value = dispatch
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "start_article_generation") as start:
            result = operations.start(self.user, self.company, self.args)
        self.assertEqual(result["runId"], "reserved-1")
        self.assertTrue(result["dispatchPending"])
        self.assertTrue(result["replayed"])
        start.assert_not_called()

    def test_pending_dispatch_status_is_owner_scoped_and_performs_no_remote_work(self):
        dispatch = SimpleNamespace(run_id="", client_request_id="reserved-1", state="pending",
            payload={"request": {"roo_points_requested_by_user_id": self.user.pk, "mcp_request_hash": "original-hash"}})
        self.dispatch_manager.filter.return_value.first.return_value = dispatch
        with patch.object(operations, "owned_run", side_effect=PermissionDenied("no local run")), \
             patch.object(operations.views, "_call_content_factory_run_status") as remote:
            result = operations.run_status(self.user, self.company, "reserved-1")
        self.assertTrue(result["dispatchPending"])
        remote.assert_not_called()
        dispatch.payload["request"]["roo_points_requested_by_user_id"] = 8
        with patch.object(operations, "owned_run", side_effect=PermissionDenied("no local run")), self.assertRaises(PermissionDenied):
            operations.run_status(self.user, self.company, "reserved-1")

    def test_research_crash_retry_reuses_original_durable_inputs_and_rejects_changed_topic(self):
        arguments = {"companyId": str(self.company.pk), "topic": "Original research", "requestedTopicCount": 3,
                     "expectedCostPoints": 3, "idempotencyKey": str(uuid4())}
        request_hash = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        dispatch = SimpleNamespace(run_id="", client_request_id="research-reserved", state="pending",
            payload={"request": {"roo_points_requested_by_user_id": self.user.pk, "mcp_request_hash": request_hash}})
        self.dispatch_manager.filter.return_value.first.return_value = dispatch
        with patch.object(operations.views, "ContentFactoryRun", self.model), patch.object(operations, "_request_lock", return_value=nullcontext()), \
             patch.object(operations.views, "start_article_research") as start:
            result = operations.start(self.user, self.company, arguments, research=True)
            self.assertTrue(result["replayed"])
            self.assertEqual(result["runId"], "research-reserved")
            expected_key = "vibe-research:" + hashlib.sha256(
                f"{self.user.pk}:{self.company.pk}:{operations._paid_identity(self.user, self.company, arguments['idempotencyKey'], 'research')}".encode()).hexdigest()
            self.assertEqual(self.dispatch_manager.filter.call_args.kwargs["client_request_id"], expected_key)
            with self.assertRaises(operations.ArticleOperationError):
                operations.start(self.user, self.company, {**arguments, "topic": "Changed topic, same price"}, research=True)
        start.assert_not_called()

    def test_price_or_setup_refusal_keeps_canonical_structured_error(self):
        response = Response({"code": "roo_points_quote_changed", "costPoints": 7}, status=409)
        with self.assertRaises(operations.ArticleOperationError) as error:
            operations.result_payload(response)
        self.assertEqual(str(error.exception.detail["code"]), "roo_points_quote_changed")
        self.assertEqual(str(error.exception.detail["costPoints"]), "7")
        self.assertEqual(error.exception.status_code, 409)

    def _approved(self):
        self.run.approval_state = "approved"
        self.run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(self.run, actor_id="founder-7")

    def _publication(self):
        with patch.object(operations, "owned_run", return_value=self.run), patch.object(operations.views, "_get_config", return_value=self.config), \
             patch.object(operations.views, "_latest_review_ready_component_revision", return_value=None), \
             patch.object(operations, "scoped_run_contract", return_value=self.args["websiteConnection"]):
            return operations.publication(self.user, self.company, self.run.run_id)

    def test_prepare_does_not_create_founder_approval_from_preview_quality(self):
        before = deepcopy(self.run.run_request)
        state = self._publication()
        self.assertTrue(state["reviewIdentityComplete"])
        self.assertFalse(state["founderApproved"])
        self.assertEqual(self.run.run_request, before)
        self.assertIn("/my-startup/runs/article-1?company_id=", state["reviewUrl"])
        self.assertIn("articleStep=review", state["reviewUrl"])

    def test_unapproved_or_changed_exact_review_cannot_publish(self):
        for condition in ("unapproved", "changed"):
            if condition == "changed":
                self._approved()
            state = self._publication()
            identity = deepcopy(state["reviewIdentity"])
            if condition == "changed":
                identity["commit_sha"] = "c" * 40
            value = {"runId": "article-1", "reviewIdentity": identity, "destination": state["destination"], "idempotencyKey": str(uuid4())}
            with self.subTest(condition=condition), patch.object(operations, "owned_run", return_value=self.run), \
                 patch.object(operations, "publication", return_value=state), patch.object(operations.views, "control_article_run") as action, \
                 self.assertRaises(operations.ArticleOperationError):
                operations.publish(self.user, self.company, value)
            action.assert_not_called()

    def test_superseded_approved_draft_requires_new_founder_review(self):
        self._approved()
        with patch.object(operations, "owned_run", return_value=self.run), patch.object(operations.views, "_get_config", return_value=self.config), \
             patch.object(operations.views, "_latest_review_ready_component_revision", return_value=SimpleNamespace(run_id="new-1")), \
             patch.object(operations, "scoped_run_contract", return_value=self.args["websiteConnection"]):
            state = operations.publication(self.user, self.company, self.run.run_id)
        self.assertFalse(state["founderApproved"])

    def test_new_publish_target_does_not_inherit_approval_for_another_destination(self):
        self._approved()
        self.config.default_publish_target_id = "different-destination"
        state = self._publication()
        self.assertFalse(state["founderApproved"])
        self.assertEqual(state["approvedPublishTargetId"], "articles")

    def test_changed_destination_or_foreign_approver_never_calls_provider(self):
        self._approved()
        state = self._publication()
        for condition in ("destination", "actor"):
            value = {"runId": "article-1", "reviewIdentity": state["reviewIdentity"], "destination": deepcopy(state["destination"]), "idempotencyKey": str(uuid4())}
            if condition == "destination":
                value["destination"]["githubRepo"] = "foreign/site"
            actor = "foreign-founder" if condition == "actor" else "founder-7"
            with self.subTest(condition=condition), patch.object(operations, "owned_run", return_value=self.run), patch.object(operations, "publication", return_value=state), \
                 patch.object(operations.views, "founder_actor_id_for_user", return_value=actor), patch.object(operations.views, "control_article_run") as action, \
                 self.assertRaises((operations.ArticleOperationError, PermissionDenied)):
                operations.publish(self.user, self.company, value)
            action.assert_not_called()

    def test_approved_publish_only_retries_existing_handoff_never_approves_or_merges(self):
        self._approved()
        state = self._publication()
        value = {"runId": "article-1", "reviewIdentity": state["reviewIdentity"], "destination": state["destination"], "idempotencyKey": str(uuid4())}
        with patch.object(operations, "owned_run", return_value=self.run), patch.object(operations, "publication", return_value=state), \
             patch.object(operations.views, "founder_actor_id_for_user", return_value="founder-7"), \
             patch.object(operations, "authority_guard", return_value=nullcontext()) as guard, \
             patch.object(operations, "owner_operation_scope", return_value=nullcontext()), \
             patch.object(operations, "scoped_run_contract", return_value=self.args["websiteConnection"]), \
             patch.object(operations.views, "control_article_run", return_value=Response({"runId": "publish-1"}, status=202)) as action:
            result = operations.publish(self.user, self.company, value)
        self.assertEqual(result["runId"], "publish-1")
        self.assertEqual(guard.call_args.kwargs["action"], "publish")
        self.assertEqual(action.call_args.kwargs["action"], "publish-pr")
        self.assertIs(action.call_args.kwargs["data"]["auto_merge"], False)
