"""A failed site preview no longer blocks a finished article's approval (plan step 9).

Content Factory offers a website-bound article whose own site build could not
be previewed for approval from its stand-in preview or, with no rendering at
all, from its text. The backend's review-identity gate lets that approval
through; ``ARTICLES_STAND_IN_PREVIEW_APPROVAL_ENABLED=False`` restores the
exact-build-only gate.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from content_factory.article_publish_approval import (
    approval_basis,
    article_review_identity,
    article_review_identity_is_complete,
)

URL = "https://stand-in.example/articles/one"


def _run(result):
    return SimpleNamespace(run_id="article-1", status="awaiting_approval", approval_state="approval_required",
                           run_request={}, result=result)


def _stand_in(rung="snapshot"):
    return {
        "status": "fallback_ready", "approval_basis": "stand_in_preview",
        "livePreview": {"previewUrl": URL, "exactRender": False, "resumeGeneration": 1,
                        "proof": {"commitSha": "c" * 40, "repoBoundSnapshot": True, "previewRung": rung}},
        "article_preview_quality": {"status": "passed_no_baseline", "preview_url": URL, "resume_generation": 1,
                                    "inputs_sha256": "d" * 64},
    }


def _article_text():
    return {"status": "fallback_ready", "approval_basis": "article_text",
            "livePreview": {"status": "failed", "previewUrl": "", "exactRender": False}}


class StandInPreviewApprovalTests(SimpleTestCase):
    def test_a_stand_in_with_its_browser_review_is_approvable(self):
        for rung in ("snapshot", "template"):
            with self.subTest(rung=rung):
                run = _run(_stand_in(rung))
                self.assertEqual(approval_basis(run), "stand_in_preview")
                self.assertTrue(article_review_identity_is_complete(run))

    def test_a_stand_in_still_needs_its_browser_review(self):
        result = _stand_in()
        result["article_preview_quality"] = {"status": "blocking_findings", "preview_url": URL,
                                             "resume_generation": 1, "inputs_sha256": "d" * 64}
        self.assertFalse(article_review_identity_is_complete(_run(result)))

    def test_a_fallback_that_is_not_a_stand_in_still_needs_the_exact_build(self):
        result = _stand_in()
        result["livePreview"]["proof"] = {"commitSha": "c" * 40}
        run = _run(result)
        self.assertEqual(approval_basis(run), "")
        self.assertFalse(article_review_identity_is_complete(run))

    def test_the_article_text_is_approvable_with_no_preview_or_review(self):
        run = _run(_article_text())
        self.assertEqual(approval_basis(run), "article_text")
        self.assertTrue(article_review_identity_is_complete(run))
        self.assertEqual(article_review_identity(run)["preview_url"], "")

    @override_settings(ARTICLES_STAND_IN_PREVIEW_APPROVAL_ENABLED=False)
    def test_with_the_kill_switch_off_only_the_exact_build_is_approvable(self):
        for result in (_stand_in(), _article_text()):
            with self.subTest(basis=result["approval_basis"]):
                run = _run(result)
                self.assertEqual(approval_basis(run), "")
                self.assertFalse(article_review_identity_is_complete(run))

    def test_the_exact_build_is_unchanged(self):
        result = _stand_in()
        result.pop("approval_basis")
        result["livePreview"]["exactRender"] = True
        result["livePreview"]["proof"] = {"commitSha": "c" * 40}
        self.assertTrue(article_review_identity_is_complete(_run(result)))
