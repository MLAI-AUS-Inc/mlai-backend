"""Staff "Retry now" on a held article quality check fails closed without a positive receipt."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import staff_console as console

HELD = {"status": "blocking_findings", "self_heal_exhausted": True, "escalation": "safety_review",
        "preview_url": "https://preview.test/a", "resume_generation": 3}
BLOCKER = {"code": "article_preview_safety_review_required"}
QUEUED = {"status": "quality_review_queued",
          "article_preview_quality": {"status": "queued", "preview_url": "https://preview.test/a",
                                      "resume_generation": 3}}


class RecheckAcknowledgementTests(SimpleTestCase):
    def test_only_a_current_queued_receipt_counts(self):
        self.assertTrue(console.recheck_acknowledged(QUEUED, HELD))
        running = {**QUEUED, "article_preview_quality": {**QUEUED["article_preview_quality"], "status": "running"}}
        self.assertTrue(console.recheck_acknowledged(running, HELD))

    def test_missing_malformed_refused_or_pending_responses_fail_closed(self):
        for remote in (None, {}, [], "ok", {"status": "quality_review_queued"},
                       {"status": "quality_review_queued", "article_preview_quality": "queued"},
                       {"status": "pending"}, {"status": "refused"},
                       {**QUEUED, "error": "worker unavailable"},
                       {**QUEUED, "status": "dispatch_unconfirmed"},
                       {**QUEUED, "article_preview_quality": {**QUEUED["article_preview_quality"],
                                                              "status": "blocking_findings"}}):
            with self.subTest(remote=remote):
                self.assertFalse(console.recheck_acknowledged(remote, HELD))

    def test_a_receipt_for_another_preview_or_older_generation_fails_closed(self):
        other = {**QUEUED, "article_preview_quality": {**QUEUED["article_preview_quality"],
                                                       "preview_url": "https://preview.test/b"}}
        older = {**QUEUED, "article_preview_quality": {**QUEUED["article_preview_quality"], "resume_generation": 2}}
        self.assertFalse(console.recheck_acknowledged(other, HELD))
        self.assertFalse(console.recheck_acknowledged(older, HELD))


class RecheckWriteTests(SimpleTestCase):
    def run_recheck(self, remote, current_quality=HELD, held_blocker=BLOCKER, current_blocker=None):
        current_blocker = held_blocker if current_blocker is None else current_blocker
        run = SimpleNamespace(pk=7, run_id="run-1", workflow="article_generation",
                              result={"article_preview_quality": HELD, "approval_blocker": held_blocker})
        current = SimpleNamespace(pk=7, result={"article_preview_quality": current_quality,
                                                "approval_blocker": current_blocker})
        manager = Mock()
        manager.select_for_update.return_value.get.return_value = current
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action",
                   return_value=remote) as call, \
                patch.object(console.ContentFactoryRun, "objects", manager), \
                patch.object(console.transaction, "atomic", nullcontext):
            outcome = console._recheck_article_quality(run)
        self.assertEqual(call.call_args.kwargs["action"], "retry-preview-quality")
        return outcome, manager

    def test_positive_receipt_replaces_the_hold_and_clears_the_blocker(self):
        outcome, manager = self.run_recheck(QUEUED)
        self.assertEqual(outcome["status"], "queued")
        written = manager.filter.return_value.update.call_args.kwargs["result"]
        self.assertEqual(written["article_preview_quality"]["status"], "queued")
        self.assertNotIn("approval_blocker", written)

    def test_refused_or_empty_responses_keep_the_original_blocker(self):
        for remote in ({}, None, {"error": "Preview quality could not be queued."}, {"status": "pending"}):
            with self.subTest(remote=remote):
                outcome, manager = self.run_recheck(remote)
                self.assertEqual(outcome["status"], "refused")
                manager.filter.return_value.update.assert_not_called()
                manager.select_for_update.assert_not_called()

    def test_a_newer_revision_or_check_is_never_overwritten(self):
        newer = {"status": "passed", "preview_url": "https://preview.test/a", "resume_generation": 4}
        outcome, manager = self.run_recheck(QUEUED, current_quality=newer)
        self.assertEqual(outcome["status"], "observe")
        manager.filter.return_value.update.assert_not_called()

    def test_a_replaced_blocker_with_unchanged_quality_is_never_cleared(self):
        # Same held quality and preview, but a new approval rejection landed
        # after the hold was read (e.g. the founder pressed Approve again).
        for newer in ({"code": "article_preview_safety_review_required", "recorded_at": "later"},
                      {"code": "website_authority_mismatch", "message": "Reconnect the site."}):
            with self.subTest(newer=newer):
                outcome, manager = self.run_recheck(QUEUED, current_blocker=newer)
                self.assertEqual(outcome["status"], "observe")
                manager.filter.return_value.update.assert_not_called()

    def test_a_non_quality_blocker_survives_a_positive_recheck(self):
        other = {"code": "website_authority_mismatch", "message": "Reconnect the site."}
        outcome, manager = self.run_recheck(QUEUED, held_blocker=other)
        self.assertEqual(outcome["status"], "queued")
        written = manager.filter.return_value.update.call_args.kwargs["result"]
        self.assertEqual(written["approval_blocker"], other)
        self.assertEqual(written["article_preview_quality"]["status"], "queued")

    def test_a_refused_recheck_is_audited_as_refused(self):
        run = SimpleNamespace(result={"article_preview_quality": HELD})
        with patch.object(console, "_recheck_article_quality", return_value={"status": "refused", "detail": "x"}):
            response = console._perform("retry_now", run, None, None, now=None)
        self.assertEqual(response["status"], "refused")
