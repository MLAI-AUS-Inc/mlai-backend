"""Golden alert scenarios and alert invariants (plan F14).

Each scenario rebuilds, on fresh synthetic rows, the shape the 18 Nov 2026
founder catalogue recorded for one organisation, then drives the real alert
code: the queue functions, the self-heal policy sweep, the system-stopped run
sweep (B24), the hosted quality queue, the staff alert rules, the nightly
digest (B6), the founder stuck sweep, the connection invariant sweep (B22) and
the founder-journey sweep (detect and deliver). It asserts the exact alerts that come out: how many receipts, which
reason, and the title staff see in Slack. Nothing leaves the process: Slack is
a test double, Content Factory calls are recorded doubles, and any other HTTP
request fails the test.

The invariants below run as table tests over every producer:

- a scheduler-source run never produces a founder receipt;
- every receipt's title is registry copy (``REASON_LABELS``, or the failure
  registry's own founder copy for ``founder_stuck``), never raw error text;
- two sweep passes never give a second receipt for the same (org, run, reason);
- a marked test organisation produces nothing (waits for F1's marker).

No organisation, founder or run here is real; ids and domains are synthetic
and only the shapes follow the catalogue.
"""
from __future__ import annotations

from contextlib import ExitStack
import re
from datetime import timedelta
from unittest import skip
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

from . import background_job_digest as digest
from . import failure_policy_engine as engine
from . import founder_journey_alerts as alerts
from . import founder_stuck_alerts as stuck
from . import staff_alert_rules as rules
from . import system_stopped_runs as stopped
from . import website_connection_invariants as invariants
from .hosted_quality_alerts import queue_hosted_quality_review_alert
from .models import OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .website_models import WebsiteConnection, WebsiteConnectionOperation

CHANNEL = "C0000000000"
SLACK = "integrations.services.slack.SlackService.get_client"
CF_ACTION = "content_factory.vibe_marketing_views._call_content_factory_run_action"
CHANNEL_SWITCH = "content_factory.channel_switch.switch_to_copy_ready"
SCHEDULER = {"request_source": "mlai_backend_scheduler", "include_expansion": False}
FOUNDER = {"request_source": "roo_slackbot"}
# Raw worker text that must never reach a receipt or a Slack line.
RAW_ERROR = "Traceback (most recent call last): secret sk-live-RAWTEXT at founder-raw@example.test"
TITLE = re.compile(r"Founder journey alert: (.+?) \(([a-z0-9_]+)\)\. ")
RAW_MARKERS = ("Traceback", "sk-live-RAWTEXT", "founder-raw@example.test", "most recent call last")


def acknowledged(**kwargs):
    return {"ok": True, "ts": f"1700000000.{kwargs['client_msg_id'][:6]}"}


def founder_research(**extra):
    return {**FOUNDER, "client_request_id": "island-research:" + uuid4().hex * 2,
            "island_research_brief": {"topic": "synthetic"}, **extra}


class NetworkRefused(AssertionError):
    """A golden scenario tried to reach the network."""


