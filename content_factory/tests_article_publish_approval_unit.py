from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from content_factory.article_publish_approval import (
    RECEIPT_KEY,
    RECEIPT_REQUIRED_KEY,
    article_publish_approval_receipt_matches,
    make_article_publish_approval_receipt,
)
from content_factory.vibe_marketing_views import VibeMarketingRunControlView, _article_publish_retry_authorized


def _review_run():
    return SimpleNamespace(
        run_id="revision-1",
        status="awaiting_approval",
        approval_state="approval_required",
        run_request={},
        result={
            "generation": 0,
            "preview_url": "https://preview.example/articles/featured/one",
            "livePreview": {
                "previewUrl": "https://preview.example/articles/featured/one",
                "exactRender": True,
                "resumeGeneration": 0,
                "proof": {"commitSha": "a" * 40},
            },
            "article_preview_quality": {
                "status": "passed", "preview_url": "https://preview.example/articles/featured/one",
                "resume_generation": 0, "inputs_sha256": "b" * 64,
            },
        },
    )


class ArticlePublishApprovalReceiptTests(SimpleTestCase):
    def test_passing_preview_does_not_authorize_direct_promotion(self):
        run = _review_run()
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_explicit_approve_receipt_allows_same_review_retry(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertEqual(receipt["run_id"], run.run_id)
        self.assertEqual(receipt["commit_sha"], "a" * 40)
        run.approval_state = "approved"
        run.run_request[RECEIPT_REQUIRED_KEY] = True
        run.run_request[RECEIPT_KEY] = receipt
        self.assertTrue(article_publish_approval_receipt_matches(run))
        self.assertTrue(_article_publish_retry_authorized(run))

    def test_new_approval_without_receipt_cannot_use_legacy_child_retry(self):
        run = _review_run()
        run.approval_state = "approved"
        run.result["publish_child_run_id"] = "publish-child-1"
        run.run_request[RECEIPT_REQUIRED_KEY] = True
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_postapprove_identity_drift_returns_conflict_with_or_without_child(self):
        for has_child in (False, True):
            with self.subTest(has_child=has_child):
                run = _review_run()
                run.pk = 1
                run.workflow = "article_generation"
                run.save = MagicMock()
                current = _review_run()
                current.pk = 1
                current.workflow = "article_generation"
                current.save = MagicMock()
                request = SimpleNamespace(data={}, user=SimpleNamespace(pk=1))

                def remote_approve(**_kwargs):
                    self.assertTrue(current.run_request[RECEIPT_REQUIRED_KEY])
                    return {
                        "run_id": "publish-child-1" if has_child else run.run_id,
                        "status": "completed",
                    }

                lock_reads = 0

                def locked_current(**_kwargs):
                    nonlocal lock_reads
                    lock_reads += 1
                    if lock_reads == 2:
                        # A status poll advanced the saved generation after CF
                        # approved and the local action synced its response.
                        current.approval_state = "approved"
                        current.result["generation"] = 1
                        current.result["publish_child_run_id"] = "publish-child-1"
                    return current

                with (
                    patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(object(), None)),
                    patch("content_factory.vibe_marketing_views.get_object_or_404", return_value=run),
                    patch("content_factory.vibe_marketing_views._run_belongs_to_context", return_value=True),
                    patch("content_factory.vibe_marketing_views._latest_review_ready_component_revision", return_value=None),
                    patch("content_factory.vibe_marketing_views.founder_actor_id_for_user", return_value="founder-1"),
                    patch("content_factory.vibe_marketing_views.transaction.atomic", return_value=nullcontext()),
                    patch("content_factory.vibe_marketing_views.ContentFactoryRun.objects.select_for_update") as lock,
                    patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=remote_approve),
                    patch(
                        "content_factory.vibe_marketing_views._sync_publish_child_from_control_response",
                        return_value=SimpleNamespace(run_id="publish-child-1") if has_child else None,
                    ),
                ):
                    lock.return_value.get.side_effect = locked_current
                    response = VibeMarketingRunControlView().post(request, run.run_id, "approve")

                self.assertEqual(response.status_code, 409)
                self.assertEqual(lock_reads, 2)
                self.assertIn("reviewed preview changed", response.data["detail"])
                self.assertEqual(current.result["livePreview"]["resumeGeneration"], 0)
                self.assertEqual(current.result["article_preview_quality"]["resume_generation"], 0)
                self.assertNotIn(RECEIPT_KEY, current.run_request)
                self.assertFalse(_article_publish_retry_authorized(current))

    def test_receipt_rejects_changed_run_preview_commit_and_quality(self):
        for changed in ("run_id", "preview_url", "commit_sha", "resume_generation", "quality_inputs_sha256"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(run, actor_id="founder-1")
                run.approval_state = "approved"
                if changed == "run_id":
                    run.run_id = "revision-2"
                elif changed == "preview_url":
                    run.result["livePreview"]["previewUrl"] = "https://preview.example/articles/featured/two"
                elif changed == "commit_sha":
                    run.result["livePreview"]["proof"]["commitSha"] = "c" * 40
                elif changed == "resume_generation":
                    run.result["livePreview"]["resumeGeneration"] = 1
                else:
                    run.result["article_preview_quality"]["inputs_sha256"] = "d" * 64
                run.result["publish_child_run_id"] = "publish-child-1"
                self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_changed_run_generation_with_unchanged_hosted_review(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertEqual(receipt["run_generation"], 0)
        run.approval_state = "approved"
        run.run_request[RECEIPT_KEY] = receipt
        run.result["generation"] = 1
        self.assertEqual(run.result["livePreview"]["resumeGeneration"], 0)
        self.assertEqual(run.result["article_preview_quality"]["resume_generation"], 0)
        self.assertFalse(article_publish_approval_receipt_matches(run))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_regressed_quality_status_with_unchanged_hash(self):
        for status in ("blocking_findings", "queued"):
            with self.subTest(status=status):
                run = _review_run()
                receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
                run.approval_state = "approved"
                run.run_request[RECEIPT_KEY] = receipt
                run.result["article_preview_quality"]["status"] = status
                self.assertEqual(run.result["article_preview_quality"]["inputs_sha256"], receipt["quality_inputs_sha256"])
                self.assertFalse(article_publish_approval_receipt_matches(run))
                self.assertFalse(_article_publish_retry_authorized(run))

        run = _review_run()
        run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(run, actor_id="founder-1")
        run.approval_state = "approved"
        run.result["article_preview_quality"]["status"] = "advisory_findings"
        self.assertTrue(_article_publish_retry_authorized(run))

    def test_no_baseline_quality_requires_hash_and_allows_exact_retry(self):
        run = _review_run()
        run.result["article_preview_quality"]["status"] = "passed_no_baseline"
        # The run lease generation and hosted preview attempt are separate.
        run.result["generation"] = 1
        run.result["livePreview"]["resumeGeneration"] = "0"
        run.result["article_preview_quality"]["resume_generation"] = "0"
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["run_generation"], 1)
        self.assertEqual(receipt["resume_generation"], 0)
        self.assertEqual(receipt["quality_inputs_sha256"], "b" * 64)
        run.approval_state = "approved"
        run.run_request[RECEIPT_KEY] = receipt
        self.assertTrue(article_publish_approval_receipt_matches(run))
        self.assertTrue(_article_publish_retry_authorized(run))

        run.result["article_preview_quality"]["inputs_sha256"] = "c" * 64
        self.assertFalse(_article_publish_retry_authorized(run))

        run.result["article_preview_quality"].pop("inputs_sha256")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_render_or_quality_target_drift(self):
        for changed in ("exact_render", "quality_preview_url", "quality_generation"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.result["article_preview_quality"]["status"] = "passed_no_baseline"
                receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
                self.assertIsNotNone(receipt)
                run.approval_state = "approved"
                run.run_request[RECEIPT_KEY] = receipt
                if changed == "exact_render":
                    run.result["livePreview"]["exactRender"] = False
                elif changed == "quality_preview_url":
                    run.result["article_preview_quality"]["preview_url"] = "https://preview.example/articles/other"
                else:
                    run.result["article_preview_quality"]["resume_generation"] = 1
                self.assertFalse(article_publish_approval_receipt_matches(run))
                self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_creation_rejects_inexact_or_stale_hosted_quality(self):
        for changed in ("exact_render", "quality_preview_url", "quality_generation", "missing_quality"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.result["article_preview_quality"]["status"] = "passed_no_baseline"
                if changed == "exact_render":
                    run.result["livePreview"]["exactRender"] = False
                elif changed == "quality_preview_url":
                    run.result["article_preview_quality"]["preview_url"] = "https://preview.example/articles/other"
                elif changed == "quality_generation":
                    run.result["livePreview"]["resumeGeneration"] = 1
                else:
                    run.result.pop("article_preview_quality")
                self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))

    def test_legacy_retry_requires_prior_approval_and_known_child(self):
        run = _review_run()
        run.result["publish_child_run_id"] = "publish-child-1"
        self.assertFalse(_article_publish_retry_authorized(run))
        run.approval_state = "approved"
        self.assertTrue(_article_publish_retry_authorized(run))
        for status in ("blocking_findings", "queued"):
            run.result["article_preview_quality"]["status"] = status
            self.assertTrue(_article_publish_retry_authorized(run))
        del run.result["publish_child_run_id"]
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_requires_hosted_proof_not_fallback_or_forged_commit(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        del run.result["livePreview"]["proof"]
        run.result["livePreview"]["commitSha"] = "a" * 40
        run.result["branch_commit_sha"] = "a" * 40
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.approval_state = "approved"
        run.result["publish_child_run_id"] = "publish-child-1"
        run.run_request[RECEIPT_KEY] = receipt
        self.assertFalse(article_publish_approval_receipt_matches(run))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_requires_valid_commit_and_applicable_quality_hash(self):
        run = _review_run()
        run.result["livePreview"]["proof"]["commitSha"] = "not-a-commit"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["livePreview"]["proof"]["commitSha"] = "a" * 40
        run.result["article_preview_quality"].pop("inputs_sha256")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["article_preview_quality"]["status"] = "advisory_findings"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["article_preview_quality"]["status"] = "passed_no_baseline"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result.pop("article_preview_quality")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
