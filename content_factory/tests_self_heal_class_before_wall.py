"""Self-heal reads the class before the wall, keeps the original code, and alerts once (B1, B2, B5, B6).

- B1: one audience test (``is_scheduler_run``, now also the scheduler principal)
  for the engine and every alert producer; candidates stop at the alerts'
  history horizon; the wall counts from the run's own failure; class before
  wall: a founder ask waits with no wall, a transient item still stops at the
  wall; a refunded run is terminal with "start again" (D16).
- B2: a capped stop never overwrites ``error_code``; the stop is recorded under
  ``self_heal_v1.stop`` and the alert carries both codes.
- B5: ``self_heal_capped`` and ``run_terminal`` share one slot per (workflow,
  original code): nine failed runs with one code give one alert.
- B6: nightly scheduler runs alert only through the digest: three consecutive
  failed nights give one alert, two give none.
"""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from . import failure_policy_engine as engine, founder_journey_alerts as alerts
from . import background_job_digest as digest, no_delivery_refunds as refunds
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_run

CHANNEL = "C0000000000"
SLACK = "integrations.services.slack.SlackService.get_client"
FOUNDER = {"request_source": "roo_slackbot"}
SCHEDULER = {"request_source": "mlai_backend_scheduler", "include_expansion": False}
NEEDS_USER = "AUTH_REQUIRED"
TRANSIENT = "REVIEW_PROVIDER_UNAVAILABLE"
UNSUPPORTED = "APPLICATION_ROOT_NOT_FOUND"
EXECUTE_ACTION = "content_factory.vibe_marketing_views._call_content_factory_run_action"


def acknowledged(**kwargs):
    return {"ok": True, "ts": f"1700000000.{kwargs['client_msg_id'][:6]}"}


class SelfHealFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        rollout = override_settings(**{
            **ALL_FLAGS_ON,
            "ARTICLES_SELF_HEAL_CLASS_BEFORE_WALL_ENABLED": True,
            "ARTICLES_SELF_HEAL_PRESERVE_CODE_ENABLED": True,
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED": True,
            "ARTICLES_STAFF_ALERT_RULES_ENABLED": True,
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE": (self.now - timedelta(days=60)).date().isoformat(),
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK": 50,
            "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID": CHANNEL,
            "DEFAULT_BACKEND_URL": "https://api.example.test",
        })
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.fixture = make_p2_org()
        self.org = self.fixture.organization
        OrganizationContentConfig.objects.get_or_create(organization=self.org)

    def make(self, code=TRANSIENT, *, workflow="article_generation", status="failed", request=None, result=None,
             age=timedelta(hours=2), failed_for=None):
        """A founder run that failed ``failed_for`` ago (default: when it was last updated)."""
        payload = {"error_code": code, **(result or {})} if code else dict(result or {})
        run = make_run(self.org, stamped=True, workflow=workflow, status=status,
                       run_request=FOUNDER if request is None else request, result=payload,
                       run_id=f"run-{uuid4().hex[:12]}")
        moment = self.now - (failed_for if failed_for is not None else age)
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=moment)
        run.refresh_from_db()
        return run

    def org_receipts(self):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy.get(alerts.RECEIPTS_KEY, {})

    def receipts_for(self, reason):
        return [receipt for receipt in self.org_receipts().values() if receipt.get("alert_reason") == reason]

    def founder_sweep(self, now=None):
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            outcome = alerts.run_founder_journey_alert_sweep(now=now or self.now)
        return outcome, [call.kwargs["text"] for call in client.return_value.chat_postMessage.call_args_list]

    def run_engine(self, run, now=None):
        now = now or self.now
        run.refresh_from_db()
        decision = engine.decide(run, now=now)
        with patch(EXECUTE_ACTION) as action, patch("content_factory.channel_switch.switch_to_copy_ready") as switch:
            engine.apply(run, decision, now=now)
        action.assert_not_called()
        switch.assert_not_called()
        run.refresh_from_db()
        return decision