class GoldenFixture(TestCase):
    """Fresh state per scenario, the alert switches on, and every outside seam doubled."""

    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        rollout = override_settings(**{
            **ALL_FLAGS_ON,
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED": True,
            "ARTICLES_STAFF_ALERT_RULES_ENABLED": True,
            "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED": True,
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE": (self.now - timedelta(days=60)).date().isoformat(),
            "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK": 200,
            "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID": CHANNEL,
            "DEFAULT_BACKEND_URL": "https://api.example.test",
            "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.example.test",
        })
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.texts = []
        self.cf_actions = []
        self.channel_switches = []
        self.network_calls = []

    def _refuse_network(self, *args, **kwargs):
        self.network_calls.append((args, kwargs))
        raise NetworkRefused("A golden alert scenario must not make an HTTP request.")

    # --- builders ---------------------------------------------------------

    def startup(self, name, domain):
        built = make_p2_org(domain)
        built.organization.name = name
        built.organization.save(update_fields=["name"])
        built.company.name = name
        built.company.save(update_fields=["name"])
        OrganizationContentConfig.objects.get_or_create(organization=built.organization)
        return built

    def connect(self, built, *, generation=1, verified_sha="", blockers=None):
        connection = WebsiteConnection.objects.create(
            organization=built.organization, authorized_by=built.owner, github_repo="synthetic/site",
            repository_id=4242, installation_id="77", branch="main", site_url=f"https://{built.organization.domain}",
            state=WebsiteConnection.State.CONNECTED, generation=generation, verified_sha=verified_sha,
            blockers=blockers or [])
        config = built.config
        config.refresh_from_db()
        config.website_connection = connection
        config.github_repo = connection.github_repo
        config.save(update_fields=["website_connection", "github_repo", "updated_at"])
        return connection

    def operation(self, connection, *, action="prepare", state="needs_attention", attempts=1, receipt=None, age):
        op = WebsiteConnectionOperation.objects.create(
            connection=connection, generation=connection.generation, idempotency_key=uuid4().hex, action=action,
            state=state, attempts=attempts, receipt=receipt or {})
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(created_at=self.now - age, updated_at=self.now - age)
        op.refresh_from_db()
        return op

    def make_run(self, built, *, workflow, status, age, request=None, result=None, error=RAW_ERROR, label=None):
        """One run, created ``age`` ago, carrying raw worker text in ``error`` and ``failure.message``."""
        result = dict(result or {})
        if error and isinstance(result.get("failure"), dict):
            result["failure"] = {"message": error, **result["failure"]}
        run = ContentFactoryRun.objects.create(
            run_id=label or f"run-{uuid4().hex[:12]}", organization=built.organization, domain=built.organization.domain,
            workflow=workflow, status=status, run_request=dict(request or {}), result=result,
            error=error if status not in {"completed", "approval_required", "awaiting_approval"} else "")
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run

    # --- the producers ----------------------------------------------------

    def tick(self, now):
        """One tick of every alert producer.

        ``run_scheduled_discovery`` runs the founder-journey, stuck, invariant
        and B24 sweeps before ``scheduled_self_heal_sweeps``. The fixtures are
        days old the moment they exist, so this tick runs the producers that
        act on a stop within minutes (self-heal, B24, the hosted quality hold)
        first: in production they have long run before the stuck sweep's six
        hours or the invariant sweep's one hour of grace is reached.
        """
        engine.run_policy_sweep(now=now)
        stopped.run_system_stopped_run_sweep(now=now)
        # The hosted quality alert is queued from the callback that holds the run.
        for run in ContentFactoryRun.objects.filter(workflow__in=alerts.ARTICLE_WORKFLOWS):
            with self.captureOnCommitCallbacks(execute=True):
                queue_hosted_quality_review_alert(run)
        rules.run_alert_rule_sweep(now=now)
        digest.run_background_job_digest_sweep(now=now)
        # The two throttled sweeps run on every golden tick, so a second pass
        # proves dedupe by receipt, never by throttle.
        cache.delete(stuck.THROTTLE_KEY)
        stuck.run_founder_stuck_alert_sweep(now=now)
        cache.delete(invariants.THROTTLE_KEY)
        invariants.run_connection_invariant_sweep(now=now)
        alerts.run_founder_journey_alert_sweep(now=now)

    def sweep_all(self, now=None):
        """Two ticks a minute apart (the second must add nothing new); returns the Slack texts posted."""
        now = now or self.now
        posted = []
        with ExitStack() as stack:
            client = stack.enter_context(patch(SLACK))
            client.return_value.chat_postMessage.side_effect = acknowledged
            stack.enter_context(patch(CF_ACTION, side_effect=lambda **kwargs: self.cf_actions.append(kwargs) or {}))
            stack.enter_context(patch(CHANNEL_SWITCH, side_effect=lambda **kwargs: self.channel_switches.append(kwargs)
                                      or {"status": "refused"}))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=self._refuse_network))
            self.tick(now)
            self.tick(now + timedelta(minutes=1))
            posted = [call.kwargs["text"] for call in client.return_value.chat_postMessage.call_args_list]
        # Recorded as well as raised: a producer that swallows the error must still fail the scenario.
        self.assertEqual(self.network_calls, [])
        self.texts.extend(posted)
        return posted

    # --- what came out ----------------------------------------------------

    def receipts(self, built=None):
        """Every founder-journey receipt, or one startup's, keyed by alert id."""
        rows = OrganizationContentConfig.objects.all()
        if built is not None:
            rows = rows.filter(organization=built.organization)
        found = {}
        for strategy in rows.values_list("pillar_strategy", flat=True):
            receipts = (strategy or {}).get(alerts.RECEIPTS_KEY) if isinstance(strategy, dict) else None
            found.update({key: value for key, value in (receipts or {}).items() if isinstance(value, dict)})
        return found

    def produced(self, built=None):
        """Sorted (reason, title) of every receipt, plus the global staff receipts."""
        rows = [(receipt["alert_reason"], alerts.REASON_LABELS[receipt["alert_reason"]])
                for receipt in self.receipts(built).values()]
        if built is None:
            rows += [(receipt["alert_reason"], alerts.REASON_LABELS[receipt["alert_reason"]])
                     for receipt in rules.global_alerts(now=self.now)]
        return sorted(rows)

    def assert_titles(self, texts):
        """Each Slack line carries its reason's registry title and no raw worker text."""
        for text in texts:
            for marker in RAW_MARKERS:
                self.assertNotIn(marker, text)
            if text.startswith("Founder stuck:"):
                # founder_stuck names its cause with the failure registry's own copy.
                reason = re.search(r"Reason: (?:([A-Za-z][A-Za-z0-9_.\-]*): )?(.+?)(?: Stuck for| Waiting on| Attempts| Run | Website operation| Staff console| Alert delivery)", text)
                self.assertIsNotNone(reason, text)
                code, copy = reason.groups()
                entry = stuck._registry_entry(code) if code else None
                expected = {(entry or {}).get("user_message"), *stuck.KIND_DESCRIPTIONS.values(),
                            "No failure code was reported."}
                self.assertIn(copy, expected, text)
                continue
            match = TITLE.search(text)
            self.assertIsNotNone(match, text)
            label, reason = match.groups()
            self.assertEqual(label, alerts.REASON_LABELS[reason], text)

    def assert_second_pass_adds_nothing(self):
        """A second tick (11 minutes later) keeps every receipt and posts nothing new."""
        before = self.receipts()
        globals_before = rules.global_alerts(now=self.now)
        self.assertEqual(self.sweep_all(now=self.now + timedelta(minutes=11)), [])
        after = self.receipts()
        self.assertEqual(set(after), set(before))
        self.assertEqual([receipt["state"] for receipt in after.values()],
                         [before[key]["state"] for key in after])
        self.assertEqual(len(rules.global_alerts(now=self.now)), len(globals_before))

    # --- the catalogue shapes (founder-issues-2026-11-18) --------------------

    def build_175_history(self):
        """Org 175 on 18 Nov: nine failed nightly refreshes (NO_KEYWORDS) 3 to 4 weeks old, plus one unstamped."""
        built = self.startup("Collab synthetic", "collab.example.test")
        for day in range(9):
            self.make_run(built, workflow="island_refresh", status="failed", age=timedelta(days=21 + day, hours=3),
                          request=SCHEDULER, result={"error_code": "NO_KEYWORDS"}, label=f"collab-night-{day}")
        self.make_run(built, workflow="island_refresh", status="failed", age=timedelta(days=31), request={},
                      result={"error_code": "NO_KEYWORDS"}, label="collab-unstamped")
        return built

    def build_175_streak(self):
        """The same nine nights while they still happen: the last nine Melbourne nights in a row."""
        built = self.startup("Collab streak synthetic", "collab-streak.example.test")
        for day in range(9):
            self.make_run(built, workflow="island_refresh", status="failed", age=timedelta(days=day, hours=1),
                          request=SCHEDULER, result={"error_code": "NO_KEYWORDS"}, label=f"collab-streak-{day}")
        return built

    def build_200(self):
        """Org 200 Maxbiz: one nightly SYNC_FAILED with no reason; every other night completed."""
        built = self.startup("Maxbiz synthetic", "maxbiz.example.test")
        for day in range(1, 9):
            failed = day == 5
            self.make_run(built, workflow="island_refresh", status="failed" if failed else "completed",
                          age=timedelta(days=day, hours=1), request=SCHEDULER, label=f"maxbiz-night-{day}",
                          result={"error_code": "SYNC_FAILED"} if failed else {}, error=RAW_ERROR if failed else "")
        return built

    def build_173(self):
        """Org 173 La Vie: pk 5388 and 5391 failed, pk 5396 held for quality review with a connection code."""
        built = self.startup("La Vie synthetic", "lavie.example.test")
        self.connect(built, generation=11, verified_sha="f" * 40)
        self.make_run(built, workflow="confirmed_topic", status="failed", age=timedelta(days=1, hours=6), request=FOUNDER,
                      label="lavie-5388", result={"error_code": "verified_target_required",
                                                  "failure": {"code": "verified_target_required", "stage": "load_context"}})
        self.make_run(built, workflow="confirmed_topic", status="failed", age=timedelta(days=1, hours=3), request=FOUNDER,
                      label="lavie-5391", result={"error_code": "CORPUS_INCOMPLETE",
                                                  "failure": {"code": "CORPUS_INCOMPLETE", "stage": "plan_article"}})
        url = "https://preview.example.test/stories/held"
        self.make_run(built, workflow="confirmed_topic", status="approval_required", age=timedelta(days=1),
                      request={**FOUNDER, "roo_points_billing_status": "charged", "roo_points_cost": 12}, label="lavie-5396",
                      result={"generation": 0,
                              "livePreview": {"previewUrl": url, "resumeGeneration": 0, "proof": {"commitSha": "a" * 40}},
                              "failure": {"code": "website_connection_required", "requires_user_action": True,
                                          "stage": "await_review", "generation": 0},
                              "article_preview_quality": {"preview_url": url, "resume_generation": 0,
                                                          "inputs_sha256": "b" * 64, "status": "blocking_findings",
                                                          "self_heal_exhausted": True, "escalation": "quality_review"}})
        return built

    def build_124(self):
        """Org 124 Pawshare: four failed or blocked setup runs and a prepare op stopped after 230 claims."""
        built = self.startup("Pawshare synthetic", "pawshare.example.test")
        connection = self.connect(built, blockers=[{"code": "repository_source_changed"}])
        for label, days, status, code in (("5131", 11, "failed", "ARTICLE_DIRECTORY_ROUTE_AUDIT_BLOCKED"),
                                          ("5133", 11, "failed", "ARTICLE_DIRECTORY_ROUTE_AUDIT_BLOCKED"),
                                          ("5159", 10, "blocked", "DIRECTORY_DEPENDENCY_VALIDATION_FAILED"),
                                          ("5241", 7, "blocked", "DIRECTORY_DEPENDENCY_VALIDATION_FAILED")):
            self.make_run(built, workflow="article_system_setup", status=status, age=timedelta(days=days), request=FOUNDER,
                          result={"error_code": code, "failure": {"code": code}}, label=f"pawshare-{label}")
        self.operation(connection, state="needs_attention", attempts=230, age=timedelta(days=5),
                       receipt={"reason_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED", "retryable": False,
                                "status": "needs_attention"})
        return built

    def build_209(self):
        """Org 209 Lilia: a prepare op parked on the retired page choice, three denied research runs, no seeds.

        The eight points-blocked research runs are absent: since A18 the backend
        refuses research below the floor before any run row exists.
        """
        built = self.startup("Lilia synthetic", "lilia.example.test")
        connection = self.connect(built)
        self.operation(connection, state="needs_user", attempts=3, age=timedelta(days=9),
                       receipt={"reason_code": "articles_page_choice_required", "status": "needs_user",
                                "user_action": {"id": "choose_articles_page"}})
        for index in range(3):
            self.make_run(built, workflow="island_refresh", status="denied", age=timedelta(days=6 - index),
                          request=founder_research(), label=f"lilia-research-{index}",
                          result={"error_code": "website_connection_required",
                                  "authority_denied": {"code": "website_connection_required"}})
        self.make_run(built, workflow="auto_discovery", status="failed", age=timedelta(days=8), request=FOUNDER,
                      label="lilia-discovery", result={"error_code": "VALIDATION_ERROR", "failure": {"code": "VALIDATION_ERROR"}})
        return built

    def build_30(self):
        """Org 30 Tala: six cancelled confirmed_topic runs on a connected, verified site.

        Four are the founder's own cancels, pk 4978 carries an unstamped
        ``website_operation_terminal`` (left alone until the A1 trace, B24) and
        one was stopped by a reset transition two days ago.
        """
        built = self.startup("Tala synthetic", "tala.example.test")
        connection = self.connect(built, generation=3, verified_sha="e" * 40)
        connection.capabilities = {"publishingReady": True}
        connection.save(update_fields=["capabilities"])
        contract = {**FOUNDER, "website_connection_id": str(connection.pk), "connection_generation": 3,
                    "repository_id": connection.repository_id}
        for index, days in enumerate((15, 12, 9, 6)):
            self.make_run(built, workflow="confirmed_topic", status="cancelled", age=timedelta(days=days), request=contract,
                          error="", label=f"tala-owner-{index}",
                          result={"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"})
        self.make_run(built, workflow="confirmed_topic", status="cancelled", age=timedelta(days=13), request=contract,
                      error="", label="tala-4978", result={"error_code": "website_operation_terminal",
                      "failure": {"code": "website_operation_terminal", "stage": "package_content_delivery"}})
        self.make_run(built, workflow="confirmed_topic", status="cancelled", age=timedelta(days=2), request=contract,
                      error=stopped.SYSTEM_TRANSITION_ERROR, label="tala-system",
                      result={"cancellation_reason": "system_stop", "cancelled_by": "system",
                              "cancellation_transition": "reset", "cancellation_generation": 3})
        return built

    def build_87(self):
        """Org 87 Vyavos: three empty research nights (completed empty since B23), then out of points."""
        built = self.startup("Vyavos synthetic", "vyavos.example.test")
        self.connect(built, verified_sha="d" * 40)
        for day in (11, 8, 7):
            self.make_run(built, workflow="auto_discovery", status="completed", age=timedelta(days=day), error="",
                          request={**FOUNDER, "client_request_id": alerts.AUTOMATION_RESEARCH_REQUEST_PREFIX + uuid4().hex},
                          result={"outcome": "empty", "empty_reason": "no_opportunities"}, label=f"vyavos-night-{day}")
        return built

    def build_all(self):
        return [builder() for builder in (self.build_175_history, self.build_175_streak, self.build_200, self.build_173,
                                          self.build_124, self.build_209, self.build_30, self.build_87)]


