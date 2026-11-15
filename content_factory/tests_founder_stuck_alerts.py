"""The always-on founder stuck alert, on synthetic rows."""
from datetime import timedelta
import re
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import founder_stuck_alerts as stuck
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation

CHANNEL = "C0000000000"
SLACK = "integrations.services.slack.SlackService.get_client"
# A registered stop with no founder action, and one the founder fixes herself.
STRATEGY_CODE = "ADOPTED_DIRECTORY_APP_SHELL_MISSING"
FOUNDER_CODE = "GITHUB_ACCESS_REQUIRED"


def acknowledged(**kwargs):
    return {"ok": True, "ts": f"1700000000.{kwargs['client_msg_id'][:6]}"}


@override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True, ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False,
                   ARTICLES_STAFF_ALERT_RULES_ENABLED=False, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=CHANNEL,
                   COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test")
class FounderStuckFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.since = override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE=(self.now - timedelta(days=60)).date().isoformat())
        self.since.enable()
        self.addCleanup(self.since.disable)
        self.org, self.website = self.startup("Lavie Studio", "lavie.example.test", "Clara", "Founder")

    def startup(self, name, domain, first, last):
        user = User.objects.create_user(email=f"{uuid4().hex[:8]}@example.test", first_name=first, last_name=last)
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        org = Organization.objects.create(domain=domain, name=name)
        VibeRaisingCompany.objects.create(profile=profile, organization=org, domain=domain, name=name)
        website = WebsiteConnection.objects.create(organization=org, github_repo="example/site", installation_id="1",
                                                   branch="main", site_url=f"https://{domain}")
        OrganizationContentConfig.objects.get_or_create(organization=org)
        return org, website

    def operation(self, *, action="prepare", state="needs_attention", attempts=1, receipt=None, age=timedelta(hours=2),
                  website=None, generation=None):
        website = website or self.website
        op = WebsiteConnectionOperation.objects.create(
            connection=website, generation=website.generation if generation is None else generation,
            idempotency_key=uuid4().hex, action=action, state=state, attempts=attempts, receipt=receipt or {})
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(created_at=self.now - age, updated_at=self.now - age)
        return op

    def make_run(self, *, status="failed", workflow="article_generation", result=None, age=timedelta(hours=8), org=None):
        org = org or self.org
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, domain=org.domain,
                                               organization=org, status=status, result=result or {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        return run

    def sweep(self, now=None):
        cache.delete(stuck.THROTTLE_KEY)
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            outcome = stuck.run_founder_stuck_alert_sweep(now=now or self.now)
        return outcome, [call.kwargs["text"] for call in client.return_value.chat_postMessage.call_args_list]

    def receipts(self, org=None):
        strategy = OrganizationContentConfig.objects.get(organization=org or self.org).pillar_strategy
        return [r for r in strategy.get("ops_alerts", {}).values() if r.get("alert_reason") == stuck.STUCK_REASON]


class FounderStuckAlertTests(FounderStuckFixture):
    def test_stopped_setup_alerts_once_with_names_step_reason_duration_and_console_link(self):
        op = self.operation(receipt={"code": STRATEGY_CODE, "retryable": False}, age=timedelta(hours=27))
        outcome, texts = self.sweep()
        self.assertEqual((outcome["queued"], outcome["sent"]), (1, 1))
        text = texts[0]
        for expected in ("Founder stuck: Clara Founder at Lavie Studio (lavie.example.test).", "Step: Website setup.",
                         f"Reason: {STRATEGY_CODE}: The installed article page failed its own preview proof.",
                         "Stuck for 1 day 3 hours.", f"Website operation {op.pk}.",
                         f"Staff console: https://chat.example.test/admin/startups/{self.org.pk}"):
            self.assertIn(expected, text)
        self.assertEqual(self.receipts()[0]["state"], "sent")
        # Names are read at send time and never stored.
        self.assertNotIn("Clara", str(self.receipts()))
        # One alert per condition until it changes.
        self.assertEqual(self.sweep()[0]["queued"], 0)

    def test_condition_change_alerts_again(self):
        op = self.operation(action="reconnect", state="pending", attempts=21, age=timedelta(days=3))
        outcome, texts = self.sweep()
        self.assertEqual(outcome["queued"], 1)
        self.assertIn("Step: Reconnecting GitHub.", texts[0])
        self.assertIn("keeps retrying without finishing", texts[0])
        self.assertIn("Attempts 21.", texts[0])
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(state="needs_attention")
        self.assertEqual(self.sweep()[0]["queued"], 1)
        self.assertEqual(len(self.receipts()), 2)

    def test_young_or_settled_operations_do_not_alert(self):
        self.operation(state="pending", attempts=3)                     # retrying, but not long
        self.operation(state="failed", receipt={"retryable": True})     # will retry by itself
        self.operation(state="completed")
        self.operation(action="goal-usage", state="needs_attention")    # bookkeeping, never blocks a founder
        self.operation(generation=self.website.generation + 5)          # not the current generation
        self.assertEqual(self.sweep()[0]["candidates"], 0)

    def test_a_newer_operation_or_a_disconnected_site_hides_the_old_stop(self):
        self.operation(age=timedelta(hours=5))
        self.operation(state="running", age=timedelta(hours=1))
        self.assertEqual(self.sweep()[0]["candidates"], 0)
        other, site = self.startup("Gone", "gone.example.test", "Gone", "Founder")
        WebsiteConnection.objects.filter(pk=site.pk).update(state="disconnected")
        self.operation(website=site)
        self.assertEqual(self.sweep()[0]["candidates"], 0)

    def test_latest_run_stopped_with_no_next_action_alerts_for_a_legacy_startup(self):
        # No founder-journey marker: the legacy cohort is covered too.
        run = self.make_run(result={"error_code": STRATEGY_CODE})
        outcome, texts = self.sweep()
        self.assertEqual(outcome["queued"], 1)
        self.assertIn("Step: Writing an article.", texts[0])
        self.assertIn(f"Run {run.run_id}.", texts[0])
        self.assertIn("Stuck for 8 hours.", texts[0])

    def test_runs_the_founder_can_act_on_or_that_moved_on_do_not_alert(self):
        self.make_run(result={"error_code": FOUNDER_CODE})                         # her turn, not stuck
        self.assertEqual(self.sweep()[0]["candidates"], 0)
        self.make_run(result={"failure": {"action": {"id": "buy_points"}}})        # named action
        self.assertEqual(self.sweep()[0]["candidates"], 0)
        self.make_run(result={"self_heal_v1": {"next_at": (self.now + timedelta(hours=1)).isoformat()}})
        self.assertEqual(self.sweep()[0]["candidates"], 0)
        self.make_run(age=timedelta(hours=2))                                       # stopped, but recently
        self.assertEqual(self.sweep()[0]["candidates"], 0)
        older = self.make_run(age=timedelta(hours=30))
        self.make_run(status="running", age=timedelta(hours=7))                     # a newer run replaced it
        self.assertNotIn(older.run_id, str(stuck.find_stuck_conditions(now=self.now, since=self.now - timedelta(days=60))))
        self.assertEqual(self.sweep()[0]["candidates"], 0)

    def test_kill_switch_does_no_queries(self):
        self.operation()
        with override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False), self.assertNumQueries(0):
            self.assertEqual(stuck.run_founder_stuck_alert_sweep(now=self.now), {"status": "disabled"})

    def test_runs_at_most_once_per_interval(self):
        self.operation()
        self.assertEqual(self.sweep()[0]["status"], "completed")
        with self.assertNumQueries(0):
            self.assertEqual(stuck.run_founder_stuck_alert_sweep(now=self.now), {"status": "throttled"})

    def test_unconfigured_channel_still_records_and_logs(self):
        self.operation()
        with override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=""):
            outcome, texts = self.sweep()
        self.assertEqual((outcome["unconfigured"], texts), (1, []))
        self.assertEqual(self.receipts()[0]["state"], "unconfigured")

    def test_names_cannot_inject_slack_markup(self):
        Organization.objects.filter(pk=self.org.pk).update(name="x")
        VibeRaisingCompany.objects.filter(organization=self.org).update(name="<!channel> Evil\nStartup")
        self.operation()
        text = self.sweep()[1][0]
        self.assertIn("&lt;!channel&gt; Evil Startup", text)
        self.assertNotIn("<!channel>", text)