class AudienceTests(SelfHealFixture):
    def test_a_scheduler_principal_is_background_work_whatever_the_request_says(self):
        principal = {"run_owner_v1": {"identity": {"principal": {"kind": "scheduler", "service": "island_refresh"}}}}
        admitted = self.make(workflow="island_refresh", request={"request_source": "roo_slackbot"}, result=principal)
        founder = self.make(workflow="island_refresh", request={"request_source": "roo_slackbot"},
                            result={"run_owner_v1": {"identity": {"principal": {"kind": "user", "user_id": "1"}}}})
        self.assertTrue(alerts.is_scheduler_run(admitted))
        self.assertFalse(alerts.is_scheduler_run(founder))
        founder_rows = set(ContentFactoryRun.objects.filter(alerts.founder_run_q()).values_list("pk", flat=True))
        scheduler_rows = set(ContentFactoryRun.objects.exclude(alerts.founder_run_q()).values_list("pk", flat=True))
        self.assertEqual((admitted.pk in founder_rows, admitted.pk in scheduler_rows), (False, True))
        self.assertEqual((founder.pk in founder_rows, founder.pk in scheduler_rows), (True, False))
        self.assertNotIn(admitted.pk, {run.pk for run in engine._candidates(self.now, 500)})
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=admitted)
        self.assertEqual(self.org_receipts(), {})

    def test_candidates_stop_at_the_alerts_history_horizon(self):
        old = self.make(age=timedelta(days=61))
        recent = self.make(age=timedelta(days=59))
        candidates = {run.pk for run in engine._candidates(self.now, 500)}
        self.assertNotIn(old.pk, candidates)
        self.assertIn(recent.pk, candidates)
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE="not-a-date"):
            self.assertEqual(engine._candidates(self.now, 500), [])

    def test_the_wall_counts_from_the_runs_own_failure_not_the_first_sweep(self):
        run = self.make(failed_for=timedelta(hours=49), age=timedelta(hours=50))
        decision = engine.decide(run, now=self.now)
        self.assertEqual((decision.action, decision.cap), ("stop", "wall"))
        # A failed step's end is the failure time when the run has one.
        stepped = self.make(failed_for=timedelta(minutes=5), age=timedelta(hours=50))
        ContentFactoryRunStep.objects.create(run=stepped, step_key="write_article", status="failed",
                                             completed_at=self.now - timedelta(hours=30))
        self.assertEqual(engine.run_failed_at(stepped), self.now - timedelta(hours=30))
        self.assertEqual(engine.decide(stepped, now=self.now).cap, "transient_elapsed")
        # A freshly failed run is retried, and its receipt keeps the run's own failure time.
        fresh = self.make(failed_for=timedelta(minutes=5))
        decision = engine.decide(fresh, now=self.now)
        self.assertEqual(decision.action, "retry")
        engine.apply(fresh, decision, now=self.now)
        fresh.refresh_from_db()
        self.assertEqual(fresh.result["self_heal_v1"]["first_failed_at"], (self.now - timedelta(minutes=5)).isoformat())


