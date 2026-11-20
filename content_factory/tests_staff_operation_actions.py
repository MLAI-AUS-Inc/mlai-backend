"""Plan F10: operation-scoped staff actions and the escalation ladder.

Acceptance: each action is idempotent (a second call with the same key is a
no-op with the same answer), audited (who, when, why, before and after), and
inside consent (a staff action never approves or merges a setup pull request;
a restart that resets the attempt cap has its own cap). No email, Slack or
provider call is made: alerts are only queued and Content Factory is never
reached (no prepare is advanced).
"""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.models import User
from . import staff_console as console, staff_escalation as escalation, staff_operation_actions as actions
from . import website_prepare_setup_cap as setup_cap
from .founder_journey_testing import _connect, make_history_free_org
from .founder_stuck_alerts import StuckCondition
from .website_models import WebsiteConnectionOperation

CODE = "article_surface_unresolved"


@override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=True, APP_RELEASE="build-f10a")
class OperationActionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.built = make_history_free_org()
        self.org, self.config = self.built.organization, self.built.config
        self.connection, _ = _connect(self.org, self.config, repo="example-staff/website")
        self.connection.authorized_by = self.built.owner
        self.connection.save(update_fields=["authorized_by"])
        self.config.refresh_from_db()
        self.staff = User.objects.create_superuser(email=uuid4().hex + "@staff.example.test")
        # No prepare step ever runs in these tests: nothing reaches Content Factory or GitHub.
        self.enterContext(patch("content_factory.website_prepare.advance_prepare",
                                side_effect=AssertionError("no prepare step in tests")))

    def stopped(self, *, state="needs_attention", receipt=None, code=CODE):
        return WebsiteConnectionOperation.objects.create(
            connection=self.connection, generation=self.connection.generation, action="prepare", state=state,
            idempotency_key=f"{self.connection.pk}:prepare:{self.connection.generation}",
            payload={"company_id": str(self.built.company.pk), "attempt": 1, "user_clicks": 1,
                     "requested_by_user_id": str(self.built.owner.pk)},
            receipt={"status": "stopped", "reason_code": code, "current_step": "setup", "scan_run_id": "scan-1",
                     "setup_run_id": "setup-1", "completed_steps": ["verify-access", "scan"],
                     "message": "We could not find the articles page.", **(receipt or {})})

    def act(self, op, action, key=None, **extra):
        return console.perform_action(self.org.pk, {"action": action, "operationId": str(op.pk),
            "idempotencyKey": key or uuid4().hex, "note": "Fix shipped for the articles page.", **extra},
            by_user=self.staff)

    def trail(self, op):
        op.refresh_from_db()
        return op.receipt.get(actions.AUDIT_KEY, [])

    # -- Idempotent and audited ---------------------------------------------------------

    def test_restart_is_idempotent_and_audited(self):
        op = self.stopped()
        key = uuid4().hex
        status, body, replayed = self.act(op, "restart_prepare", key=key)
        self.assertEqual((status, body["status"], replayed), (200, "done", False), body)
        op.refresh_from_db()
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt["reason_code"], "")
        # The founder's own Try again: one more attempt, under the founder who authorised the website.
        self.assertEqual(op.payload["attempt"], 2)
        self.assertEqual(op.payload["requested_by_user_id"], str(self.built.owner.pk))
        before = (op.state, op.receipt, op.payload, op.updated_at)
        again_status, again, again_replayed = self.act(op, "restart_prepare", key=key)
        self.assertEqual((again_status, again, again_replayed), (status, body, True))
        op.refresh_from_db()
        self.assertEqual((op.state, op.receipt, op.payload, op.updated_at), before)
        trail = self.trail(op)
        self.assertEqual(len(trail), 1)
        row = trail[0]
        self.assertEqual((row["action"], row["by_user_id"], row["outcome"], row["idempotency_key"]),
                         ("restart_prepare", self.staff.pk, "done", key))
        self.assertEqual(row["note"], "Fix shipped for the articles page.")
        self.assertEqual((row["before"]["state"], row["before"]["stopCode"]), ("needs_attention", CODE))
        self.assertEqual(row["after"]["state"], "pending")
        self.assertTrue(row["at"])

    def test_a_note_and_a_known_operation_are_required(self):
        op = self.stopped()
        with self.assertRaises(ValueError):
            console.perform_action(self.org.pk, {"action": "restart_prepare", "operationId": str(op.pk),
                                                 "idempotencyKey": uuid4().hex}, by_user=self.staff)
        with self.assertRaises(WebsiteConnectionOperation.DoesNotExist):
            console.perform_action(self.org.pk, {"action": "restart_prepare", "operationId": "not-a-uuid", "note": "x",
                                                 "idempotencyKey": uuid4().hex}, by_user=self.staff)
        other = make_history_free_org()
        with self.assertRaises(WebsiteConnectionOperation.DoesNotExist):
            console.perform_action(other.organization.pk, {"action": "restart_prepare", "operationId": str(op.pk),
                                                           "note": "x", "idempotencyKey": uuid4().hex}, by_user=self.staff)

    def test_staff_gate_holds(self):
        op = self.stopped()
        from community_chat.admin_access import AdminError
        with self.assertRaises(AdminError):
            console.perform_action(self.org.pk, {"action": "restart_prepare", "operationId": str(op.pk), "note": "x",
                                                 "idempotencyKey": uuid4().hex}, by_user=self.built.owner)
        with override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=False), self.assertRaises(AdminError):
            self.act(op, "restart_prepare")

    def test_rescan_reads_the_source_again_and_is_audited(self):
        op = self.stopped()
        status, body, _ = self.act(op, "rescan")
        self.assertEqual(body["status"], "done", body)
        op.refresh_from_db()
        self.assertEqual((op.state, op.receipt["current_step"]), ("pending", "scan"))
        self.assertTrue(op.payload["source_reverify"])
        self.assertNotIn("scan_run_id", op.receipt)
        self.assertEqual(self.trail(op)[-1]["action"], "rescan")

    def test_reset_setup_starts_the_setup_over_from_a_fresh_scan(self):
        op = self.stopped()
        status, body, _ = self.act(op, "reset_setup")
        self.assertEqual(body["status"], "done", body)
        op.refresh_from_db()
        self.assertEqual(op.state, "pending")
        for key in actions.SETUP_PROGRESS_KEYS:
            self.assertNotIn(key, op.receipt)
        self.assertEqual(op.receipt["current_step"], "scan")
        self.assertEqual(self.trail(op)[-1]["outcome"], "done")

    def test_replay_on_the_current_build_once(self):
        op = self.stopped()
        status, body, _ = self.act(op, "replay_current_build")
        self.assertEqual(body["status"], "done", body)
        op.refresh_from_db()
        self.assertEqual(op.receipt["replay_v1"]["keys"], [f"{CODE}@build:build-f10a"])
        # The same stop on the same build is not replayed twice, whatever the key.
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(
            state="needs_attention", receipt={**op.receipt, "reason_code": CODE})
        op.refresh_from_db()
        self.assertNotIn("replay_current_build", actions.offered(op)["actions"])
        _, again, _ = self.act(op, "replay_current_build")
        self.assertEqual((again["status"], again["reason"]), ("refused", "already_replayed"))
        op.refresh_from_db()
        self.assertEqual(op.state, "needs_attention")
        self.assertEqual([row["outcome"] for row in self.trail(op)], ["done", "refused"])
        # A new build may replay it again.
        with override_settings(APP_RELEASE="build-f10b"):
            self.assertIn("replay_current_build", actions.offered(op)["actions"])

    # -- Inside consent ---------------------------------------------------------------

    def test_never_approves_or_merges_a_setup_pull_request(self):
        op = self.stopped(state="needs_user", receipt={"user_action": {"id": "approve_setup"}})
        strategy = dict(self.config.pillar_strategy or {})
        for action in ("approve_setup", "merge_setup_pr", "merge_pr"):
            with self.subTest(action=action):
                status, body, _ = self.act(op, action)
                self.assertEqual((status, body["status"], body["reason"]), (200, "refused", "founder_consent_required"))
        op.refresh_from_db()
        self.assertEqual((op.state, op.receipt["user_action"]), ("needs_user", {"id": "approve_setup"}))
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy or {}, strategy)
        self.assertEqual([row["reason"] for row in self.trail(op)], ["founder_consent_required"] * 3)
        # Restarting cannot route around the founder's approval either: it is their turn.
        _, body, _ = self.act(op, "restart_prepare")
        self.assertEqual(body["reason"], "founder_turn")
        self.assertEqual(actions.offered(op)["actions"], [])
        op.refresh_from_db()
        self.assertEqual(op.state, "needs_user")

    def test_consent_billing_and_ownership_actions_are_not_offered(self):
        op = self.stopped()
        for action in sorted(actions.NOT_OFFERED):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.act(op, action)

    def test_a_revoked_or_superseded_preparation_is_refused(self):
        op = self.stopped()
        self.connection.__class__.objects.filter(pk=self.connection.pk).update(state="disconnected")
        op.refresh_from_db()
        _, body, _ = self.act(op, "restart_prepare")
        self.assertEqual(body["reason"], "not_current_connected_generation")
        op.refresh_from_db()
        self.assertEqual(op.state, "needs_attention")

    # -- Caps ---------------------------------------------------------------------------

    def test_setup_cap_holds_unless_reset_and_the_reset_has_its_own_cap(self):
        op = self.stopped(receipt={setup_cap.RECEIPT_KEY: ["s1", "s2", "s3"],
                                   setup_cap.HELD_KEY: {"limit": 3, "dispatched": 3}})
        offered = actions.offered(op)
        self.assertNotIn("restart_prepare", offered["actions"])
        self.assertEqual(offered["held"]["restart_prepare"], "setup_cap")
        self.assertEqual(offered["capReset"], {"available": True, "reason": None, "used": 0,
                                               "limit": actions.CAP_RESET_LIMIT})
        _, body, _ = self.act(op, "restart_prepare")
        self.assertEqual(body["reason"], "setup_cap")
        _, body, _ = self.act(op, "reset_setup")
        self.assertEqual(body["reason"], "setup_cap")
        _, body, _ = self.act(op, "restart_prepare", resetAttemptCap=True)
        self.assertEqual(body["status"], "done", body)
        op.refresh_from_db()
        self.assertEqual(op.state, "pending")
        self.assertNotIn(setup_cap.HELD_KEY, op.receipt)
        self.assertEqual(op.receipt.get(setup_cap.RECEIPT_KEY, []), [])
        resets = op.receipt[actions.CAP_RESET_KEY]
        self.assertEqual((len(resets), resets[0]["by_user_id"], resets[0]["setup_dispatch_run_ids"]),
                         (1, self.staff.pk, ["s1", "s2", "s3"]))
        self.assertTrue(self.trail(op)[-1]["reset_attempt_cap"])
        # The cap is reached again: a second reset is over the staff reset cap.
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(state="needs_attention", receipt={
            **op.receipt, "reason_code": setup_cap.CODE, setup_cap.RECEIPT_KEY: ["s4", "s5", "s6"],
            setup_cap.HELD_KEY: {"limit": 3, "dispatched": 3}})
        op.refresh_from_db()
        self.assertEqual(actions.offered(op)["capReset"]["reason"], "cap_reset_limit")
        _, body, _ = self.act(op, "restart_prepare", resetAttemptCap=True)
        self.assertEqual((body["status"], body["reason"]), ("refused", "cap_reset_limit"))
        op.refresh_from_db()
        self.assertEqual((op.state, op.receipt[setup_cap.RECEIPT_KEY]), ("needs_attention", ["s4", "s5", "s6"]))

    def test_a_refused_reopen_keeps_the_cap_reset_unspent(self):
        op = self.stopped(receipt={setup_cap.RECEIPT_KEY: ["s1", "s2", "s3"]})
        from .website_contract import WebsiteAuthorityError
        with patch("content_factory.website_prepare.start_prepare",
                   side_effect=WebsiteAuthorityError("website_prepare_blocked", "Refused.")):
            _, body, _ = self.act(op, "restart_prepare", resetAttemptCap=True)
        self.assertEqual(body["reason"], "refused:website_prepare_blocked")
        op.refresh_from_db()
        self.assertEqual(op.receipt[setup_cap.RECEIPT_KEY], ["s1", "s2", "s3"])
        self.assertNotIn(actions.CAP_RESET_KEY, op.receipt)

    def test_pull_request_cap_holds_every_action(self):
        op = self.stopped()
        with patch("content_factory.failure_policy_engine.pull_request_cap_reached", return_value=True):
            self.assertEqual(actions.offered(op)["actions"], [])
            _, body, _ = self.act(op, "restart_prepare", resetAttemptCap=True)
        self.assertEqual(body["reason"], "pull_request_cap")

    # -- Staff console ------------------------------------------------------------------

    def test_detail_offers_the_actions_and_the_audit_trail(self):
        op = self.stopped()
        self.act(op, "rescan")
        detail = console.startup_detail(self.org.pk)
        summary = detail["website"]["operation"]
        self.assertEqual(summary["id"], str(op.pk))
        self.assertEqual(summary["state"], "pending")
        self.assertEqual(summary["actions"], [])  # a working preparation offers nothing
        self.assertEqual([row["action"] for row in summary["staffActions"]], ["rescan"])
        self.assertIn("escalation", detail)


class EscalationLadderTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()

    def condition(self, *, kind="operation_stopped", code=CODE, age=timedelta(hours=1), **details):
        return StuckCondition(organization_id=1, slot="op-1-stopped", kind=kind, step="prepare", code=code,
                              stuck_since=self.now - age, details=details)

    def test_generic_or_internal_stop_alerts_at_once_then_the_owner_at_48_hours(self):
        state = escalation.ladder(now=self.now, stuck=[self.condition(kind="run_stopped", code="UNCLASSIFIED")])
        self.assertEqual((state["kind"], state["rung"], state["next"]["id"]), ("generic_or_internal_stop", "staff_alert", "owner"))
        self.assertEqual(state["waitingOn"], "mlai")
        late = escalation.ladder(now=self.now, stuck=[self.condition(code="", age=timedelta(hours=49))])
        self.assertEqual((late["rung"], late["next"]), ("owner", None))

    def test_failed_run_reaches_staff_at_two_hours(self):
        with patch("content_factory.no_generic_stop.generic_stop_reason", return_value=None):
            early = escalation.ladder(now=self.now, stuck=[self.condition(kind="run_stopped", age=timedelta(minutes=90))])
            late = escalation.ladder(now=self.now, stuck=[self.condition(kind="run_stopped", age=timedelta(hours=3))])
        self.assertEqual((early["kind"], early["rung"], early["next"]["id"]), ("failed_run", None, "staff_alert"))
        self.assertEqual(late["rung"], "staff_alert")
        from .founder_stuck_alerts import RUN_STUCK_AFTER
        self.assertEqual(RUN_STUCK_AFTER, escalation.FAILED_RUN_AFTER)

    def test_unanswered_ask_rides_the_e7_ladder(self):
        state = escalation.ladder(now=self.now, ask="describe_customer", ask_since=self.now - timedelta(hours=25))
        self.assertEqual((state["kind"], state["waitingOn"], state["rung"]), ("unanswered_ask", "founder", "founder_reminder_24h"))
        self.assertEqual({rung["sender"] for rung in state["rungs"]}, {"ask_ladder"})
        self.assertEqual(state["next"]["id"], "founder_reminder_72h")
        self.assertIsNone(escalation.ladder(now=self.now, ask="describe_customer", ask_since=None))

    def test_staff_fixing_has_a_bounded_life(self):
        since = self.now - timedelta(hours=80)
        state = escalation.ladder(now=self.now, stuck=[self.condition(age=timedelta(hours=80),
                                                                      staff_fixing_since=since.isoformat())])
        self.assertEqual(state["kind"], "staff_fixing")
        rungs = {rung["id"]: rung for rung in state["rungs"]}
        self.assertTrue(rungs["staff_owner_named"]["reached"])
        self.assertFalse(rungs["staff_owner_named"]["done"])
        self.assertTrue(rungs["founder_update"]["reached"])
        self.assertEqual(state["next"]["id"], "day_7_exit")

    def test_owner_escalation_is_once_within_the_horizon_and_never_for_staff_fixing(self):
        due = escalation.owner_escalations([
            self.condition(age=timedelta(hours=47)), self.condition(age=timedelta(hours=50)),
            self.condition(age=timedelta(days=30)),
            self.condition(age=timedelta(hours=50), staff_fixing_since=self.now.isoformat())], now=self.now)
        self.assertEqual(len(due), 1)
        self.assertEqual((due[0].slot, due[0].details["escalation"]), ("op-1-stopped-owner", "owner"))

    def test_ask_since_reads_e7_notices(self):
        class Config:
            pillar_strategy = {"founder_notices": {"last": {
                "website:1": {"state": "your_turn", "ask": "approve_setup", "at": "2026-11-01T00:00:00+00:00"},
                "article:2": {"state": "your_turn", "ask": "review_draft", "at": "2026-11-02T00:00:00+00:00"}}}}
        self.assertEqual(escalation.ask_since(Config, "approve_setup").isoformat(), "2026-11-01T00:00:00+00:00")
        self.assertIsNone(escalation.ask_since(Config, "agree_terms"))