class FounderStuckBoundTests(FounderStuckFixture):
    """10 Nov 2026: an unbounded alert sweep OOM-killed the scheduler (#1325). This one must stay bounded."""

    def test_every_detection_query_is_limited_and_never_reads_whole_json_documents(self):
        for index in range(stuck.QUERY_LIMIT + 10):
            org, site = self.startup(f"Load {index}", f"load-{index}.example.test", "Load", str(index))
            self.operation(website=site)
            self.make_run(org=org, result={"error_code": STRATEGY_CODE, "verification_summary": "x" * 1000})
        with CaptureQueriesContext(connection) as queries:
            conditions = stuck.find_stuck_conditions(now=self.now, since=self.now - timedelta(days=60))
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(conditions), 2 * stuck.QUERY_LIMIT)
        for query in queries:
            sql = query["sql"]
            self.assertIn(f"LIMIT {stuck.QUERY_LIMIT}", sql)
            select = sql.split(" FROM ", 1)[0]
            # A whole JSON column is selected bare; single keys are read inside a JSON function call.
            for column in ('"content_factory_run"."result"', '"content_factory_run"."run_request"',
                           '"content_factory_website_operation"."receipt"', '"content_factory_website_operation"."payload"'):
                self.assertNotRegex(select, r"(?<!\()" + re.escape(column), column)
            self.assertNotIn("verification_summary", select)

    def test_one_sweep_queues_and_sends_a_bounded_number_of_alerts(self):
        for index in range(stuck.QUERY_LIMIT):
            _, site = self.startup(f"Load {index}", f"load-{index}.example.test", "Load", str(index))
            self.operation(website=site)
        with CaptureQueriesContext(connection) as queries:
            outcome, texts = self.sweep()
        self.assertEqual(outcome["queued"], stuck.MAX_ALERTS_PER_SWEEP)
        self.assertLessEqual(len(texts), stuck.MAX_ALERTS_PER_SWEEP)
        # Fixed work per alert; nothing grows with the number of startups or receipts.
        self.assertLessEqual(len(queries), 16 * stuck.MAX_ALERTS_PER_SWEEP)

