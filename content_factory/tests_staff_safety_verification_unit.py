"""Plan step 8: staff verify exact current safety sentences; founders hear only after the acknowledged recheck passes."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import staff_safety_verification as verification

URL, GEN, FP = "https://preview.test/a", 3, "fp-1"
SAFETY_HASH = "a" * 64
LIVE = {"previewUrl": URL, "resumeGeneration": GEN}
BLOCKER = {"code": "article_preview_safety_review_required"}
HELD = {"status": "blocking_findings", "self_heal_exhausted": True, "escalation": "safety_review",
        "preview_url": URL, "resume_generation": GEN, "self_heal_fingerprint": FP, "task_id": "task-old",
        "review_token": "task-old"}
CLAIMS = [{"sentence_sha256": "b" * 64, "source_url": "https://www.healthdirect.gov.au/x",
           "note": "Healthdirect lists 000 for immediate danger."}]
REQUEST = {"held": {"preview_url": URL, "resume_generation": GEN, "self_heal_fingerprint": FP, "safety_hash": SAFETY_HASH},
           "claims": CLAIMS, "actor": {"user_id": "999", "organization_id": "1"}}
QUEUED = {"status": "queued", "preview_url": URL, "resume_generation": GEN, "task_id": "task-new", "review_token": "task-new"}
RECHECK = {"status": "quality_review_queued", "idempotent": False, "article_preview_quality": QUEUED}
STAFF = SimpleNamespace(pk=17, email="Staff@MLAI.au", is_active=True, is_staff=True)
ORG = SimpleNamespace(pk=30)
NOW = __import__("datetime").datetime(2026, 11, 6, 13, 0, tzinfo=__import__("datetime").timezone.utc)


def stored(quality=HELD, *, live=LIVE, blocker=BLOCKER, org=30, **extra):
    result = {"article_preview_quality": quality, "live_preview": live, **extra}
    if blocker is not None:
        result["approval_blocker"] = blocker
    return SimpleNamespace(pk=7, run_id="run-1", workflow="article_generation", organization_id=org,
                           status="awaiting_approval", result=result)


class VerifyRequestTests(SimpleTestCase):
    def verify(self, data=REQUEST, *, run=None, current=None, remote=None, key="key-1"):
        run = run or stored()
        current = current or stored(run.result["article_preview_quality"])
        manager = Mock()
        manager.select_for_update.return_value.get.return_value = current
        remote = {"status": "recorded", "recheck": RECHECK} if remote is None else remote
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=remote) as call, \
                patch.object(verification.ContentFactoryRun, "objects", manager), \
                patch.object(verification.transaction, "atomic", nullcontext):
            outcome = verification.verify_safety_claims(run, ORG, STAFF, data, now=NOW, command_key=key)
        return outcome, call, manager

    def written(self, manager):
        return manager.filter.return_value.update.call_args.kwargs["result"]

    def test_a_matching_request_arms_the_notice_for_the_acknowledged_recheck(self):
        outcome, call, manager = self.verify()
        self.assertEqual((outcome["status"], outcome["recheck_queued"], outcome["notice_armed"]), ("verified", True, True))
        from .vibe_marketing_views import STAFF_ACTION_CAPABILITY
        self.assertIs(call.call_args.kwargs["staff_capability"], STAFF_ACTION_CAPABILITY)
        self.assertEqual(call.call_args.kwargs["action"], "record-staff-safety-verification")
        written = self.written(manager)
        receipt = written[verification.RECEIPT_KEY]
        self.assertEqual((receipt["recheck_task_id"], receipt["recheck_review_token"]), ("task-new", "task-new"))
        self.assertEqual((receipt["preview_url"], receipt["resume_generation"], receipt["by_user_id"]), (URL, GEN, 17))
        self.assertIsNone(receipt["notified_at"])
        self.assertEqual(written["article_preview_quality"], QUEUED)
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
                outcome, call, _ = self.verify(run=stored(quality))
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

    def test_a_state_that_changed_during_the_call_is_observe_and_arms_nothing(self):
        changes = {
            "newer quality": stored({**HELD, "status": "passed", "resume_generation": 4}),
            "same quality, new blocker": stored(HELD, blocker={"code": "article_publish_policy_hold"}),
            "preview moved on": stored(HELD, live={"previewUrl": "https://preview.test/b", "resumeGeneration": GEN}),
            "newer generation": stored(HELD, live={"previewUrl": URL, "resumeGeneration": GEN + 1}),
            "other organization": stored(HELD, org=31),
        }
        for label, current in changes.items():
            with self.subTest(label):
                before = dict(current.result)
                outcome, call, manager = self.verify(current=current)
                call.assert_called_once()
                self.assertEqual(outcome["status"], "observe")
                self.assertFalse(outcome["recheck_queued"])
                # The audit of what Content Factory recorded is still returned for staff history.
                self.assertEqual(outcome["audit"]["claims"], CLAIMS)
                manager.filter.return_value.update.assert_not_called()
                self.assertEqual(current.result, before)

    def test_no_fresh_recheck_acknowledgement_arms_no_notice(self):
        for label, recheck in (
                ("refused", {"status": "refused"}),
                ("idempotent", {**RECHECK, "idempotent": True}),
                ("idempotent unknown", {k: v for k, v in RECHECK.items() if k != "idempotent"}),
                ("already running", {**RECHECK, "article_preview_quality": {**QUEUED, "status": "running"}}),
                ("no task", {**RECHECK, "article_preview_quality": {**QUEUED, "task_id": ""}}),
                ("held task", {**RECHECK, "article_preview_quality": {**QUEUED, "task_id": "task-old"}}),
                ("newer generation", {**RECHECK, "article_preview_quality": {**QUEUED, "resume_generation": GEN + 1}})):
            with self.subTest(label):
                outcome, _, manager = self.verify(remote={"status": "recorded", "recheck": recheck})
                self.assertEqual((outcome["status"], outcome["notice_armed"]), ("verified", False))
                manager.filter.return_value.update.assert_not_called()


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
    PASSED = {"status": "passed", "preview_url": URL, "resume_generation": GEN, "task_id": "task-new", "review_token": "task-new"}

    def receipt(self, **extra):
        return {"request_key": "staff-safety:k", "by_user_id": 17, "organization_id": 30, "preview_url": URL,
                "resume_generation": GEN, "recheck_task_id": "task-new", "recheck_review_token": "task-new",
                "recorded_at": NOW.isoformat(), "claims": 1, "notified_at": None, **extra}

    def held_run(self, *, quality=None, receipt=None, live=LIVE, status="awaiting_approval"):
        run = stored(self.PASSED if quality is None else quality, live=live, blocker=None,
                     **{verification.RECEIPT_KEY: self.receipt() if receipt is None else receipt})
        run.status = status
        return run

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

    def test_the_acknowledged_recheck_passing_notifies_once(self):
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

    def test_an_older_overlapping_pass_of_the_same_preview_never_notifies(self):
        # Same preview and generation, finished after the staff submission, but
        # it is not the recheck Content Factory acknowledged for the verification.
        for label, quality in (
                ("older task, later finish", {**self.PASSED, "task_id": "task-old", "review_token": "task-old",
                                              "reviewed_at": "2026-11-06T13:05:00+00:00"}),
                ("same task, other token", {**self.PASSED, "review_token": "task-other"}),
                ("no task identity", {k: v for k, v in self.PASSED.items() if k not in {"task_id", "review_token"}})):
            with self.subTest(label):
                count, send, manager = self.notify(self.held_run(quality=quality))
                self.assertEqual(count, 0)
                send.assert_not_called()
                manager.filter.return_value.update.assert_not_called()

    def test_no_notice_without_the_same_generation_passing(self):
        for label, quality in (
                ("recheck failed", {**self.PASSED, "status": "blocking_findings"}),
                ("still checking", {**self.PASSED, "status": "running"}),
                ("other generation", {**self.PASSED, "resume_generation": GEN + 1}),
                ("other preview", {**self.PASSED, "preview_url": "https://preview.test/b"})):
            with self.subTest(label):
                count, send, _ = self.notify(self.held_run(quality=quality))
                self.assertEqual(count, 0)
                send.assert_not_called()
        for label, run in (("preview moved on", self.held_run(live={"previewUrl": "https://preview.test/b", "resumeGeneration": GEN})),
                           ("cancelled", self.held_run(status="cancelled")), ("no receipt", self.held_run(receipt={})),
                           ("receipt without recheck identity", self.held_run(receipt=self.receipt(recheck_task_id=None)))):
            with self.subTest(label):
                count, send, _ = self.notify(run)
                self.assertEqual(count, 0)
                send.assert_not_called()

    def test_a_concurrent_sync_that_already_notified_does_not_send_again(self):
        already = self.held_run(receipt=self.receipt(notified_at=NOW.isoformat()))
        count, send, manager = self.notify(self.held_run(), current=already)
        self.assertEqual(count, 0)
        send.assert_not_called()
        manager.filter.return_value.update.assert_not_called()


class StaffSafetyStateTests(SimpleTestCase):
    REMOTE = {"held": True, "preview_url": URL, "resume_generation": GEN, "self_heal_fingerprint": FP,
              "safety_hash": SAFETY_HASH, "locale_supported": True, "allowed_source_domains": ["healthdirect.gov.au"],
              "sentences": [{"sentence_sha256": "b" * 64, "text": "If there is immediate danger, call 000.",
                             "medication_or_dosing": False}]}

    def read(self, remote=None, *, run=None):
        run = run or stored()
        remote = self.REMOTE if remote is None else remote
        with patch("organizations.models.Organization.objects") as orgs, \
                patch.object(verification.ContentFactoryRun, "objects") as runs, \
                patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=remote) as call:
            orgs.get.return_value = ORG
            runs.get.return_value = run
            state = verification.staff_safety_state(30, "run-1")
        return state, call, runs

    def test_staff_see_the_held_binding_and_current_sentences_to_submit(self):
        state, call, runs = self.read()
        runs.get.assert_called_once_with(organization=ORG, run_id="run-1")
        from .vibe_marketing_views import STAFF_ACTION_CAPABILITY
        self.assertEqual(call.call_args.kwargs["action"], "staff-safety-state")
        self.assertIs(call.call_args.kwargs["staff_capability"], STAFF_ACTION_CAPABILITY)
        self.assertEqual(state["status"], "held")
        self.assertTrue(state["verifiable"])
        self.assertEqual(state["held"], {"previewUrl": URL, "resumeGeneration": GEN,
                                         "selfHealFingerprint": FP, "safetyHash": SAFETY_HASH})
        self.assertEqual(state["sentences"], [{"sentenceSha256": "b" * 64, "text": "If there is immediate danger, call 000.",
                                               "medicationOrDosing": False}])
        self.assertEqual(state["allowedSourceDomains"], ["healthdirect.gov.au"])

    def test_the_shown_binding_round_trips_into_a_verification_request(self):
        state, _, _ = self.read()
        held = state["held"]
        data = {"held": {"preview_url": held["previewUrl"], "resume_generation": held["resumeGeneration"],
                         "self_heal_fingerprint": held["selfHealFingerprint"], "safety_hash": held["safetyHash"]},
                "claims": [{"sentence_sha256": item["sentenceSha256"], "source_url": "https://www.healthdirect.gov.au/x",
                            "note": "Healthdirect lists 000 for immediate danger."} for item in state["sentences"]]}
        outcome, call, _ = VerifyRequestTests().verify(data)
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(call.call_args.kwargs["payload"]["held"], data["held"])

    def test_a_run_not_held_for_safety_is_not_read_from_content_factory(self):
        state, call, _ = self.read(run=stored({**HELD, "escalation": "quality_review"}))
        self.assertEqual(state, {"status": "not_held", "runId": "run-1"})
        call.assert_not_called()

    def test_a_binding_that_differs_from_the_held_state_is_observe(self):
        for key, value in (("preview_url", "https://preview.test/b"), ("resume_generation", GEN + 1),
                           ("self_heal_fingerprint", "fp-0"), ("safety_hash", "short"), ("held", False)):
            with self.subTest(key=key):
                state, _, _ = self.read({**self.REMOTE, key: value})
                self.assertEqual(state["status"], "observe")
                self.assertNotIn("sentences", state)
        state, _, _ = self.read(run=stored(HELD, live={"previewUrl": "https://preview.test/b", "resumeGeneration": GEN}))
        self.assertEqual(state["status"], "observe")
        state, _, _ = self.read({**self.REMOTE, "sentences": [{"sentence_sha256": "nope", "text": "x"}]})
        self.assertEqual(state["status"], "observe")

    def test_real_holds_are_shown_but_not_verifiable(self):
        cases = {
            "medication_or_dosing": {**self.REMOTE, "sentences": [{**self.REMOTE["sentences"][0], "medication_or_dosing": True}]},
            "locale_unsupported": {**self.REMOTE, "locale_supported": False, "allowed_source_domains": []},
            "no_safety_sentences": {**self.REMOTE, "sentences": []},
        }
        for reason, remote in cases.items():
            with self.subTest(reason):
                state, _, _ = self.read(remote)
                self.assertEqual((state["status"], state["verifiable"], state["reason"]), ("held", False, reason))

    def test_content_factory_unavailable_is_reported_without_sentences(self):
        state, _, _ = self.read({"error": "down"})
        self.assertEqual(state, {"status": "unavailable", "runId": "run-1"})

    def test_the_founder_run_response_never_carries_the_staff_safety_state(self):
        from .hosted_quality_state import founder_result
        view = founder_result({**stored().result, verification.RECEIPT_KEY: {"recheck_task_id": "task-new"},
                               "staff_actions_v1": [{"claims": CLAIMS}]})
        text = str(view)
        for leaked in ("task-new", "healthdirect", "sentence_sha256", SAFETY_HASH):
            self.assertNotIn(leaked, text)
