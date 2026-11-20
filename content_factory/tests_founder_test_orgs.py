"""Plan F1: one test-organisation marker, honoured by every founder report, alert, sweep and revenue number.

Acceptance: a marked organisation appears in no founder report or alert. One
test per surface: the funnel (both tables), the stuck alert, the founder-journey
alert queue and its Slack delivery, the goal-scoped operational (staff Slack)
alerts, the self-heal sweep, the background-job digest, the re-engagement report
and the revenue numbers. The marker and the command keep every guard: nothing
here changes a charge, a refund or a consent.
"""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import founder_journey_alerts as alerts
from . import founder_test_orgs
from .editorial_catalog import merge_strategy, public_strategy
from .models import OrganizationContentConfig, WrittenArticle
from .website_models import WebsiteConnection, WebsiteConnectionOperation

class Fixture(TestCase):
    def setUp(self):
        self.now = timezone.now() + timedelta(minutes=5)

    def founder(self, name, *, domain=None, joined=None):
        user = get_user_model().objects.create_user(email=f"{uuid4().hex[:10]}@synthetic.test")
        if joined:
            get_user_model().objects.filter(pk=user.pk).update(date_joined=joined)
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        org = Organization.objects.create(name=name, domain=domain or f"{uuid4().hex[:10]}.example.com")
        VibeRaisingCompany.objects.create(profile=profile, organization=org, name=name, domain=org.domain)
        return user, org

    def make_run(self, org, *, workflow="confirmed_topic", status="failed", code="SOME_STOP", age=timedelta(hours=8),
            request=None):
        run = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, domain=org.domain,
                                               workflow=workflow, status=status, run_request=request or {},
                                               result={"error_code": code} if code else {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run

    def mark(self, org):
        return founder_test_orgs.set_marker(org, marked=True, reason="catalogue test org")


class MarkerTests(Fixture):
    def test_marker_and_setting_override_are_one_helper(self):
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        self.assertFalse(founder_test_orgs.is_test_org(test))
        self.assertTrue(self.mark(test)["changed"])
        self.assertFalse(self.mark(test)["changed"])
        self.assertTrue(founder_test_orgs.is_test_org(test))
        self.assertTrue(founder_test_orgs.is_test_org(test.pk))
        self.assertFalse(founder_test_orgs.is_test_org(real))
        self.assertEqual(founder_test_orgs.test_organization_ids(), {test.pk})
        with override_settings(FOUNDER_TEST_ORGANIZATION_IDS=f"{real.pk}, nonsense, -3, 0"):
            self.assertEqual(founder_test_orgs.test_organization_ids(), {test.pk, real.pk})
            self.assertTrue(founder_test_orgs.is_test_org(real))
        founder_test_orgs.set_marker(test, marked=False)
        self.assertFalse(founder_test_orgs.is_test_org(test))

    def test_the_marker_survives_a_scan_and_is_hidden_from_workers(self):
        _, org = self.founder("xyz startup")
        self.mark(org)
        strategy = OrganizationContentConfig.objects.get(organization=org).pillar_strategy
        merged = merge_strategy(strategy, {"pillars": ["scan output"]})
        self.assertEqual(merged["test_organization"], strategy["test_organization"])
        self.assertNotIn("test_organization", public_strategy(merged))

    def test_a_malformed_marker_marks_nothing(self):
        _, org = self.founder("Real startup")
        OrganizationContentConfig.objects.create(organization=org, pillar_strategy={
            "test_organization": {"version": 2, "marked": True}})
        self.assertFalse(founder_test_orgs.is_test_org(org))
        OrganizationContentConfig.objects.filter(organization=org).update(pillar_strategy={
            "test_organization": {"version": 1, "marked": "yes"}})
        self.assertEqual(founder_test_orgs.test_organization_ids(), frozenset())

    def test_the_command_is_a_dry_run_by_default(self):
        _, org = self.founder("xyz startup")
        out, err = StringIO(), StringIO()
        call_command("mark_test_organization", "--organization-id", str(org.pk), "--reason", "test", stdout=out,
                     stderr=err)
        self.assertEqual(json.loads(out.getvalue())["result"], "dry_run")
        self.assertIn("Dry run", err.getvalue())
        self.assertFalse(founder_test_orgs.is_test_org(org))
        with self.assertRaises(CommandError):
            call_command("mark_test_organization", "--organization-id", str(org.pk), "--apply", stdout=StringIO())
        out = StringIO()
        call_command("mark_test_organization", "--organization-id", str(org.pk), "--reason", "test", "--apply",
                     stdout=out)
        self.assertEqual(json.loads(out.getvalue())["result"], "changed")
        self.assertTrue(founder_test_orgs.is_test_org(org))
        out = StringIO()
        call_command("mark_test_organization", "--list", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["testOrganizations"],
                         [{"organizationId": org.pk, "name": "xyz startup", "marker": True, "setting": False}])
        out = StringIO()
        call_command("mark_test_organization", "--organization-id", str(org.pk), "--unmark", "--apply", stdout=out)
        self.assertFalse(founder_test_orgs.is_test_org(org))


class FunnelTests(Fixture):
    def test_marked_org_is_in_neither_funnel_table(self):
        from .founder_funnel import journey_funnel, organization_funnel
        from .management.commands.founder_funnel_report import founder_funnel
        joined = self.now - timedelta(days=2)
        self.founder("Real startup", joined=joined)
        _, test = self.founder("xyz startup", joined=joined)
        self.make_run(test, workflow="island_refresh", request={"request_source": alerts.SCHEDULER_REQUEST_SOURCE})
        before = journey_funnel(since=self.now - timedelta(days=3), until=self.now)
        self.assertEqual(before["stages"]["signed_up"]["users"], 2)
        self.assertEqual(organization_funnel()["organizations"], 2)
        self.mark(test)
        after = journey_funnel(since=self.now - timedelta(days=3), until=self.now)
        self.assertEqual(after["stages"]["signed_up"]["users"], 1)
        self.assertEqual(after["stages"]["startup_created"]["users"], 1)
        self.assertEqual(after["approved"]["organizations"], 1)
        self.assertEqual(organization_funnel()["organizations"], 1)
        legacy = founder_funnel(since=self.now - timedelta(days=3), until=self.now)
        self.assertEqual(legacy["funnel"]["users"], 1)
        self.assertEqual(legacy["startup_companies_created"], 1)
        self.assertEqual(legacy["island_refresh_platform_wide"]["failed"], 0)


class AlertTests(Fixture):
    def test_queue_and_batch_write_no_receipt_for_a_marked_org(self):
        _, org = self.founder("xyz startup")
        run = self.make_run(org)
        self.mark(org)
        alert_id = alerts.queue_founder_journey_alert(organization=org, alert_reason="run_terminal", run=run)
        self.assertTrue(alert_id)
        batch = alerts.queue_founder_journey_alerts_batch(
            [{"organization": org, "alert_reason": "run_terminal", "run": run}])
        self.assertEqual(batch, {"ids": [], "created": 0})
        strategy = OrganizationContentConfig.objects.get(organization=org).pillar_strategy
        self.assertNotIn(alerts.RECEIPTS_KEY, strategy)
        run.refresh_from_db()
        self.assertNotIn(alerts.RECEIPTS_KEY, run.result)

    def test_detection_skips_a_marked_org_and_never_spends_its_budget(self):
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        self.make_run(real)
        self.make_run(test)
        self.mark(test)
        counts = alerts._detect(now=self.now, since=self.now - timedelta(days=30), limit=1)
        self.assertEqual(counts["run_terminal"], 1)
        orgs = {organization_id for organization_id, _ in alerts._receipt_index()}
        self.assertEqual(orgs, {real.pk})

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0123456789")
    def test_a_receipt_queued_before_marking_is_never_posted_to_slack(self):
        _, org = self.founder("xyz startup")
        alerts.queue_founder_journey_alert(organization=org, alert_reason="run_terminal", run=self.make_run(org))
        self.mark(org)
        with patch.object(alerts, "_post") as post:
            outcome = alerts._deliver(now=self.now, limit=10)
        post.assert_not_called()
        self.assertEqual(outcome, {"sent": 0, "unconfigured": 0, "undelivered": 0})

    def test_stuck_alert_never_finds_a_marked_org(self):
        from .founder_stuck_alerts import find_stuck_conditions
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        self.make_run(real, workflow="confirmed_topic")
        self.make_run(test, workflow="confirmed_topic")
        connection = WebsiteConnection.objects.create(organization=test, github_repo="synthetic/test")
        WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation,
                                                  idempotency_key=uuid4().hex, action="prepare",
                                                  state="needs_attention", receipt={"reason_code": "SOME_STOP"})
        self.assertEqual({c.organization_id for c in find_stuck_conditions(now=self.now)}, {real.pk, test.pk})
        self.mark(test)
        self.assertEqual({c.organization_id for c in find_stuck_conditions(now=self.now)}, {real.pk})

    @override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True)
    def test_operational_staff_slack_alerts_are_off_for_a_marked_org(self):
        from . import website_operational_alert_delivery as delivery
        _, org = self.founder("xyz startup")
        connection = WebsiteConnection.objects.create(organization=org, github_repo="synthetic/test")
        with patch("content_factory.website_goal.enrolled", return_value=True):
            self.assertTrue(delivery._delivers(connection))
            self.mark(org)
            self.assertFalse(delivery._delivers(connection))

    @override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True, ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE="2026-01-01")
    def test_staff_alert_rules_neither_alert_nor_count_a_marked_org(self):
        from django.core.cache import cache

        from . import staff_alert_rules
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        for org in (real, test):
            OrganizationContentConfig.objects.get_or_create(organization=org)
            # A codeless terminal stop raises the staff "codeless_terminal" rule.
            self.make_run(org, code=None, age=timedelta(minutes=2))
        self.mark(test)
        cache.clear()
        with patch.object(staff_alert_rules, "queue_founder_journey_alerts_batch") as batch:
            outcome = staff_alert_rules.run_alert_rule_sweep(now=self.now)
        self.assertEqual(outcome["organizations"], 1)
        queued = {item["organization"].pk for item in batch.call_args.args[0]}
        self.assertEqual(queued, {real.pk})

    def test_background_digest_neither_alerts_nor_counts_a_marked_org(self):
        from . import background_job_digest
        _, org = self.founder("xyz startup")
        for night in range(3):
            self.make_run(org, workflow="island_refresh", age=timedelta(days=night, hours=1),
                     request={"request_source": alerts.SCHEDULER_REQUEST_SOURCE})
        self.assertIn(org.pk, {key[0] for key in background_job_digest._nights(self.now)})
        self.mark(org)
        self.assertEqual(dict(background_job_digest._nights(self.now)), {})


