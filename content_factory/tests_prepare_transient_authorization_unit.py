"""Unanswered setup authorization resumes the saved run; no database construction.

Tala Thrive's setup c14aff04 stopped on 2026-10-16 after this backend's deploy
restarted its containers: Content Factory's authorization GET timed out, the
child was saved as a repairable CONNECTION_AUTHORIZATION_FAILED, and preparation
waited at "We stopped after 1 attempt" for the founder to press Try again.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_prepare as prepare, tests_prepare_journey_unit as journey_fixtures


TALA_RUN = "c14aff04-8d3d-4432-acbd-93d83bc86586"
UNANSWERED = "Articles directory scaffolding failed: Connection authorization could not be verified."


class UnansweredSetupAuthorizationTests(SimpleTestCase):
    def setUp(self):
        self.fixture = journey_fixtures.PrepareJourneyReplayTests()
        self.fixture.setUp()
        self.op = self.fixture.op
        self.op.receipt = {"child_run_id": TALA_RUN, "current_step": "setup"}
        self.op.payload["run_id"] = TALA_RUN

    def child(self, code="CONNECTION_AUTHORIZATION_FAILED", *, failure_class="repairable", generation=3,
              connection_id="website", error=UNANSWERED):
        # The callback saves CF's class and collapses retryable for non-transient classes.
        setup = {"status": "preview_failed", "error_code": code, "failure_step": "prepare_branch"}
        result = {"error_code": code, "retryable": failure_class == "transient", "article_system_setup": setup}
        if failure_class:
            result["failure_class"] = setup["failure_class"] = failure_class
        return SimpleNamespace(run_id=TALA_RUN, workflow="article_system_setup", status="blocked", error=error,
            result=result, domain="fixture.test", github_repo="fixture/site",
            run_request={"website_connection_id": connection_id, "connection_generation": generation,
                         "repository_id": 123, "domain": "fixture.test", "github_repo": "fixture/site"})

    def advance(self, child):
        with patch.object(prepare, "_resume_prepare_child") as resume:
            before = timezone.now()
            self.fixture.advance({"setup": self.fail_dispatch}, child)
        return resume, before

    def fail_dispatch(self, *args, **kwargs):
        raise AssertionError("an unanswered authorization must not dispatch a new setup run or branch")

    def test_tala_unanswered_authorization_schedules_a_resume_of_the_same_run(self):
        child = self.child()
        resume, before = self.advance(child)
        resume.assert_not_called()
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(self.op.receipt["failure_class"], "transient")
        self.assertTrue(self.op.receipt["child_resume_pending"])
        self.assertEqual(self.op.receipt["child_run_id"], TALA_RUN)
        self.assertGreaterEqual(self.op.next_attempt_at, before + timedelta(minutes=1) - timedelta(seconds=5))
        self.assertLessEqual(self.op.next_attempt_at, timezone.now() + timedelta(minutes=1))
        self.assertNotIn("We stopped", self.op.receipt["message"])
        self.assertNotEqual(self.op.receipt.get("user_action", {}).get("id"), "retry")

        resume, _ = self.advance(child)
        resume.assert_called_once()
        self.assertIs(resume.call_args.args[1], child)  # the saved run (and its branch), not a new setup
        self.assertEqual(self.op.state, "pending")
        self.assertNotIn("child_resume_pending", self.op.receipt)
        self.assertEqual(self.op.receipt["message"], "Resuming the saved website setup.")

    def test_unanswered_authorization_resumes_are_bounded(self):
        child = self.child()
        resumes = 0
        for _ in range(prepare.UNVERIFIED_AUTHORIZATION_RESUMES):
            self.advance(child)  # the resumed run failed again the same way
            self.assertEqual(self.op.state, "pending")
            resume, _ = self.advance(child)
            resumes += resume.call_count
        self.assertEqual(resumes, prepare.UNVERIFIED_AUTHORIZATION_RESUMES)
        resume, _ = self.advance(child)
        resume.assert_not_called()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertIn("We stopped after 4 attempts", self.op.receipt["message"])
        self.assertEqual(self.op.receipt["user_action"]["run_id"], TALA_RUN)

    def test_regenerated_connection_still_stops(self):
        for changed in ({"generation": 2}, {"connection_id": "another-website"}):
            with self.subTest(**changed):
                self.setUp()
                resume, _ = self.advance(self.child(**changed))
                resume.assert_not_called()
                self.assertEqual(self.op.state, "needs_attention")
                self.assertIsNone(self.op.next_attempt_at)
                self.assertIn("We stopped after 1 attempt", self.op.receipt["message"])

    def test_disconnected_connection_still_stops(self):
        self.op.connection.state = "disconnected"
        self.assertFalse(prepare._unverified_authorization_is_transient(
            self.op, self.child(), "CONNECTION_AUTHORIZATION_FAILED", "repairable", "setup"))

    def test_explicit_denials_still_stop(self):
        for code in ("connection_revoked", "website_connection_changed", "OPERATION_AUTHORIZATION_MISMATCH"):
            with self.subTest(code=code):
                self.setUp()
                resume, _ = self.advance(self.child(code))
                resume.assert_not_called()
                self.assertEqual(self.op.state, "needs_attention")
                self.assertIn("We stopped", self.op.receipt["message"])

    def test_content_factory_transient_code_resumes_without_a_supplied_class(self):
        resume, _ = self.advance(self.child("CONNECTION_SERVICE_TRANSIENT", failure_class=None))
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(self.op.receipt["failure_class"], "transient")
        self.assertTrue(self.op.receipt["child_resume_pending"])


@override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",))
class TaxonomyPolicyAuthorizationTests(SimpleTestCase):
    """Enrolled organizations follow the shared registry for both codes."""

    def setUp(self):
        self.replay = UnansweredSetupAuthorizationTests()
        self.replay.setUp()

    def test_registered_transient_code_resumes_the_same_run(self):
        self.replay.advance(self.replay.child("CONNECTION_SERVICE_TRANSIENT", failure_class="transient"))
        self.assertEqual(self.replay.op.state, "pending")
        self.assertTrue(self.replay.op.receipt["child_resume_pending"])

    def test_unversioned_legacy_code_gets_the_same_bounded_resume(self):
        # The registry projects CONNECTION_SERVICE_TRANSIENT onto this legacy code.
        self.replay.advance(self.replay.child())
        self.assertEqual(self.replay.op.state, "pending")
        self.assertTrue(self.replay.op.receipt["child_resume_pending"])

    def test_versioned_registry_failure_still_decides(self):
        from .website_failure_contract import attach_failure_projection, classify_failure
        child = self.replay.child()
        failure = classify_failure("CONNECTION_AUTHORIZATION_FAILED", "prepare_branch")
        child.result = attach_failure_projection({"article_system_setup": {"status": "preview_failed"}},
            failure, "33333333-3333-4333-8333-333333333333")
        resume, _ = self.replay.advance(child)
        resume.assert_not_called()
        self.assertEqual(self.replay.op.state, "needs_attention")
        self.assertEqual(self.replay.op.receipt["failure_class"], "unsupported")
