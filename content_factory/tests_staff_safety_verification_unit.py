"""Plan step 8: staff verify exact current safety sentences; founders hear only after a real pass."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import staff_safety_verification as verification

URL, GEN, FP = "https://preview.test/a", 3, "fp-1"
SAFETY_HASH = "a" * 64
HELD = {"status": "blocking_findings", "self_heal_exhausted": True, "escalation": "safety_review",
        "preview_url": URL, "resume_generation": GEN, "self_heal_fingerprint": FP}
CLAIMS = [{"sentence_sha256": "b" * 64, "source_url": "https://www.healthdirect.gov.au/x",
           "note": "Healthdirect lists 000 for immediate danger."}]
REQUEST = {"held": {"preview_url": URL, "resume_generation": GEN, "self_heal_fingerprint": FP, "safety_hash": SAFETY_HASH},
           "claims": CLAIMS, "actor": {"user_id": "999", "organization_id": "1"}}
RECHECK = {"status": "quality_review_queued",
           "article_preview_quality": {"status": "queued", "preview_url": URL, "resume_generation": GEN}}
NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)
STAFF = SimpleNamespace(pk=17, email="Staff@MLAI.au", is_active=True, is_staff=True)
ORG = SimpleNamespace(pk=30)


class VerifyRequestTests(SimpleTestCase):
    def verify(self, data=REQUEST, *, quality=HELD, remote=None, current_quality=None, key="key-1"):
        run = SimpleNamespace(pk=7, run_id="run-1", workflow="article_generation",
                              result={"article_preview_quality": quality, "approval_blocker": {"code": "article_preview_safety_review_required"}})
        current = SimpleNamespace(pk=7, result={"article_preview_quality": current_quality or quality,
                                                "approval_blocker": {"code": "article_preview_safety_review_required"}})
        manager = Mock()
        manager.select_for_update.return_value.get.return_value = current
        remote = {"status": "recorded", "recheck": RECHECK} if remote is None else remote
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=remote) as call, \
                patch.object(verification.ContentFactoryRun, "objects", manager), \
                patch.object(verification.transaction, "atomic", nullcontext):
            outcome = verification.verify_safety_claims(run, ORG, STAFF, data, now=NOW, command_key=key)
        return outcome, call, manager

    def test_a_matching_request_records_and_queues_the_normal_recheck(self):
        outcome, call, manager = self.verify()
        self.assertEqual(outcome["status"], "verified")
        self.assertTrue(outcome["recheck_queued"])
        sent = call.call_args.kwargs
        self.assertEqual(sent["action"], "record-staff-safety-verification")
        self.assertIs(sent["staff_capability"], verification_capability())
        written = manager.filter.return_value.update.call_args.kwargs["result"]
        receipt = written[verification.RECEIPT_KEY]
        self.assertEqual((receipt["preview_url"], receipt["resume_generation"], receipt["by_user_id"]), (URL, GEN, 17))
        self.assertIsNone(receipt["notified_at"])
        self.assertEqual(written["article_preview_quality"]["status"], "queued")
        self.assertNotIn("approval_blocker", written)
        self.assertEqual(outcome["audit"]["claims"], CLAIMS)

    def test_the_actor_and_organization_come_from_the_staff_session_only(self):
        _, call, _ = self.verify()
        actor = call.call_args.kwargs["payload"]["actor"]
        self.assertEqual((actor["user_id"], actor["organization_id"]), ("17", "30"))
        self.assertEqual(len(actor["email_sha256"]), 64)
        self.assertNotIn("Staff@MLAI.au", str(call.call_args.kwargs["payload"]))

    def test_a_run_not_held_for_safety_is_refused_without_a_call(self):
        for quality in ({**HELD, "escalation": "quality_review"}, {**HELD, "self_heal_exhausted": False},
                        {**HELD, "status": "passed"}, {}):
            with self.subTest(quality=quality):
                outcome, call, _ = self.verify(quality=quality)
                self.assertEqual(outcome["status"], "refused")
                call.assert_not_called()

    def test_a_held_state_mismatch_is_observe_without_a_call(self):
        for key, value in (("preview_url", "https://preview.test/b"), ("resume_generation", 2),
                           ("self_heal_fingerprint", "fp-0"), ("safety_hash", "not-a-hash")):
            with self.subTest(key=key):
                outcome, call, _ = self.verify({**REQUEST, "held": {**REQUEST["held"], key: value}})
                self.assertEqual(outcome["status"], "observe")
                call.assert_not_called()

    def test_malformed_claims_or_missing_key_are_refused_without_a_call(self):
        bad = CLAIMS[0]
        for claims in ([], None, [{**bad, "sentence_sha256": "xyz"}], [{**bad, "source_url": "http://healthdirect.gov.au"}],
                       [{**bad, "note": "too short"}], [bad] * 51, ["text"]):
            with self.subTest(claims=str(claims)[:40]):
                outcome, call, _ = self.verify({**REQUEST, "claims": claims})
                self.assertEqual(outcome["status"], "refused")
                call.assert_not_called()
        outcome, call, _ = self.verify(key=None)
        self.assertEqual(outcome["status"], "refused")
        call.assert_not_called()

    def test_a_content_factory_refusal_writes_nothing(self):
        for remote, expected in (({"error": "x", "content_factory_response": {"detail": {"code": "observe"}}}, "observe"),
                                 ({"error": "x", "content_factory_response": {"detail": {"code": "partial_cover"}}}, "refused"),
                                 ({"error": "x", "content_factory_response": {"detail": {"code": "<script>"}}}, "refused"),
                                 ({}, "refused"), ({"status": "pending"}, "refused")):
            with self.subTest(remote=remote):
                outcome, _, manager = self.verify(remote=remote)
                self.assertEqual(outcome["status"], expected)
                self.assertIn(outcome["refusal"], verification.CF_REFUSALS | {"unknown"})
                manager.filter.return_value.update.assert_not_called()

    def test_a_newer_quality_state_is_kept_while_the_verification_is_still_recorded(self):
        newer = {**HELD, "status": "passed", "resume_generation": 4}
        outcome, _, manager = self.verify(current_quality=newer)
        written = manager.filter.return_value.update.call_args.kwargs["result"]
        self.assertEqual(written["article_preview_quality"], newer)
        self.assertIn(verification.RECEIPT_KEY, written)
        self.assertFalse(outcome["recheck_queued"])


def verification_capability():
    from .vibe_marketing_views import STAFF_ACTION_CAPABILITY
    return STAFF_ACTION_CAPABILITY


class StaffOnlyActionTests(SimpleTestCase):
    def test_no_other_caller_can_send_the_staff_verification_action(self):
        from . import vibe_marketing_views as views
        with patch.object(views, "require_unlocked_remote_call", side_effect=AssertionError("no remote call")):
            for capability in (None, object(), "staff"):
                remote = views._call_content_factory_run_action(run_id="run-1", action="record-staff-safety-verification",
                                                                payload={"actor": {"user_id": "1"}}, staff_capability=capability)
                self.assertEqual(remote["content_factory_status_code"], 404)

    def test_staff_records_never_reach_a_founder_response(self):
        from .hosted_quality_state import founder_result
        view = founder_result({"staff_actions_v1": [{"note": "staff"}], verification.RECEIPT_KEY: {"by_user_id": 17},
                               "result": {verification.RECEIPT_KEY: {}}, "title": "T"})
        self.assertEqual(view, {"result": {}, "title": "T"})


class FounderNotificationTests(SimpleTestCase):
    RECORDED = NOW.isoformat()

    def receipt(self, **extra):
        return {"request_key": "staff-safety:k", "by_user_id": 17, "organization_id": 30, "preview_url": URL,
                "resume_generation": GEN, "recorded_at": self.RECORDED, "claims": 1, "notified_at": None, **extra}

    def held_run(self, *, quality=None, receipt=None, live=None, status="awaiting_approval"):
        quality = {"status": "passed", "preview_url": URL, "resume_generation": GEN,
                   "reviewed_at": (NOW + timedelta(minutes=2)).isoformat()} if quality is None else quality
        return SimpleNamespace(pk=7, run_id="run-1", organization_id=30, status=status, result={
            "article_preview_quality": quality, "live_preview": live or {"previewUrl": URL, "resumeGeneration": GEN},
            verification.RECEIPT_KEY: self.receipt() if receipt is None else receipt})

    def notify(self, run, current=None):
        current = current or run
        manager = Mock()
        manager.select_for_update.return_value.get.return_value = current
        users = Mock()
        users.objects.filter.return_value.first.return_value = STAFF
        with patch.object(verification.ContentFactoryRun, "objects", manager), \
                patch.object(verification.transaction, "atomic", nullcontext), \
                patch("organizations.models.Organization.objects") as orgs, \
                patch("django.contrib.auth.get_user_model", return_value=users), \
                patch("content_factory.founder_transitions.notify_team_resolved", return_value=2) as send:
            orgs.select_for_update.return_value.get.return_value = ORG
            count = verification.notify_after_staff_safety_verification(run)
        return count, send, manager

    def test_the_same_preview_passing_after_the_verification_notifies_once(self):
        count, send, manager = self.notify(self.held_run())
        self.assertEqual(count, 2)
        organization, item = send.call_args.args
        self.assertEqual(organization, ORG)
        self.assertEqual(item, f"article:run-1:safety:staff-safety:k:{GEN}")
        self.assertIs(send.call_args.kwargs["by_user"], STAFF)
        written = manager.filter.return_value.update.call_args.kwargs["result"][verification.RECEIPT_KEY]
        self.assertTrue(written["notified_at"])
        count, send, _ = self.notify(self.held_run(receipt=self.receipt(notified_at=NOW.isoformat())))
        self.assertEqual(count, 0)
        send.assert_not_called()

    def test_no_notice_without_a_current_same_generation_pass(self):
        later = (NOW + timedelta(minutes=2)).isoformat()
        cases = {
            "recheck failed": {"status": "blocking_findings", "preview_url": URL, "resume_generation": GEN, "reviewed_at": later},
            "still checking": {"status": "queued", "preview_url": URL, "resume_generation": GEN},
            "stale callback": {"status": "passed", "preview_url": URL, "resume_generation": GEN,
                               "reviewed_at": (NOW - timedelta(minutes=1)).isoformat()},
            "no review time": {"status": "passed", "preview_url": URL, "resume_generation": GEN},
            "other generation": {"status": "passed", "preview_url": URL, "resume_generation": GEN + 1, "reviewed_at": later},
            "other preview": {"status": "passed", "preview_url": "https://preview.test/b", "resume_generation": GEN, "reviewed_at": later},
        }
        for label, quality in cases.items():
            with self.subTest(label):
                count, send, _ = self.notify(self.held_run(quality=quality))
                self.assertEqual(count, 0)
                send.assert_not_called()
        for label, run in (("preview moved on", self.held_run(live={"previewUrl": "https://preview.test/b", "resumeGeneration": GEN})),
                           ("cancelled", self.held_run(status="cancelled")), ("no receipt", self.held_run(receipt={}))):
            with self.subTest(label):
                count, send, _ = self.notify(run)
                self.assertEqual(count, 0)
                send.assert_not_called()

    def test_a_concurrent_sync_that_already_notified_does_not_send_again(self):
        run = self.held_run()
        already = self.held_run(receipt=self.receipt(notified_at=NOW.isoformat()))
        count, send, manager = self.notify(run, current=already)
        self.assertEqual(count, 0)
        send.assert_not_called()
        manager.filter.return_value.update.assert_not_called()