class SweepAndReportTests(Fixture):
    def test_self_heal_never_sweeps_a_marked_org(self):
        from . import failure_policy_engine
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        self.make_run(real)
        self.make_run(test)
        self.assertEqual({run.organization_id for run in failure_policy_engine._candidates(self.now, 50)}, {real.pk, test.pk})
        self.mark(test)
        self.assertEqual({run.organization_id for run in failure_policy_engine._candidates(self.now, 50)}, {real.pk})

    def test_reengagement_report_leaves_out_a_marked_org(self):
        from . import founder_reengagement
        _, real = self.founder("Real startup")
        _, test = self.founder("xyz startup")
        report = founder_reengagement.build_report(now=self.now)
        self.assertEqual({row["organizationId"] for row in report["organizations"]}, {real.pk, test.pk})
        self.mark(test)
        report = founder_reengagement.build_report(now=self.now)
        self.assertEqual({row["organizationId"] for row in report["organizations"]}, {real.pk})

    def test_revenue_numbers_leave_out_a_marked_org(self):
        from . import admin_metrics
        real_user, real = self.founder("Real startup")
        test_user, test = self.founder("xyz startup")
        for user, org in ((real_user, real), (test_user, test)):
            ledger = Ledger.objects.create(user=user, delta=-10, kind="SPEND", source="CONTENT_FACTORY")
            self.make_run(org, status="completed", code=None, age=timedelta(hours=1),
                     request={"roo_points_ledger_id": ledger.pk})
            WrittenArticle.objects.create(organization=org, title=org.name, slug=uuid4().hex[:8])
        start, end = self.now - timedelta(days=1), self.now + timedelta(minutes=1)
        before, _ = admin_metrics._summary_for_period(start, end)
        self.assertEqual((before["activeStartups"], before["articlesCreated"], before["grossPointsSpent"]), (2, 2, 20))
        self.mark(test)
        after, _ = admin_metrics._summary_for_period(start, end)
        self.assertEqual((after["activeStartups"], after["articlesCreated"], after["grossPointsSpent"]), (1, 1, 10))