class ClassBeforeWallTests(SelfHealFixture):
    def test_needs_user_at_49_hours_stays_await_owner_with_no_wall(self):
        run = self.make(NEEDS_USER, failed_for=timedelta(hours=49), age=timedelta(hours=49))
        decision = self.run_engine(run)
        self.assertEqual((decision.action, decision.reason, decision.cap), ("await_owner", "owner", None))
        receipt = run.result["self_heal_v1"]
        self.assertEqual(receipt["last_action"], "await_owner")
        self.assertFalse(receipt["capped"])
        self.assertEqual(receipt["next_at"], (self.now + engine.AWAIT_OWNER_RECHECK).isoformat())
        self.assertEqual(run.result["error_code"], NEEDS_USER)
        self.assertEqual(self.receipts_for("self_heal_capped"), [])
        # A week later it still waits for the founder.
        later = self.now + timedelta(days=7)
        self.assertEqual(engine.decide(run, now=later).action, "await_owner")

    def test_transient_at_49_hours_still_stops_at_the_wall(self):
        run = self.make(TRANSIENT, failed_for=timedelta(hours=49), age=timedelta(hours=49))
        decision = engine.decide(run, now=self.now)
        self.assertEqual((decision.action, decision.reason, decision.cap), ("stop", "wall", "wall"))

    def test_unsupported_and_unregistered_stop_at_once_without_a_cap(self):
        for code in (UNSUPPORTED, "NOT_A_REGISTERED_CODE_X"):
            with self.subTest(code=code):
                run = self.make(code, failed_for=timedelta(minutes=5))
                decision = engine.decide(run, now=self.now)
                self.assertEqual((decision.action, decision.reason, decision.cap), ("stop", "terminal", None))

    def test_an_action_still_in_flight_settles_before_the_class_decides(self):
        pending = {"self_heal_v1": {"version": 1, "mode": "policy", "attempts": 1, "pending_action": {
            "state": "uncertain", "claim": "c", "action": "retry", "attempt": 1, "cap": None, "reason": "backoff",
            "lease_until": (self.now - timedelta(minutes=1)).isoformat()}}}
        for code in (UNSUPPORTED, NEEDS_USER):
            with self.subTest(code=code):
                run = self.make(code, failed_for=timedelta(minutes=5), result=pending)
                decision = engine.decide(run, now=self.now)
                self.assertEqual((decision.action, decision.attempt), ("retry", 1))

    def test_flag_off_keeps_the_wall_first(self):
        run = self.make(NEEDS_USER, failed_for=timedelta(hours=49), age=timedelta(hours=49))
        with override_settings(ARTICLES_SELF_HEAL_CLASS_BEFORE_WALL_ENABLED=False):
            self.assertEqual(engine.decide(run, now=self.now).cap, "wall")

    def test_class_table_over_every_registry_class(self):
        rows = (
            ("needs_user", NEEDS_USER, ("await_owner", None), ("await_owner", None)),
            ("unsupported", UNSUPPORTED, ("stop", None), ("stop", None)),
            ("unregistered", "NOT_A_REGISTERED_CODE_X", ("stop", None), ("stop", None)),
            ("transient", TRANSIENT, ("retry", None), ("stop", "wall")),
            ("waiting", "HOSTED_PROXY_UNVERIFIED", ("observe", None), ("stop", "wall")),
            ("repairable", "EDITORIAL_REJECTED", ("resume", None), ("stop", "wall")),
        )
        for label, code, fresh, walled in rows:
            with self.subTest(label):
                run = self.make(code, failed_for=timedelta(minutes=1))
                decision = engine.decide(run, now=self.now)
                self.assertEqual((decision.action, decision.cap), fresh)
                old = self.make(code, failed_for=timedelta(hours=49), age=timedelta(hours=49))
                decision = engine.decide(old, now=self.now)
                self.assertEqual((decision.action, decision.cap), walled)


