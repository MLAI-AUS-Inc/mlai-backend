from types import SimpleNamespace

from django.test import SimpleTestCase

from content_factory.article_publish_approval import (
    RECEIPT_KEY,
    article_publish_approval_receipt_matches,
    make_article_publish_approval_receipt,
)
from content_factory.vibe_marketing_views import _article_publish_retry_authorized


def _review_run():
    return SimpleNamespace(
        run_id="revision-1",
        status="awaiting_approval",
        approval_state="approval_required",
        run_request={},
        result={
            "preview_url": "https://preview.example/articles/featured/one",
            "livePreview": {
                "previewUrl": "https://preview.example/articles/featured/one",
                "exactRender": True,
                "proof": {"commitSha": "a" * 40},
            },
            "article_preview_quality": {"status": "passed", "inputs_sha256": "b" * 64},
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
        run.run_request[RECEIPT_KEY] = receipt
        self.assertTrue(article_publish_approval_receipt_matches(run))
        self.assertTrue(_article_publish_retry_authorized(run))

    def test_receipt_rejects_changed_run_preview_commit_and_quality(self):
        for changed in ("run_id", "preview_url", "commit_sha", "quality_inputs_sha256"):
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
                else:
                    run.result["article_preview_quality"]["inputs_sha256"] = "d" * 64
                run.result["publish_child_run_id"] = "publish-child-1"
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
        run.result.pop("article_preview_quality")
        self.assertIsNotNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
