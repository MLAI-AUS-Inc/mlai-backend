"""Background work is decided by request source, and alerts lead with the blocking code (A2, A3).

Org 175 (Nov 2026): nine failed nightly ``island_refresh`` runs were swept by
self-heal, recoded, alerted, and some ran ``advance_rung`` (the org's strategy
budget and a CF channel-switch resume). Founder-paid island research runs as the
same workflow with a founder request source, and was silent in every sweep.
La Vie (pk 5396 shape): an ``approval_required`` run held for quality review also
carried ``website_connection_required``; the alert named only the quality hold.
"""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import channel_switch, failure_policy_engine as engine, founder_journey_alerts as alerts
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_run

CHANNEL = "C0000000000"
SLACK = "integrations.services.slack.SlackService.get_client"
SCHEDULER = {"request_source": "mlai_backend_scheduler", "include_expansion": False}
FOUNDER_RESEARCH = {"request_source": "roo_slackbot", "client_request_id": "island-research:" + "a" * 64,
                    "island_research_brief": {"topic": "x"}}
# A registered advance_strategy code: the policy that runs advance_rung.
STRATEGY_CODE = "PUBLISHING_ADAPTER_REQUIRED"


def acknowledged(**kwargs):
    return {"ok": True, "ts": f"1700000000.{kwargs['client_msg_id'][:6]}"}


class AudienceFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        rollout = override_settings(**{
            **ALL_FLAGS_ON,
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

    def make(self, *, workflow="island_refresh", status="failed", request=None, result=None, age=timedelta(hours=2)):
        run = make_run(self.org, stamped=True, workflow=workflow, status=status, run_request=request or {},
                       result=result or {}, run_id=f"run-{uuid4().hex[:12]}")
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run

    def bare_run(self, *, workflow="island_refresh", status="failed", request=None, result=None, age=timedelta(hours=2)):
        """A run saved before the scheduler stamped ``request_source`` (no P2 stamp either)."""
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", organization=self.org,
            domain=self.org.domain, workflow=workflow, status=status, run_request=request or {}, result=result or {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run

    def org_receipts(self):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy.get(alerts.RECEIPTS_KEY, {})

    def run_receipts(self, run):
        run.refresh_from_db()
        return run.result.get(alerts.RECEIPTS_KEY, {})

    def founder_sweep(self, now=None):
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            outcome = alerts.run_founder_journey_alert_sweep(now=now or self.now)
        return outcome, [call.kwargs["text"] for call in client.return_value.chat_postMessage.call_args_list]


class SchedulerAudienceTests(AudienceFixture):
    def test_the_audience_is_decided_by_request_source_only(self):
        cases = (
            ("stamped nightly refresh", self.make(request=SCHEDULER), True),
            ("scheduler source on another workflow", self.make(workflow="topic_discovery", request=SCHEDULER), True),
            ("founder-paid island research", self.make(request=FOUNDER_RESEARCH), False),
            ("founder article", self.make(workflow="article_generation", request={"request_source": "roo_slackbot"}), False),
            ("unstamped old nightly refresh", self.bare_run(), True),
            ("unstamped refresh with a research id", self.bare_run(request={"client_request_id": "island-research:x"}), False),
            ("unstamped article", self.bare_run(workflow="article_generation"), False),
            ("unstamped refresh with a null id", self.bare_run(request={"client_request_id": None}), True),
            ("stamped null source", self.make(request={"request_source": None}), False),
        )
        founder = set(ContentFactoryRun.objects.filter(alerts.founder_run_q()).values_list("pk", flat=True))
        # The quiesce command selects scheduler rows with exclude(): it must be the exact complement.
        scheduler_rows = set(ContentFactoryRun.objects.exclude(alerts.founder_run_q()).values_list("pk", flat=True))
        for label, run, scheduler in cases:
            with self.subTest(label):
                self.assertIs(alerts.is_scheduler_run(run), scheduler)
                self.assertIs(run.pk in founder, not scheduler)
                self.assertIs(run.pk in scheduler_rows, scheduler)

    def test_org_175_nine_failed_nights_produce_zero_receipts(self):
        nights = [self.make(request=SCHEDULER, result={"error_code": STRATEGY_CODE}, age=timedelta(days=day, hours=2))
                  for day in range(9)]
        old = self.bare_run(result={"error_code": STRATEGY_CODE}, age=timedelta(days=10))
        for run in [*nights, old]:
            for reason in ("run_terminal", "self_heal_capped", "codeless_terminal", "our_team_item"):
                alerts.queue_founder_journey_alert(organization=self.org, alert_reason=reason, run=run)
        batch = alerts.queue_founder_journey_alerts_batch(
            [{"organization": self.org, "alert_reason": "run_terminal", "run": run} for run in nights])
        self.assertEqual(batch, {"ids": [], "created": 0})
        outcome, texts = self.founder_sweep()
        from .staff_alert_rules import run_alert_rule_sweep
        run_alert_rule_sweep(now=self.now)
        self.assertEqual(outcome["queued"], 0)
        self.assertEqual(texts, [])
        self.assertEqual(self.org_receipts(), {})
        for run in [*nights, old]:
            self.assertEqual(self.run_receipts(run), {})

    def test_founder_started_research_failure_alerts_in_the_queue_and_the_sweeps(self):
        run = self.make(request=FOUNDER_RESEARCH)
        outcome, texts = self.founder_sweep()
        self.assertEqual(outcome["queued_by_reason"]["run_terminal"], 1)
        self.assertEqual(len(texts), 1)
        self.assertIn(run.run_id, texts[0])
        from .staff_alert_rules import run_alert_rule_sweep
        run_alert_rule_sweep(now=self.now)
        self.assertIn("codeless_terminal", self.run_receipts(run))
        queued = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="our_team_item", run=run)
        self.assertIn(queued, self.org_receipts())

    def test_the_stuck_sweep_uses_the_same_audience(self):
        from . import founder_stuck_alerts as stuck
        founder = self.make(workflow="article_generation", request={"request_source": "roo_slackbot"}, age=timedelta(days=3))
        nightly = self.make(workflow="article_generation", request=SCHEDULER, age=timedelta(days=3))
        scope = stuck.Scope(organization_ids=[self.org.pk], run_stuck_after=timedelta(0), lookback=None, limit=None)
        found = {condition.details["run_id"] for condition in stuck._stopped_runs(now=self.now, scope=scope)}
        self.assertIn(founder.run_id, found)
        self.assertNotIn(nightly.run_id, found)

    def test_hosted_quality_alert_skips_a_scheduler_article_run(self):
        from .hosted_quality_alerts import _held_review
        url = "https://preview.example.test/stories/held"
        result = {"generation": 0, "livePreview": {"previewUrl": url, "resumeGeneration": 0, "proof": {"commitSha": "a" * 40}},
                  "article_preview_quality": {"preview_url": url, "resume_generation": 0, "status": "blocking_findings",
                                              "self_heal_exhausted": True, "escalation": "quality_review"}}
        nightly = self.make(workflow="article_generation", status="approval_required", request=SCHEDULER, result=result)
        self.assertIsNone(_held_review(nightly))


class SelfHealAudienceTests(AudienceFixture):
    def test_nightly_runs_are_never_self_heal_candidates(self):
        stamped = self.make(request=SCHEDULER, result={"error_code": STRATEGY_CODE})
        unstamped = self.bare_run(result={"error_code": STRATEGY_CODE})
        founder = self.make(request=FOUNDER_RESEARCH, result={"error_code": STRATEGY_CODE})
        candidates = {run.pk for run in engine._candidates(self.now, 500)}
        self.assertNotIn(stamped.pk, candidates)
        self.assertNotIn(unstamped.pk, candidates)
        self.assertIn(founder.pk, candidates)

    def test_island_refresh_never_advances_a_rung_or_switches_channel(self):
        before = dict(OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy)
        research = self.make(request=FOUNDER_RESEARCH, result={"error_code": STRATEGY_CODE})
        decision = engine.decide(research, now=self.now)
        self.assertEqual(decision.action, "stop")
        for code in (STRATEGY_CODE, "SOURCE_CHANGED"):
            for later in (timedelta(0), timedelta(days=30)):
                with self.subTest(code=code, later=later):
                    run = self.make(request=FOUNDER_RESEARCH, result={"error_code": code, "re_pin_v1": {"count": 9}})
                    self.assertNotIn(engine.decide(run, now=self.now + later).action, engine.STRATEGY_ACTIONS)
        # The same code on an article run still advances the strategy rung.
        article = self.make(workflow="article_generation", request={"request_source": "roo_slackbot"},
                           result={"error_code": STRATEGY_CODE})
        self.assertEqual(engine.decide(article, now=self.now).action, "advance_rung")
        with patch("content_factory.channel_switch.switch_to_copy_ready") as switch, \
                patch("content_factory.vibe_marketing_views._call_content_factory_run_action") as action, \
                patch("content_factory.no_delivery_refunds.apply_refund_decision", return_value={}):
            self.make(request=SCHEDULER, result={"error_code": STRATEGY_CODE, "draft_ready_v1": {"x": 1}})
            for run in ContentFactoryRun.objects.filter(workflow="island_refresh"):
                engine.apply(run, engine.decide(run, now=self.now), now=self.now)
        switch.assert_not_called()
        action.assert_not_called()
        day = OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy.get("self_heal", {})
        self.assertEqual(day.get("strategy_attempts", 0), before.get("self_heal", {}).get("strategy_attempts", 0))

    def test_channel_switch_refuses_a_non_article_run(self):
        research = self.make(request=FOUNDER_RESEARCH, result={"draft_ready_v1": {"x": 1}})
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action") as action:
            answer = channel_switch.switch_to_copy_ready(run=research, reason="strategy", now=self.now)
        self.assertEqual(answer["status"], "refused")
        action.assert_not_called()
        research.refresh_from_db()
        self.assertNotIn("channel_v1", research.result)


class BlockingCodeTextTests(AudienceFixture):
    def la_vie_run(self):
        url = "https://preview.example.test/stories/held"
        return self.make(workflow="article_generation", status="approval_required",
            request={"request_source": "roo_slackbot", "roo_points_billing_status": "charged", "roo_points_cost": 12},
            age=timedelta(days=1), result={
                "generation": 0, "livePreview": {"previewUrl": url, "resumeGeneration": 0, "proof": {"commitSha": "a" * 40}},
                "failure": {"code": "website_connection_required", "requires_user_action": True,
                            "message": "Reconnect this website to continue."},
                "article_preview_quality": {"preview_url": url, "resume_generation": 0, "inputs_sha256": "b" * 64,
                                            "status": "blocking_findings", "self_heal_exhausted": True,
                                            "escalation": "quality_review"}})

    def test_pk_5396_alert_leads_with_the_connection_code_and_still_names_the_quality_review(self):
        run = self.la_vie_run()
        self.assertEqual(alerts.run_blocking_code(run), "website_connection_required")
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="article_quality_review_required", run=run)
        _, texts = self.founder_sweep()
        quality = [text for text in texts if "article_quality_review_required" in text]
        self.assertEqual(len(quality), 1)
        self.assertTrue(quality[0].startswith("website_connection_required"), quality[0])
        self.assertIn("quality repair needs staff review", quality[0])

    def test_hosted_quality_path_delivers_the_same_lead(self):
        from .hosted_quality_alerts import queue_hosted_quality_review_alert
        run = self.la_vie_run()
        with self.captureOnCommitCallbacks(execute=True):
            queue_hosted_quality_review_alert(run)
        _, texts = self.founder_sweep(now=timezone.now() + timedelta(seconds=1))
        quality = [text for text in texts if "article_quality_review_required" in text]
        self.assertEqual(len(quality), 1)
        self.assertTrue(quality[0].startswith("website_connection_required"), quality[0])

    def test_precedence_and_non_blocking_runs(self):
        def blocking(status, **result):
            return alerts.run_blocking_code(ContentFactoryRun(status=status, result=result))
        # A self-heal recode is informational; the connection code wins.
        self.assertEqual(blocking("failed", error_code="SELF_HEAL_DEADLINE_REACHED",
                                  failure={"code": "website_connection_required"}), "website_connection_required")
        self.assertEqual(blocking("blocked", error_code="ARTICLE_SYSTEM_SETUP_REQUIRED",
                                  authority_denied={"code": "consent_revoked"}), "consent_revoked")
        self.assertEqual(blocking("failed", error_code="PUBLISH_MERGE_FAILED",
                                  failure={"code": "ARTICLE_SYSTEM_SETUP_REQUIRED"}), "ARTICLE_SYSTEM_SETUP_REQUIRED")
        self.assertEqual(blocking("failed", error_code="REVIEW_PROVIDER_UNAVAILABLE"), "REVIEW_PROVIDER_UNAVAILABLE")
        # Waiting on the founder without an ask, or a finished run, leads with nothing.
        self.assertEqual(blocking("approval_required", failure={"code": "website_connection_required"}), "")
        self.assertEqual(blocking("completed", error_code="X"), "")

    def test_text_without_a_blocking_code_is_unchanged(self):
        context = {"org_id": self.org.pk, "domain": "", "company_id": "", "run_id": "r1", "workflow": "x",
                   "run_status": "completed", "stop_code": "", "step_attempts": None, "staff_link": ""}
        text = alerts.alert_text({"alert_reason": "run_terminal", "attempts": 1}, context)
        self.assertTrue(text.startswith("Founder journey alert:"))
