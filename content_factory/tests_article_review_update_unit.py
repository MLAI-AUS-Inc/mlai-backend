"""Review updates use synthetic storage and service seams; no DB or network."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.core import signing
from django.http import HttpResponse
from rest_framework.test import APIRequestFactory
from rest_framework.response import Response

from . import article_review_views as review
from . import article_preview_lease as preview
from . import article_review_callbacks as callbacks


class CommentQuery(list):
    def order_by(self, *args):
        return self

    def update(self, **changes):
        for comment in self:
            for key, value in changes.items():
                setattr(comment, key, value)


class Comments:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def select_for_update(self):
        return self

    def filter(self, **conditions):
        self.queries.append(conditions)
        return CommentQuery(row for row in self.rows if all(
            str(row.id) in {str(value) for value in values} if key == "id__in"
            else getattr(row, key) == values for key, values in conditions.items()
        ))


class ArticleReviewUpdateTests(SimpleTestCase):
    def setUp(self):
        self.org = SimpleNamespace(pk=7, domain="owned.test")
        self.context = SimpleNamespace(organization=self.org)
        self.run = SimpleNamespace(pk=1, run_id="setup-1", workflow="article_system_setup",
            domain=self.org.domain, github_repo="fixture/site", result={}, run_request={}, save=Mock())
        self.comment = SimpleNamespace(id=uuid4(), run=self.run, status="draft", batch_id="",
            component_id="hero", component_type="section", component_label="Hero",
            context={}, anchor={}, selector="[data-cf-component-id=hero]", body="Make it welcoming")
        self.storage = Comments([self.comment])
        self.view = review.VibeMarketingArticleReviewView()
        self.view._resolve_run = Mock(return_value=(self.context, self.run, None))
        self.snapshot = {"articleId": self.run.run_id, "revision": "after", "fields": [], "previewPending": True}
        for target, value in (
            ("remote_review", self.snapshot),
            ("_review_dispatch_contract", {}),
            ("views._run_has_external_publish_evidence", False),
            ("views._latest_review_ready_component_revision", None),
            ("_revision_authorization", None),
            ("views._create_local_run", SimpleNamespace(run_id="child")),
            ("views._create_editorial_feedback_candidates", None),
        ):
            context = patch.object(review, target, return_value=value) if "." not in target else patch(
                "content_factory.article_review_views." + target, return_value=value,
            )
            result = context.start()
            self.addCleanup(context.stop)
            if target == "remote_review":
                self.remote = result
            if target == "_revision_authorization":
                self.authorization = result
        for context in (
            patch.object(review.views.VibeMarketingComponentComment, "objects", self.storage),
            patch.object(review.transaction, "atomic", nullcontext),
            patch.object(review.views.ContentFactoryRun.objects, "select_for_update"),
        ):
            result = context.start()
            self.addCleanup(context.stop)
            if hasattr(result, 'get'):
                result.return_value.get.return_value = self.run
                self.locked_runs = result.return_value

    def body(self, **extra):
        return {"action": "applyUpdate", "operationId": "operation-1", "expectedRevision": "before",
                "textEdits": [{"fieldId": "field-1", "originalValue": "Old", "value": " New\ntext "}],
                "commentIds": [], **extra}

    def post(self, body=None):
        return self.view.post(SimpleNamespace(data=body or self.body(), user=SimpleNamespace(pk=9)), self.run.run_id)

    def test_foreign_run_cannot_reach_remote_or_comments(self):
        self.view._resolve_run.return_value = (None, None, Response({"detail": "Not found"}, status=404))
        self.assertEqual(self.post().status_code, 404)
        self.remote.assert_not_called()
        self.assertEqual(self.storage.queries, [])

    def test_foreign_comment_never_reaches_worker(self):
        result = self.post(self.body(commentIds=[str(uuid4())]))
        self.assertEqual(result.status_code, 409)
        self.remote.assert_not_called()
        self.assertEqual(self.comment.status, "draft")
        self.assertTrue(all(query["run"] is self.run for query in self.storage.queries))

    def test_text_only_preserves_exact_input_and_has_no_comment_query(self):
        result = self.post()
        self.assertEqual(result.status_code, 202)
        payload = self.remote.call_args.kwargs["payload"]
        self.assertEqual(payload["textEdits"], self.body()["textEdits"])
        self.assertEqual(payload["comments"], [])
        self.assertEqual(self.storage.queries, [])
        self.assertEqual(self.run.result["article_review_updates"]["operation-1"]["status"], "accepted")

    def test_combined_update_uses_saved_comment_body_and_claims_same_run_only(self):
        result = self.post(self.body(commentIds=[str(self.comment.id)], comments=[{"body": "Forged"}]))
        self.assertEqual(result.status_code, 202)
        payload = self.remote.call_args.kwargs["payload"]
        self.assertEqual(payload["comments"][0]["body"], self.comment.body)
        self.assertEqual(payload["comments"][0]["component_id"], "hero")
        self.assertEqual(payload["feedback_batch_id"], "operation-1")
        self.assertEqual(payload["textEdits"], self.body()["textEdits"])
        self.assertEqual(self.comment.status, "submitted")
        self.assertEqual(self.comment.batch_id, "operation-1")

    def test_definitive_conflict_restores_only_newly_claimed_comments(self):
        self.remote.return_value = Response({"detail": "Draft changed"}, status=409)
        result = self.post(self.body(commentIds=[str(self.comment.id)]))
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.comment.status, "draft")
        self.assertEqual(self.comment.batch_id, "")
        self.assertEqual(self.run.result, {})

    def test_uncertain_submission_keeps_comment_batch_and_retry_identity(self):
        body = self.body(commentIds=[str(self.comment.id)])
        self.remote.return_value = Response({"detail": "Uncertain", "retryable": True}, status=502)
        self.assertEqual(self.post(body).status_code, 502)
        self.assertEqual(self.comment.status, "submitted")
        self.assertEqual(self.comment.batch_id, body["operationId"])
        self.remote.return_value = self.snapshot
        self.assertEqual(self.post(body).status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["payload"]["feedback_batch_id"], body["operationId"])

    def test_uncertain_dispatch_then_definitive_retry_rejection_restores_selected_drafts(self):
        other = SimpleNamespace(id=uuid4(), run=self.run, status="submitted", batch_id="other-operation")
        self.storage.rows.append(other)
        body = self.body(commentIds=[str(self.comment.id)])
        self.remote.return_value = Response({"detail": "Timeout"}, status=502)
        self.assertEqual(self.post(body).status_code, 502)
        self.assertEqual(self.comment.status, "submitted")
        self.remote.return_value = Response({"detail": "Source changed"}, status=409)
        self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual((self.comment.status, self.comment.batch_id), ("draft", ""))
        self.assertEqual((other.status, other.batch_id), ("submitted", "other-operation"))
        self.assertNotIn("article_review_updates", self.run.result)
        self.assertNotIn("component_feedback_latest_batch", self.run.result)

    def test_dispatch_is_recorded_first_and_retry_uses_frozen_comment_context(self):
        body = self.body(commentIds=[str(self.comment.id)])
        captured = []
        def dispatch(run, *, payload):
            ledger = run.result["article_review_updates"]["operation-1"]
            self.assertEqual(ledger["status"], "submitted")
            self.assertEqual(run.result["component_feedback_latest_batch"]["status"], "submitted")
            captured.append(payload["comments"])
            self.comment.context = {"reviewOutcome": {"status": "addressed"}}
            return Response({"detail": "Response lost"}, status=502)
        self.remote.side_effect = dispatch
        self.assertEqual(self.post(body).status_code, 502)
        self.remote.side_effect = None
        self.remote.return_value = self.snapshot
        self.assertEqual(self.post(body).status_code, 202)
        self.assertEqual(self.remote.call_args.kwargs["payload"]["comments"], captured[0])
        self.assertNotIn("reviewOutcome", self.remote.call_args.kwargs["payload"]["comments"][0].get("context", {}))

    def test_accepted_retry_reads_snapshot_without_resubmitting_changes(self):
        body = self.body()
        self.post(body)
        self.remote.reset_mock()
        self.assertEqual(self.post(body).status_code, 200)
        self.remote.assert_called_once_with(self.run)
        self.assertEqual(self.post({**body, "expectedRevision": "after"}).status_code, 200)
        self.assertEqual(self.post({**body, "textEdits": [{"fieldId": "field-1", "value": "Different", "originalValue": "Old"}]}).status_code, 409)

    def test_accepted_retry_precedes_newer_review_ready_child(self):
        body = self.body(commentIds=[str(self.comment.id)])
        self.remote.return_value = {**self.snapshot, "revisionRunId": "setup-child"}
        self.post(body)
        self.remote.reset_mock()
        with patch.object(review.views, "_latest_review_ready_component_revision",
                          return_value=SimpleNamespace(run_id="setup-child")):
            response = self.post(body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["revisionRunId"], "setup-child")
            self.remote.assert_called_once_with(self.run)
            self.assertEqual(self.post({**body, "operationId": "different-operation"}).status_code, 409)

    def test_comment_already_in_other_batch_rejected(self):
        self.comment.status = "submitted"
        self.comment.batch_id = "other-operation"
        self.assertEqual(self.post(self.body(commentIds=[str(self.comment.id)])).status_code, 409)
        self.remote.assert_not_called()

    def test_authorization_denial_keeps_drafts_and_input(self):
        self.authorization.return_value = Response({"detail": "Insufficient points"}, status=402)
        self.assertEqual(self.post(self.body(commentIds=[str(self.comment.id)])).status_code, 402)
        self.assertEqual(self.comment.status, "draft")
        self.assertEqual(self.storage.queries, [])
        self.remote.assert_not_called()

    def test_child_revision_is_locally_resolvable(self):
        self.remote.return_value = {**self.snapshot, "revisionRunId": "setup-child"}
        self.assertEqual(self.post(self.body(commentIds=[str(self.comment.id)])).status_code, 202)
        review.views._create_local_run.assert_called_once()
        self.assertEqual(review.views._create_local_run.call_args.kwargs["remote_data"]["run_id"], "setup-child")
        self.assertEqual(review.views._create_local_run.call_args.kwargs["workflow"], "article_system_setup")
        self.assertTrue(review.views._create_local_run.call_args.kwargs["preserve_existing"])

    def test_completed_merged_setup_can_start_new_combined_revision(self):
        self.run.status = "completed"
        self.run.result = {"pr_url": "https://github.invalid/fixture/site/pull/1", "merged": True}
        self.remote.return_value = {**self.snapshot, "revisionRunId": "setup-child"}
        with patch.object(review.views, "_run_has_external_publish_evidence", return_value=True):
            self.assertEqual(self.post(self.body(commentIds=[str(self.comment.id)])).status_code, 202)
            self.assertEqual(self.post({"action": "editText"}).status_code, 409)

    def test_dispatch_metadata_merges_fresh_callback_and_other_operation(self):
        fresh = SimpleNamespace(result={"callbackProof": {"sha": "new"},
            "article_review_updates": {"another-operation": {"status": "accepted"}},
            "component_feedback_latest_batch": {"id": "operation-1", "status": "completed",
                "revisionRunId": "setup-child", "outcomes": [{"status": "addressed"}]}}, save=Mock())
        self.locked_runs.get.return_value = fresh
        self.remote.return_value = {**self.snapshot, "revisionRunId": "setup-child"}
        self.assertEqual(self.post(self.body(commentIds=[str(self.comment.id)])).status_code, 202)
        self.assertEqual(fresh.result["callbackProof"], {"sha": "new"})
        self.assertIn("another-operation", fresh.result["article_review_updates"])
        self.assertEqual(fresh.result["component_feedback_latest_batch"]["status"], "completed")
        self.assertEqual(fresh.result["component_feedback_latest_batch"]["outcomes"], [{"status": "addressed"}])
        self.run.save.assert_not_called()
        self.assertEqual(fresh.save.call_count, 2)
        self.assertTrue(all(call.kwargs == {"update_fields": ["result", "updated_at"]}
                            for call in fresh.save.call_args_list))

    def test_uncertain_retry_cannot_downgrade_fresh_accepted_operation(self):
        body = self.body()
        request_hash = review._fingerprint({key: value for key, value in review.normalize_review_update(body).items()
                                           if key != "expectedRevision"})
        fresh = SimpleNamespace(result={"article_review_updates": {"operation-1": {
            "requestHash": request_hash, "status": "accepted", "revisionRunId": "setup-child"}}}, save=Mock())
        self.locked_runs.get.return_value = fresh
        self.remote.return_value = Response({"detail": "Timeout"}, status=502)
        self.assertEqual(self.post(body).status_code, 502)
        self.assertEqual(fresh.result["article_review_updates"]["operation-1"]["status"], "accepted")
        self.assertEqual(fresh.result["article_review_updates"]["operation-1"]["revisionRunId"], "setup-child")

    def test_setup_get_returns_fields_and_private_cache(self):
        with patch.object(review.views, "_component_feedback_from_run", return_value={"comments": []}):
            response = self.view.get(SimpleNamespace(), self.run.run_id)
        self.assertEqual(response.data["revision"], "after")
        self.assertEqual(response.data["fields"], [])
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_restore_only_update_is_a_stable_revision_instruction(self):
        self.run.workflow = "article_generation"
        body = {"action": "applyUpdate", "operationId": "restore-operation", "expectedRevision": "before",
                "restoredSentences": ["An unsupported sentence.", "An unsupported sentence."]}
        request = SimpleNamespace(data=body, user=SimpleNamespace(pk=2))
        response = self.view.post(request, self.run.run_id)
        self.assertEqual(response.status_code, 202)
        payload = self.remote.call_args.kwargs["payload"]
        self.assertEqual(len(payload["comments"]), 1)
        self.assertIn("recheck authoritative sources and safety", payload["comments"][0]["body"])
        self.assertEqual(payload["comments"][0]["context"]["restoredSentence"], "An unsupported sentence.")
        self.assertNotEqual(review._fingerprint(review.normalize_review_update(body)),
            review._fingerprint(review.normalize_review_update({**body, "restoredSentences": ["Changed sentence."]})))

    def test_unsafe_batch_shapes_never_dispatch(self):
        malformed = [self.body(commentIds=["bad-id"]), self.body(textEdits=[{}, {}]),
            self.body(textEdits=[self.body()["textEdits"][0]] * 2),
            self.body(operationId="x"), self.body(textEdits=[], commentIds=[])]
        for body in malformed:
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 400)
        self.remote.assert_not_called()


class ArticleReviewRemoteTests(SimpleTestCase):
    def setUp(self):
        for context in (patch.object(review.views, "scoped_run_contract", return_value={"delivery_mode": "content_only"}),
            patch.object(review.views, "owner_write_guard", side_effect=lambda *args, **kwargs: nullcontext())):
            context.start()
            self.addCleanup(context.stop)

    def test_registering_review_child_keeps_fast_callback_readiness_and_proof(self):
        child = SimpleNamespace(run_id="setup-child", workflow="article_system_setup", domain="owned.test",
            github_repo="fixture/site", slack_user_id="actor", run_request={"source_run_id": "source"},
            status="completed", current_step="await_review", result={"previewProof": {"sha": "new"}},
            error="", save=Mock())
        with patch.object(review.views.ContentFactoryRun.objects, "get_or_create", return_value=(child, False)), \
             patch.object(review.views, "_persist_web_article_billing_to_job"), \
             patch.object(review.views, "_merge_job_billing_into_run_request"):
            review.views._create_local_run(workflow="article_system_setup", domain="owned.test",
                remote_data={"run_id": child.run_id, "status": "queued"}, preserve_existing=True)
        self.assertEqual(child.status, "completed")
        self.assertEqual(child.current_step, "await_review")
        self.assertEqual(child.result, {"previewProof": {"sha": "new"}})
        self.assertNotIn("result", child.save.call_args.kwargs["update_fields"])
        self.assertNotIn("status", child.save.call_args.kwargs["update_fields"])

    @patch.object(review.views, "_content_factory_headers", return_value={"X-API-Key": "synthetic"})
    @patch.object(review.views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://factory.invalid"})
    @patch.object(review.views.http_client, "request")
    def test_worker_conflict_preserves_status_and_service_auth(self, request, config, headers):
        request.return_value.status_code = 409
        request.return_value.json.return_value = {"detail": "Changed", "revision": "current"}
        response = review.remote_review(SimpleNamespace(run_id="setup-1"), payload={"action": "applyUpdate"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["revision"], "current")
        self.assertEqual(request.call_args.kwargs["headers"], {"X-API-Key": "synthetic"})

    def test_text_only_authorization_does_not_call_ai_gate(self):
        with patch.object(review.views, "_require_roo_points_for_ai_agent") as gate:
            self.assertIsNone(review._revision_authorization(None, None, None, {"commentIds": []}))
        gate.assert_not_called()






class SetupReviewCallbackTests(SimpleTestCase):
    def setUp(self):
        self.source = SimpleNamespace(pk=1, run_id="merged-setup", workflow="article_system_setup",
            domain="owned.test", github_repo="fixture/site", status="completed", approval_state="approved",
            result={"merged": True, "pr_url": "https://github.invalid/pull/1"}, save=Mock())
        self.child = SimpleNamespace(pk=2, run_id="setup-child", workflow="article_system_setup",
            domain="owned.test", github_repo="fixture/site", result={"previewProof": "new"}, save=Mock())
        self.comment = SimpleNamespace(id=uuid4(), run=self.source, status="submitted", batch_id="operation-1",
            body="Change the heading", context={"domPath": "body > main"}, save=Mock())
        self.storage = Comments([self.comment])
        self.runs = Mock()
        self.runs.filter.return_value.first.return_value = self.source
        self.runs.get.return_value = self.child
        for context in (patch.object(callbacks.transaction, "atomic", nullcontext),
            patch.object(callbacks.ContentFactoryRun.objects, "select_for_update", return_value=self.runs),
            patch.object(callbacks.VibeMarketingComponentComment, "objects", self.storage)):
            context.start()
            self.addCleanup(context.stop)
        self.data = {"review_update_operation_id": "operation-1", "feedback_batch_id": "operation-1",
            "source_setup_run_id": self.source.run_id,
            "comment_outcomes": [{"commentId": str(self.comment.id), "status": "addressed", "summary": "Heading regenerated"}]}

    def test_outcomes_are_saved_without_changing_completed_published_source(self):
        self.source.result["article_review_updates"] = {"operation-1": {"status": "submitted", "requestHash": "hash"}}
        self.assertTrue(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.assertEqual((self.source.status, self.source.approval_state), ("completed", "approved"))
        self.assertTrue(self.source.result["merged"])
        self.assertEqual(self.source.result["pr_url"], "https://github.invalid/pull/1")
        self.assertEqual(self.source.result["component_feedback_latest_batch"]["status"], "completed")
        self.assertEqual(self.child.result["source_setup_run_id"], self.source.run_id)
        self.assertEqual(self.child.result["previewProof"], "new")
        self.assertEqual(self.comment.status, "submitted")
        self.assertEqual(self.comment.context["reviewOutcome"]["status"], "addressed")
        self.assertEqual(self.comment.context["domPath"], "body > main")
        self.assertEqual(self.source.result["article_review_updates"]["operation-1"]["status"], "accepted")
        self.assertEqual(self.source.result["article_review_updates"]["operation-1"]["revisionRunId"], self.child.run_id)
        self.source.save.assert_called_once_with(update_fields=["result", "updated_at"])

    def test_setup_revision_dispatch_reconciles_only_new_marker(self):
        from . import service_views
        view = service_views.ContentFactoryCallbackView()
        with patch.object(service_views, "_sync_article_system_setup_callback_to_run", return_value=self.child), \
             patch.object(callbacks, "reconcile_setup_review_outcomes", return_value=True) as reconcile:
            response = view._dispatch_callback_event(self.data,
                event_type="article_system_setup_revision_ready", job_id=self.child.run_id)
            self.assertEqual(response.status_code, 200)
            reconcile.assert_called_once_with(self.data, self.child)
            reconcile.reset_mock()
            view._dispatch_callback_event({}, event_type="article_system_setup_revision_ready", job_id=self.child.run_id)
            reconcile.assert_not_called()

    def test_legacy_callback_has_no_reconciliation_side_effect(self):
        self.data.pop("review_update_operation_id")
        self.assertFalse(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.runs.filter.assert_not_called()
        self.comment.save.assert_not_called()

    def test_foreign_comment_or_batch_does_not_mutate_any_run(self):
        self.data["comment_outcomes"][0]["commentId"] = str(uuid4())
        self.assertFalse(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.comment.save.assert_not_called()
        self.source.save.assert_not_called()
        self.assertTrue(all(query["run"] is self.source and query["batch_id"] == "operation-1"
                            for query in self.storage.queries))

    def test_wrong_source_or_revision_cannot_apply_outcomes(self):
        self.source.domain = "foreign.test"
        self.assertFalse(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.source.domain = self.child.domain
        self.source.result["article_review_updates"] = {"operation-1": {"revisionRunId": "another-child"}}
        self.assertFalse(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.comment.save.assert_not_called()

    def test_later_source_batch_and_explicit_acceptance_are_preserved(self):
        self.source.result["component_feedback_latest_batch"] = {"id": "later-operation", "status": "running"}
        self.assertTrue(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.assertEqual(self.source.result["component_feedback_latest_batch"]["id"], "later-operation")
        self.source.result["component_feedback_latest_batch"] = {"id": "operation-1", "status": "accepted"}
        self.assertTrue(callbacks.reconcile_setup_review_outcomes(self.data, self.child))
        self.assertEqual(self.source.result["component_feedback_latest_batch"]["status"], "accepted")