STREAK = ("background_job_failing", alerts.REASON_LABELS["background_job_failing"])
STOPPED = ("run_terminal", alerts.REASON_LABELS["run_terminal"])
BLOCKED = ("run_blocked", alerts.REASON_LABELS["run_blocked"])
CAPPED = ("self_heal_capped", alerts.REASON_LABELS["self_heal_capped"])
STUCK = ("founder_stuck", alerts.REASON_LABELS["founder_stuck"])
QUALITY = ("article_quality_review_required", alerts.REASON_LABELS["article_quality_review_required"])
TEAM_ITEM = ("our_team_item", alerts.REASON_LABELS["our_team_item"])
YOUR_TURN = ("your_turn_72h", alerts.REASON_LABELS["your_turn_72h"])
CODELESS = ("codeless_terminal", alerts.REASON_LABELS["codeless_terminal"])
INVARIANT = ("connection_invariant", alerts.REASON_LABELS["connection_invariant"])


class GoldenScenarioTests(GoldenFixture):
    """One catalogue organisation per test, each on fresh state; exact receipts and titles."""

    def by_reason(self, reason):
        return [receipt for receipt in self.receipts().values() if receipt["alert_reason"] == reason]

    def text_for(self, reason, run_id=None):
        found = [text for text in self.texts if f"({reason})." in text and (run_id is None or f"Run {run_id}." in text)]
        self.assertEqual(len(found), 1, (reason, run_id, self.texts))
        return found[0]

    def test_org_175_nine_old_failed_nights_give_zero_receipts_and_no_self_heal(self):
        built = self.build_175_history()
        before = {run.pk: dict(run.result) for run in ContentFactoryRun.objects.all()}
        strategy = dict(built.config.pillar_strategy or {})
        self.assertEqual(self.sweep_all(), [])
        self.assertEqual(self.produced(), [])
        self.assertEqual((self.cf_actions, self.channel_switches), ([], []))
        # Self-heal never touched them: no recode, no rung, no strategy budget spent (A2).
        for run in ContentFactoryRun.objects.all():
            self.assertEqual(run.result, before[run.pk])
        built.config.refresh_from_db()
        self.assertEqual((built.config.pillar_strategy or {}).get("self_heal"), strategy.get("self_heal"))
        self.assert_second_pass_adds_nothing()

    def test_org_175_a_live_nine_night_streak_gives_one_digest_alert(self):
        self.build_175_streak()
        texts = self.sweep_all()
        self.assertEqual(self.produced(), [STREAK])
        (receipt,) = self.receipts().values()
        self.assertIsNone(receipt["run_id"])
        self.assertEqual((receipt["details"]["nights"], receipt["details"]["last_code"]), (9, "NO_KEYWORDS"))
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("Founder journey alert: A startup's nightly job failed three nights running"
                                            " (background_job_failing)."), texts[0])
        self.assertEqual((self.cf_actions, self.channel_switches), ([], []))
        self.assert_titles(self.texts)
        self.assert_second_pass_adds_nothing()

    def test_org_200_one_failed_night_gives_nothing(self):
        self.build_200()
        self.assertEqual(self.sweep_all(), [])
        self.assertEqual(self.produced(), [])
        self.assert_second_pass_adds_nothing()

    def test_org_173_la_vie_leads_with_the_connection_code(self):
        self.build_173()
        texts = self.sweep_all()
        self.assertEqual(self.produced(), sorted([QUALITY, STOPPED, STOPPED, TEAM_ITEM]))
        self.assertEqual(len(texts), 4)
        quality = self.text_for("article_quality_review_required", "lavie-5396")
        self.assertTrue(quality.startswith("website_connection_required blocks this run. Founder journey alert: "
                                           "An article's automatic quality repair needs staff review"), quality)
        self.assertTrue(self.text_for("run_terminal", "lavie-5388").startswith("verified_target_required blocks this run."))
        self.assertTrue(self.text_for("run_terminal", "lavie-5391").startswith("CORPUS_INCOMPLETE blocks this run."))
        # Self-heal stopped the unsupported CORPUS_INCOMPLETE run without a cap: one staff item.
        self.assertTrue(self.text_for("our_team_item", "lavie-5391").startswith("CORPUS_INCOMPLETE blocks this run."))
        self.assertEqual(self.channel_switches, [])
        self.assert_titles(self.texts)
        self.assert_second_pass_adds_nothing()

    def test_org_124_pawshare_one_alert_per_cause_and_one_for_the_stopped_setup(self):
        self.build_124()
        texts = self.sweep_all()
        # self_heal_capped and run_terminal share one receipt per (workflow, code) (B5); run_blocked is per run.
        self.assertEqual(self.produced(), sorted([CAPPED, CAPPED, STOPPED, BLOCKED, BLOCKED, STUCK, INVARIANT]))
        self.assertEqual(len(texts), 7)
        # B22: a source blocker on a site that was never verified (the catalogue's Pawshare fault).
        (invariant,) = self.by_reason("connection_invariant")
        self.assertEqual(invariant["details"]["invariant"], "source_blocker_never_verified")
        self.assertEqual(sorted(receipt["details"]["original_code"] for receipt in self.by_reason("self_heal_capped")),
                         ["ARTICLE_DIRECTORY_ROUTE_AUDIT_BLOCKED", "DIRECTORY_DEPENDENCY_VALIDATION_FAILED"])
        self.assertEqual(sorted(receipt["run_id"] for receipt in self.by_reason("run_blocked")),
                         ["pawshare-5159", "pawshare-5241"])
        (setup,) = [text for text in texts if text.startswith("Founder stuck:")]
        self.assertIn("Step: Website setup.", setup)
        self.assertIn("Reason: DIRECTORY_DEPENDENCY_VALIDATION_FAILED: ", setup)
        self.assertIn("Attempts 230.", setup)
        # The capped stop is recorded beside the original code, never over it (B2).
        for run in ContentFactoryRun.objects.filter(workflow="article_system_setup"):
            self.assertNotIn(run.result["error_code"], alerts.SELF_HEAL_STOP_CODES)
        self.assertEqual(self.channel_switches, [])
        self.assert_titles(self.texts)
        self.assert_second_pass_adds_nothing()

    def test_org_209_lilia_the_hidden_page_choice_and_founder_research_alert(self):
        self.build_209()
        texts = self.sweep_all()
        self.assertEqual(self.produced(), sorted([STUCK, STOPPED, STOPPED, YOUR_TURN]))
        self.assertEqual(len(texts), 4)
        (hidden,) = [text for text in texts if text.startswith("Founder stuck:")]
        self.assertIn("Waiting on the retired ask choose_articles_page.", hidden)
        # Three denied founder research runs with one cause are one alert (A2 audience, B5 slot).
        research = [text for text in texts if "Workflow island_refresh." in text]
        self.assertEqual(len(research), 1)
        self.assertTrue(research[0].startswith("website_connection_required blocks this run."), research[0])
        self.assertTrue(self.text_for("run_terminal", "lilia-discovery").startswith("VALIDATION_ERROR blocks this run."))
        self.assertEqual(self.by_reason("your_turn_72h")[0]["details"]["website_operation"], "prepare")
        self.assertEqual(self.channel_switches, [])
        self.assert_titles(self.texts)
        self.assert_second_pass_adds_nothing()

    def test_org_30_tala_one_notice_for_the_system_stop_and_nothing_for_founder_cancels(self):
        self.build_30()
        texts = self.sweep_all()
        # B24 gave the system stop its notice, so the B22 "stopped without a notice" invariant stays quiet.
        self.assertEqual(self.produced(), sorted([STOPPED, STOPPED, CODELESS]))
        self.assertEqual(len(texts), 3)
        self.assertEqual(sorted(receipt["run_id"] for receipt in self.receipts().values()),
                         ["tala-4978", "tala-system", "tala-system"])
        notices = {run.run_id: stopped.notice_for(run) for run in ContentFactoryRun.objects.all()}
        self.assertEqual({run_id for run_id, notice in notices.items() if notice}, {"tala-system"})
        self.assertEqual(notices["tala-system"]["message"], stopped.NOTICE_MESSAGE)
        self.assertEqual(notices["tala-system"]["action"], {"id": "restart", "label": "Restart"})
        for run in ContentFactoryRun.objects.exclude(run_id="tala-system"):
            self.assertNotIn(stopped.FOLLOWUP_KEY, run.result)
        self.assertEqual(self.cf_actions, [])
        self.assert_titles(self.texts)
        self.assert_second_pass_adds_nothing()
        # The second pass gave no second notice either.
        run = ContentFactoryRun.objects.get(run_id="tala-system")
        self.assertEqual(run.result[stopped.FOLLOWUP_KEY]["decision"], "notice")