@override_settings(ARTICLES_REGISTRY_REFUNDS_ENABLED=True)
class RefundedRunTests(SelfHealFixture):
    def paid(self, code=NEEDS_USER, age=timedelta(hours=49)):
        from integrations.services.article_generation import _ledger_reference_id
        from . import dispatch_outbox
        PointsService.credit_purchased_topup(user=self.fixture.owner, delta=100, description="Fixture funding",
                                             idempotency_key="fund-" + uuid4().hex)
        key = uuid4().hex
        spend, _ = PointsService.spend(user=self.fixture.owner, delta=12, source="CONTENT_FACTORY",
            description="Fixture article", created_by_slack_id="fixture",
            idempotency_key="content_factory:charge:" + key, reference_type="CONTENT_FACTORY",
            reference_id=_ledger_reference_id(key))
        request = {**FOUNDER, "client_request_id": key, "domain": self.org.domain, "roo_points_ledger_id": spend.pk,
                   "roo_points_cost": 12, "roo_points_billing_user_id": self.fixture.owner.pk,
                   "roo_points_billing_status": "charged"}
        with transaction.atomic():
            dispatch_outbox.reserve_dispatch(organization=self.org, payload=request, billing_user_id=self.fixture.owner.pk)
        run = ContentFactoryRun.objects.create(run_id="run-" + uuid4().hex, organization=self.org, domain=self.org.domain,
            workflow="article_generation", status="failed", run_request=request, result={"error_code": code})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run, spend

    def test_refund_sweep_still_refunds_a_needs_user_run_at_48_hours(self):
        run, spend = self.paid()
        self.run_engine(run)
        self.assertEqual(run.result["self_heal_v1"]["last_action"], "await_owner")
        outcome = refunds.apply_refund_decision(run, refunds.refund_decision(run, now=self.now), now=self.now)
        self.assertEqual(outcome["state"], "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)
        # Before the 48 hours the awaited ask keeps its charge.
        young, _ = self.paid(age=timedelta(hours=2))
        self.run_engine(young)
        self.assertEqual(refunds.refund_decision(young, now=self.now)["why"], "retry_pending")

    def refunded(self):
        run, _ = self.paid()
        self.run_engine(run)
        refunds.apply_refund_decision(run, refunds.refund_decision(run, now=self.now), now=self.now)
        run.refresh_from_db()
        self.assertEqual(run.result["points_v1"]["state"], "refunded")
        return run

    def test_a_refunded_run_is_terminal_for_self_heal(self):
        run = self.refunded()
        decision = engine.decide(run, now=self.now + timedelta(days=1))
        self.assertEqual((decision.action, decision.reason), ("none", "terminal"))
        engine.apply(run, decision, now=self.now + timedelta(days=1))
        run.refresh_from_db()
        self.assertEqual(run.result["self_heal_v1"]["last_action"], "none")
        self.assertNotIn(run.pk, {row.pk for row in engine._candidates(self.now + timedelta(days=2), 500)})

    def test_founder_resume_of_a_refunded_run_is_refused(self):
        run = self.refunded()
        from .vibe_marketing_views import control_article_run
        with patch(EXECUTE_ACTION) as action:
            response = control_article_run(user=self.fixture.owner, context=None, run=run, action="resume", data={})
        action.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "run_refunded_start_again")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.result["points_v1"]["state"], "refunded")

    def test_the_founders_ask_becomes_start_again(self):
        from .founder_journey_p2 import run_decision
        run = self.refunded()
        decision = run_decision(self.org, run, config=None, user=self.fixture.owner, now=self.now)
        self.assertEqual(decision["copyId"], "stopped")
        self.assertIsNone(decision["action"])
        self.assertEqual(decision["next"]["kind"], "write_article")
        self.assertIn("Refunded 12 points", decision["body"])


class PreserveCodeTests(SelfHealFixture):
    def capped(self, code=TRANSIENT):
        run = self.make(code, failed_for=timedelta(hours=49), age=timedelta(hours=49))
        with patch("content_factory.no_delivery_refunds.apply_refund_decision", return_value={}):
            decision = self.run_engine(run)
        self.assertEqual((decision.action, decision.cap), ("stop", "wall"))
        return run

    def test_a_capped_stop_keeps_the_original_code_and_records_its_own_stop(self):
        run = self.capped()
        self.assertEqual(run.result["error_code"], TRANSIENT)
        stop = run.result["self_heal_v1"]["stop"]
        self.assertEqual((stop["code"], stop["cap"]), ("SELF_HEAL_DEADLINE_REACHED", "wall"))
        self.assertTrue(run.result["self_heal_v1"]["capped"])
        self.assertEqual(run.result["self_heal_v1"]["last_action"], "stop")

    def test_the_alert_shows_both_codes(self):
        run = self.capped()
        [receipt] = self.receipts_for("self_heal_capped")
        self.assertEqual(receipt["details"]["original_code"], TRANSIENT)
        self.assertEqual(receipt["details"]["stop_code"], "SELF_HEAL_DEADLINE_REACHED")
        self.assertEqual(receipt["details"]["cap"], "wall")
        self.assertEqual(receipt["details"]["failure_class"], "transient")
        _, texts = self.founder_sweep()
        [text] = [text for text in texts if "self_heal_capped" in text]
        self.assertIn(f"Code {TRANSIENT}.", text)
        self.assertIn("stop_code=SELF_HEAL_DEADLINE_REACHED", text)
        self.assertIn(run.run_id, text)

    def test_no_second_stuck_alert_after_a_capped_stop(self):
        from . import founder_stuck_alerts as stuck
        run = self.capped()
        scope = stuck.Scope(organization_ids=[self.org.pk], run_stuck_after=timedelta(0), lookback=None, limit=None)
        found = {condition.details["run_id"] for condition in stuck._stopped_runs(now=self.now, scope=scope)}
        self.assertNotIn(run.run_id, found)

    def test_a_needs_user_original_still_reads_as_the_founders_turn(self):
        from . import founder_stuck_alerts as stuck
        with override_settings(ARTICLES_SELF_HEAL_CLASS_BEFORE_WALL_ENABLED=False):
            run = self.capped(NEEDS_USER)
        self.assertEqual(run.result["error_code"], NEEDS_USER)
        self.assertTrue(stuck._founder_has_next_action({}, NEEDS_USER))

    def test_flag_off_keeps_the_old_recode(self):
        with override_settings(ARTICLES_SELF_HEAL_PRESERVE_CODE_ENABLED=False):
            run = self.capped()
        self.assertEqual(run.result["error_code"], "SELF_HEAL_DEADLINE_REACHED")
        self.assertEqual(alerts.original_failure_code(run.result), TRANSIENT)


class IncidentSlotTests(SelfHealFixture):
    def test_nine_failed_runs_with_one_code_give_one_alert(self):
        runs = [self.make("PUBLISH_MERGE_FAILED", age=timedelta(days=day, hours=2)) for day in range(9)]
        outcome, texts = self.founder_sweep()
        self.assertEqual(outcome["queued_by_reason"]["run_terminal"], 1)
        self.assertEqual(len([text for text in texts if "(run_terminal)" in text]), 1)
        self.assertEqual(len(self.receipts_for("run_terminal")), 1)
        # Every duplicate holds the shared receipt, so later ticks do not rescan them.
        for run in runs:
            run.refresh_from_db()
            self.assertIn("run_terminal", run.result.get(alerts.RECEIPTS_KEY, {}))
        outcome, texts = self.founder_sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual((outcome["queued"], texts), (0, []))
        # Another workflow or another code is another alert.
        self.make("PUBLISH_MERGE_FAILED", workflow="article_revision")
        self.make("REVIEW_PROVIDER_UNAVAILABLE")
        outcome, _ = self.founder_sweep(now=self.now + timedelta(minutes=20))
        self.assertEqual(outcome["queued_by_reason"]["run_terminal"], 2)

    def test_self_heal_capped_shares_the_slot_and_sees_through_a_recode(self):
        first = self.make(TRANSIENT, failed_for=timedelta(hours=49), age=timedelta(hours=49))
        recoded = self.make("SELF_HEAL_DEADLINE_REACHED", result={"self_heal_v1": {"code": TRANSIENT}})
        self.assertEqual(alerts.run_incident_slot(first), alerts.run_incident_slot(recoded))
        for run in (first, recoded):
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="self_heal_capped", run=run)
        self.assertEqual(len(self.receipts_for("self_heal_capped")), 1)

    def test_a_codeless_run_keeps_its_own_alert(self):
        one, two = self.make(None), self.make(None)
        self.assertIsNone(alerts.run_incident_slot(one))
        for run in (one, two):
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=run)
        self.assertEqual(len(self.receipts_for("run_terminal")), 2)