@override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="cf-key",
                   ROO_API_KEY="golden-roo-key", INTERNAL_API_KEY="golden-roo-key")
class VyavosScenarioTests(GoldenFixture):
    """Org 87 Vyavos: three empty research nights, then out of points (B23, B18)."""

    def setUp(self):
        super().setUp()
        from core.models import User
        from integrations.services.research_automations import create_or_update_research_automation
        from .models import NotificationChannelType, NotificationConsentState
        self.built = self.build_87()
        org = self.built.organization
        org.competitors, org.seed_keywords = ["competitor.example.test"], ["clinic ops"]
        org.save(update_fields=["competitors", "seed_keywords"])
        self.user = User.objects.create_user(email=f"vyavos-{uuid4().hex[:6]}@synthetic.example.test")
        self.user.slack_id = "U_VYAVOS"
        self.user.save(update_fields=["slack_id"])
        self.automation = create_or_update_research_automation(
            domain=org.domain, channel_type=NotificationChannelType.EMAIL, route_id=self.user.email, user=self.user,
            timezone_name="Australia/Melbourne", frequency_per_day=1, local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE)
        OrganizationContentConfig.objects.filter(organization=org).update(daily_discovery_enabled=True)

    def night(self, day):
        from datetime import datetime, timezone as dt_timezone
        from .models import AutomationRun, AutomationRunStatus
        scheduled = datetime(2026, 3, 23, 21, 0, tzinfo=dt_timezone.utc) + timedelta(days=day)
        return AutomationRun.objects.create(
            automation=self.automation, scheduled_for_at=scheduled, local_date=(scheduled + timedelta(hours=11)).date(),
            slot_index=0, status=AutomationRunStatus.QUEUED, content_factory_run_id=f"vyavos-cf-{day}",
            idempotency_key=f"research-automation:vyavos-golden:{day}:0")

    def empty_callback(self, run):
        from django.urls import reverse
        from integrations.services.notification_adapters import notification_context_for_run
        from tests.website_fixtures import CallbackFixtureClient
        client = CallbackFixtureClient()
        client.credentials(HTTP_X_API_KEY="golden-roo-key")
        response = client.post(reverse("content_factory_callback"), {
            "event_type": "research_empty", "job_id": run.content_factory_run_id, "run_id": run.content_factory_run_id,
            "domain": self.built.organization.domain, "workflow": "auto_discovery", "outcome": "empty",
            "empty_reason": "no_opportunities", "empty_detail_code": "NO_OPPORTUNITIES",
            "notification_context": notification_context_for_run(run)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def test_org_87_empty_nights_and_a_points_pause_give_no_alert_and_one_notice_each(self):
        from datetime import datetime, timezone as dt_timezone
        from integrations.services.article_generation import InsufficientRooPointsError
        from integrations.services.research_automations import run_research_automation_scheduler
        from .models import NotificationDelivery, ResearchAutomationStatus
        with patch("integrations.services.notification_adapters._send_email", return_value=(True, "email-1", {})) as email:
            for day in range(3):
                run = self.night(day)
                self.empty_callback(run)
                self.empty_callback(run)  # A replayed callback counts once.
            refusal = InsufficientRooPointsError({"message": "Finding topics uses 6 points. You have 2.",
                                                  "error_code": "INSUFFICIENT_ROO_POINTS", "required_points": 6,
                                                  "current_balance": 2, "cost_points": 6})
            with patch("integrations.services.research_automations._content_factory_balance_for_user", return_value=2), \
                    patch("integrations.services.research_automations.charge_content_factory_topic_generation_for_user",
                          side_effect=refusal), \
                    patch("content_factory.vibe_marketing_views._queue_content_factory_run") as dispatch:
                first = datetime(2026, 3, 27, 21, 5, tzinfo=dt_timezone.utc)
                for day in range(3):  # 14 Nov onwards: blocked every night for points.
                    run_research_automation_scheduler(now=first + timedelta(days=day))
            dispatch.assert_not_called()
            texts = self.sweep_all()
        self.assertEqual(texts, [])
        self.assertEqual(self.produced(), [])
        self.assertEqual(NotificationDelivery.objects.filter(event_type="research_empty").count(), 1)
        self.assertEqual(NotificationDelivery.objects.filter(event_type="automation_paused_points").count(), 1)
        self.assertEqual(email.call_count, 2)
        self.automation.refresh_from_db()
        self.assertEqual(self.automation.status, ResearchAutomationStatus.PAUSED)
        self.assert_second_pass_adds_nothing()


SCHEDULER_PRINCIPAL = {"run_owner_v1": {"identity": {"principal": {"kind": alerts.SCHEDULER_PRINCIPAL_KIND}}}}
QUALITY_HOLD = {"generation": 0, "livePreview": {"previewUrl": "https://preview.example.test/s", "resumeGeneration": 0,
                                                  "proof": {"commitSha": "c" * 40}},
                "article_preview_quality": {"preview_url": "https://preview.example.test/s", "resume_generation": 0,
                                            "status": "blocking_findings", "self_heal_exhausted": True,
                                            "escalation": "quality_review"}}
SYSTEM_STOP = {"cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "reset",
               "cancellation_generation": 1}


class AlertInvariantTests(GoldenFixture):
    """Table tests over every producer."""

    def scheduler_runs(self, built, connection):
        """Every scheduler shape: stamped source, the unstamped nightly refresh, and the scheduler principal."""
        contract = {"website_connection_id": str(connection.pk), "connection_generation": 1,
                    "repository_id": connection.repository_id}
        rows = (
            ("island_refresh", "failed", SCHEDULER, {"error_code": "NO_KEYWORDS"}, timedelta(hours=8)),
            ("island_refresh", "blocked", SCHEDULER, {"error_code": "SYNC_FAILED", "failure": {"action": {"id": "x"}}},
             timedelta(days=4)),
            ("island_refresh", "denied", SCHEDULER, {"authority_denied": {"code": "website_connection_required"}},
             timedelta(hours=8)),
            ("island_refresh", "cancelled", SCHEDULER, {}, timedelta(hours=8)),
            ("island_refresh", "failed", {}, {"error_code": "PUBLISHING_ADAPTER_REQUIRED"}, timedelta(hours=8)),
            ("topic_discovery", "failed", SCHEDULER, {"error_code": "TERMINAL_CODE_MISSING"}, timedelta(hours=8)),
            ("article_generation", "failed", SCHEDULER, {"error_code": "PUBLISHING_ADAPTER_REQUIRED",
                                                         "draft_ready_v1": {"x": 1}}, timedelta(hours=8)),
            ("article_generation", "failed", SCHEDULER, {"error_code": "RUN_BUDGET_EXHAUSTED",
                                                         "preview_repair_v1": {"outcome": "exhausted"}}, timedelta(hours=8)),
            ("article_generation", "approval_required", SCHEDULER, QUALITY_HOLD, timedelta(days=4)),
            ("article_generation", "queued", SCHEDULER, {}, timedelta(minutes=30)),
            ("confirmed_topic", "cancelled", {**SCHEDULER, **contract}, SYSTEM_STOP, timedelta(hours=8)),
            ("confirmed_topic", "cancelled", {**FOUNDER, **contract}, {**SYSTEM_STOP, **SCHEDULER_PRINCIPAL},
             timedelta(hours=8)),
            ("confirmed_topic", "failed", FOUNDER, {"error_code": "website_operation_terminal",
                                                    "callback_rejection": {"code": "website_operation_terminal",
                                                    "raise_site": "website_operations.validate_operation"},
                                                    **SCHEDULER_PRINCIPAL}, timedelta(hours=8)),
            ("article_system_setup", "failed", FOUNDER, {"error_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED",
                                                         **SCHEDULER_PRINCIPAL}, timedelta(hours=8)),
        )
        return [self.make_run(built, workflow=workflow, status=status, request=request, result=result, age=age,
                              label=f"scheduler-{index}-{workflow}-{status}")
                for index, (workflow, status, request, result, age) in enumerate(rows)]

    def test_a_scheduler_run_never_produces_a_founder_receipt(self):
        built = self.startup("Scheduler synthetic", "scheduler.example.test")
        connection = self.connect(built, verified_sha="b" * 40)
        runs = self.scheduler_runs(built, connection)
        for run in runs:
            with self.subTest(run=run.run_id):
                self.assertTrue(alerts.is_scheduler_run(run))
                self.assertEqual(alerts.run_alert_reasons(run, now=self.now), [])
                self.assertIsNone(stopped.stop_cause(run))
        # Every producer that is handed a run: the queue function, its batch twin and the engine's alert.
        for run in runs:
            for reason in sorted(alerts.ALERT_REASONS - {"founder_stuck"}):
                with self.subTest(run=run.run_id, reason=reason):
                    alerts.queue_founder_journey_alert(organization=built.organization, alert_reason=reason, run=run)
                    engine._alert(built.organization, reason, run=run)
        batch = alerts.queue_founder_journey_alerts_batch(
            [{"organization": built.organization, "alert_reason": reason, "run": run}
             for run in runs for reason in sorted(alerts.ALERT_REASONS - {"founder_stuck"})])
        self.assertEqual(batch, {"ids": [], "created": 0})
        # And every sweep, twice.
        self.assertEqual(self.sweep_all(), [])
        self.assertEqual(self.sweep_all(now=self.now + timedelta(minutes=11)), [])
        self.assertEqual(self.produced(), [])
        for run in ContentFactoryRun.objects.all():
            with self.subTest(run=run.run_id):
                self.assertEqual(alerts._receipts_in(run.result), {})
                self.assertNotIn(stopped.FOLLOWUP_KEY, run.result)
                self.assertIsNone(stopped.notice_for(run))
                self.assertNotIn("stop", (run.result.get("self_heal_v1") or {}))
        self.assertEqual((self.cf_actions, self.channel_switches), ([], []))

    def test_every_receipt_title_is_registry_copy_never_raw_error_text(self):
        self.assertEqual(alerts.ALERT_REASONS - set(alerts.REASON_LABELS), set())
        self.build_all()
        built = self.startup("Every reason synthetic", "every-reason.example.test")
        run = self.make_run(built, workflow="article_generation", status="failed", age=timedelta(hours=8),
                            request=FOUNDER, label="every-reason",
                            result={"error_code": "RAW_CODE_X", "failure": {"code": "RAW_CODE_X"},
                                    "error_message": RAW_ERROR})
        raw_details = {"message": RAW_ERROR, "error": RAW_ERROR, "reason": RAW_ERROR, "detail_code": "SAFE_CODE"}
        for reason in sorted(alerts.ALERT_REASONS - {"founder_stuck"}):
            alerts.queue_founder_journey_alert(organization=built.organization, alert_reason=reason, run=run,
                                               details=raw_details)
        stuck._queue([stuck.StuckCondition(organization_id=built.organization.pk, slot="golden-raw", kind="run_stopped",
                                           step="article_generation", code="RAW_CODE_X", stuck_since=self.now,
                                           details={"run_id": run.run_id, "error": RAW_ERROR})],
                     organization_ids={built.organization.pk})
        texts = self.sweep_all()
        receipts = self.receipts()
        self.assertGreaterEqual(len(texts), len(alerts.ALERT_REASONS))
        self.assertEqual(len(texts), len(receipts))
        self.assert_titles(texts)
        stored = repr(receipts) + repr(rules.global_alerts(now=self.now))
        for marker in RAW_MARKERS:
            self.assertNotIn(marker, stored)
        for receipt in receipts.values():
            self.assertIn(receipt["alert_reason"], alerts.REASON_LABELS)
        # The founder-facing B24 notice is fixed copy too.
        for row in ContentFactoryRun.objects.all():
            notice = stopped.notice_for(row)
            if notice:
                self.assertIn(notice["message"], {stopped.NOTICE_MESSAGE, stopped.REFUNDED_NOTICE_MESSAGE})

    def test_two_sweep_passes_never_give_a_second_receipt_for_one_org_run_and_reason(self):
        self.build_all()
        first = self.sweep_all()
        self.assertTrue(first)
        # Alert ids are per organisation: no (org, run, reason) holds two receipts.
        per_org = [(config.organization_id, receipt["run_id"], receipt["alert_reason"])
                   for config in OrganizationContentConfig.objects.all()
                   for receipt in alerts._receipts_in(config.pillar_strategy).values() if receipt.get("run_id")]
        self.assertTrue(per_org)
        self.assertEqual(len(per_org), len(set(per_org)))
        self.assert_second_pass_adds_nothing()
        # A run whose result was replaced wholesale (a status poll) adopts its receipt, never a second one.
        for row in ContentFactoryRun.objects.all():
            ContentFactoryRun.objects.filter(pk=row.pk).update(
                result={key: value for key, value in row.result.items() if key != alerts.RECEIPTS_KEY})
        before = set(self.receipts())
        self.assertEqual(self.sweep_all(now=self.now + timedelta(minutes=22)), [])
        self.assertEqual(set(self.receipts()) - before, set())

    @skip("TODO(F1): the test-org marker has not landed on this base branch; wire it here when it does.")
    def test_a_marked_test_organization_produces_nothing(self):
        """F1: build_all() on organisations carrying the test-org marker gives no receipt, notice or Slack line."""