class BackgroundDigestTests(SelfHealFixture):
    def nightly(self, nights_ago, *, status="failed", workflow="island_refresh", org=None):
        organization = org or self.org
        night = timezone.localtime(self.now, digest.LOCAL).replace(hour=2, minute=0, second=0, microsecond=0)
        moment = night - timedelta(days=nights_ago)
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", organization=organization,
            domain=organization.domain, workflow=workflow, status=status, run_request=SCHEDULER,
            result={"error_code": "NO_KEYWORDS"} if status == "failed" else {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=moment, updated_at=moment)
        return run

    def test_three_consecutive_failed_nights_give_one_alert(self):
        for nights_ago in (2, 1, 0):
            self.nightly(nights_ago)
        # Per-run alerts stay silent for background work.
        outcome, _ = self.founder_sweep()
        self.assertEqual(outcome["queued"], 0)
        result = digest.run_background_job_digest(now=self.now)
        self.assertEqual(result["queued"], 1)
        [receipt] = self.receipts_for("background_job_failing")
        self.assertEqual(receipt["details"]["nights"], 3)
        self.assertEqual(receipt["details"]["workflow"], "island_refresh")
        self.assertEqual(receipt["details"]["last_code"], "NO_KEYWORDS")
        _, texts = self.founder_sweep(now=self.now + timedelta(minutes=1))
        self.assertEqual(len([text for text in texts if "background_job_failing" in text]), 1)
        # A fourth and a ninth failed night continue the same streak: no second alert.
        self.assertEqual(digest.run_background_job_digest(now=self.now)["queued"], 0)
        self.nightly(-1)
        self.assertEqual(digest.run_background_job_digest(now=self.now + timedelta(days=1))["queued"], 0)
        for nights_ahead in range(2, 7):
            self.nightly(-nights_ahead)
        self.assertEqual(digest.run_background_job_digest(now=self.now + timedelta(days=6))["queued"], 0)
        # Past the lookback the streak's first night is cut off: still the same alert.
        for nights_ahead in range(7, 12):
            self.nightly(-nights_ahead)
            self.assertEqual(digest.run_background_job_digest(
                now=self.now + timedelta(days=nights_ahead))["queued"], 0)
        self.assertEqual(len(self.receipts_for("background_job_failing")), 1)

    def test_two_failed_nights_give_none(self):
        self.nightly(1)
        self.nightly(0)
        self.assertEqual(digest.run_background_job_digest(now=self.now), {"streaks": 0, "queued": 0, "spikes": 0})
        self.assertEqual(self.receipts_for("background_job_failing"), [])

    def test_a_completed_night_or_a_gap_breaks_the_streak(self):
        self.nightly(3)
        self.nightly(2)
        self.nightly(1, status="completed")
        self.nightly(0)
        self.assertEqual(digest.run_background_job_digest(now=self.now)["queued"], 0)
        other = make_p2_org().organization
        for nights_ago in (4, 3, 1, 0):
            self.nightly(nights_ago, org=other)
        self.assertEqual(digest.run_background_job_digest(now=self.now)["queued"], 0)

    def test_founder_runs_never_count_as_nightly(self):
        for nights_ago in (2, 1, 0):
            run = self.nightly(nights_ago)
            ContentFactoryRun.objects.filter(pk=run.pk).update(run_request={"request_source": "roo_slackbot"})
        self.assertEqual(digest.run_background_job_digest(now=self.now)["streaks"], 0)

    def test_five_percent_of_startups_failing_in_one_night_is_one_spike(self):
        orgs = [self.org] + [make_p2_org().organization for _ in range(3)]
        for organization in orgs:
            self.nightly(0, org=organization)
        self.assertEqual(digest.run_background_job_digest(now=self.now)["spikes"], 1)
        self.assertEqual(digest.run_background_job_digest(now=self.now)["spikes"], 0)
        from .staff_alert_rules import global_alerts
        spikes = [row for row in global_alerts(now=self.now) if row["alert_reason"] == digest.SPIKE_REASON]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["details"]["failing_orgs"], 4)

    def test_a_startup_that_also_completed_that_night_is_not_failing(self):
        orgs = [self.org] + [make_p2_org().organization for _ in range(3)]
        for organization in orgs:
            self.nightly(0, org=organization)
            self.nightly(0, org=organization, status="completed")
        self.assertEqual(digest.run_background_job_digest(now=self.now)["spikes"], 0)

    def test_one_failing_startup_is_not_a_spike(self):
        self.nightly(0)
        self.assertEqual(digest.run_background_job_digest(now=self.now)["spikes"], 0)

    def test_the_self_heal_sweeps_run_the_digest_behind_the_staff_rules_switch(self):
        from .self_heal_sweeps import SWEEPS
        self.assertIn(("background_job_digest", "content_factory.background_job_digest", "run_background_job_digest_sweep"),
                      SWEEPS)
        for nights_ago in (2, 1, 0):
            self.nightly(nights_ago)
        with override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=False):
            self.assertEqual(digest.run_background_job_digest_sweep(now=self.now), {"status": "disabled"})
        self.assertEqual(self.receipts_for("background_job_failing"), [])
        self.assertEqual(digest.run_background_job_digest_sweep(now=self.now)["queued"], 1)
        self.assertEqual(len(self.receipts_for("background_job_failing")), 1)
