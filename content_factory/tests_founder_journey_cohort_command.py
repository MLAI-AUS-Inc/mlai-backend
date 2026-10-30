"""P1.12: the founder_journey_cohort report and apply command.

The production gate before launch: the report must show
``legacy_orgs_losing_an_action: 0`` and the five named orgs as legacy. The
metric comes from an inventory written independently of the legacy reasons, so
a planted new_journey marker on an org with history fails the gate.
"""
import json
from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import founder_journey_cohort as cohorts
from .founder_journey_testing import (
    ALL_P1_FLAGS_ON, LAUNCHED_AT, NAMED_LEGACY_ORGS, make_history_free_org, make_named_legacy_org,
    make_new_journey_org,
)
from .models import NotificationChannel, OrganizationContentConfig, WrittenArticle

PRE_LAUNCH = LAUNCHED_AT - timedelta(days=30)


def run_command(*args):
    out = StringIO()
    try:
        call_command("founder_journey_cohort", *args, stdout=out)
    except CommandError as exc:
        return out.getvalue(), exc.returncode
    return out.getvalue(), 0


def plant_marker(org, cohort_value, assigned_by="report_apply"):
    config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
    strategy = dict(config.pillar_strategy or {})
    strategy[cohorts.MARKER_KEY] = cohorts._base_marker(cohort_value, assigned_by, [])
    config.pillar_strategy = strategy
    config.save()


@override_settings(**ALL_P1_FLAGS_ON)
class ReportTests(TestCase):
    def setUp(self):
        self.named = {name: make_named_legacy_org(name, marker=False) for name in NAMED_LEGACY_ORGS}
        self.fresh = make_history_free_org()

    def rows(self):
        out, code = run_command("--json")
        return json.loads(out), code

    def test_named_orgs_are_legacy_and_a_history_free_org_is_proposed_new_journey(self):
        out, code = run_command()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip().splitlines()[-1], "legacy_orgs_losing_an_action: 0")
        report, _ = self.rows()
        by_domain = {row["domain"]: row for row in report["organizations"]}
        for name, built in self.named.items():
            with self.subTest(name=name):
                row = by_domain[built.organization.domain]
                self.assertEqual((row["effectiveCohort"], row["proposedCohort"]), ("legacy", "legacy"))
                self.assertIn("named_org", row["reasons"])
                self.assertTrue(row["inventory"])
                self.assertFalse(row["losingAnAction"])
        row = by_domain[self.fresh.organization.domain]
        self.assertEqual((row["effectiveCohort"], row["proposedCohort"], row["reasons"], row["inventory"]),
                         ("legacy", "new_journey", [], []))
        # The report writes nothing.
        self.assertTrue(all(cohorts.read_marker(Organization.objects.get(pk=row["id"])) is None
                            for row in report["organizations"]))

    def test_json_shape(self):
        report, code = self.rows()
        self.assertEqual(code, 0)
        self.assertEqual(set(report), {"organizations", "legacy_orgs_losing_an_action"})
        self.assertEqual(set(report["organizations"][0]), {
            "id", "domain", "markerCohort", "assignedBy", "effectiveCohort", "proposedCohort", "reasons",
            "inventory", "entitlement", "inFlightRuns", "losingAnAction"})
        self.assertEqual(len(report["organizations"]), Organization.objects.count())

    def test_in_flight_runs_are_listed(self):
        org = self.fresh.organization
        ContentFactoryRun.objects.create(run_id="run-live-1", workflow="article_generation", domain=org.domain,
                                         organization=org, status="running")
        report, code = self.rows()
        row = next(row for row in report["organizations"] if row["id"] == org.pk)
        self.assertEqual(row["inFlightRuns"], ["article_generation:running:run-live-1"])
        self.assertIn("open_article_run", row["inventory"])
        self.assertEqual(code, 0)  # legacy, so nothing is lost

    def test_planted_new_journey_marker_on_an_org_with_state_fails_the_gate(self):
        def written_article(org):
            WrittenArticle.objects.create(organization=org, title="T", slug="t", category="c", primary_keyword="t")

        def open_run(org):
            ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:10]}", workflow="article_generation",
                                             domain=org.domain, organization=org, status="awaiting_approval")

        def channel(org):
            NotificationChannel.objects.create(organization=org, channel_type="email", route_id="f@synthetic.example.test")

        for make, signal in ((written_article, "written_article"), (open_run, "open_article_run"),
                             (channel, "notification_channel")):
            with self.subTest(signal=signal):
                org = make_history_free_org().organization
                make(org)
                plant_marker(org, cohorts.NEW_JOURNEY)
                if signal != "written_article":
                    # Independence: the legacy reasons alone would miss these.
                    self.assertEqual(cohorts.legacy_reasons(org), [])
                out, code = run_command()
                self.assertEqual(code, 1)
                self.assertEqual(out.strip().splitlines()[-1], "legacy_orgs_losing_an_action: 1")
                self.assertIn(f"org={org.pk} ", out)
                report, _ = self.rows()
                row = next(row for row in report["organizations"] if row["id"] == org.pk)
                self.assertTrue(row["losingAnAction"])
                self.assertIn(signal, row["inventory"])
                plant_marker(org, cohorts.LEGACY)

    def test_named_org_planted_new_journey_counts(self):
        plant_marker(self.named["vyavos"].organization, cohorts.NEW_JOURNEY)
        _, code = run_command()
        self.assertEqual(code, 1)

    def test_post_launch_and_create_or_opt_in_markers_never_count(self):
        new = make_new_journey_org()
        WrittenArticle.objects.create(organization=new.organization, title="T", slug="t", category="c", primary_keyword="t")
        launch_only = make_new_journey_org(marker=False)
        ContentFactoryRun.objects.create(run_id="run-new-1", workflow="article_generation",
            domain=launch_only.organization.domain, organization=launch_only.organization, status="running")
        opted = make_history_free_org()
        WrittenArticle.objects.create(organization=opted.organization, title="T", slug="t", category="c", primary_keyword="t")
        plant_marker(opted.organization, cohorts.NEW_JOURNEY, assigned_by="opt_in")
        created = make_history_free_org()
        NotificationChannel.objects.create(organization=created.organization, channel_type="email", route_id="c@synthetic.example.test")
        plant_marker(created.organization, cohorts.NEW_JOURNEY, assigned_by="create")
        out, code = run_command()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "legacy_orgs_losing_an_action: 0")

    def test_without_launched_at_every_org_is_pre_launch(self):
        launch_only = make_new_journey_org(marker=False)
        WrittenArticle.objects.create(organization=launch_only.organization, title="T", slug="t", category="c",
                                      primary_keyword="t")
        plant_marker(launch_only.organization, cohorts.NEW_JOURNEY)
        _, code = run_command()
        self.assertEqual(code, 0)  # created after LAUNCHED_AT
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=""):
            _, code = run_command()
        self.assertEqual(code, 1)


@override_settings(**ALL_P1_FLAGS_ON)
class ApplyTests(TestCase):
    def test_apply_freezes_history_and_moves_only_history_free_orgs(self):
        named = {name: make_named_legacy_org(name, marker=False) for name in NAMED_LEGACY_ORGS}
        fresh = make_history_free_org()
        no_config = Organization.objects.create(domain="no-config.example.test", name="No config")
        Organization.objects.filter(pk=no_config.pk).update(created_at=PRE_LAUNCH)
        with_inventory = make_history_free_org()
        NotificationChannel.objects.create(organization=with_inventory.organization, channel_type="email",
                                           route_id="i@synthetic.example.test")
        OrganizationContentConfig.objects.filter(organization=with_inventory.organization).update(requires_review=True)
        existing = make_history_free_org()
        plant_marker(existing.organization, cohorts.LEGACY, assigned_by="opt_in")
        existing_before = cohorts.read_marker(existing.organization)
        sticky = make_history_free_org()
        config = sticky.config
        config.pillar_strategy = {"founder_journey": {"ownerClickRequired": True}, "pillars": [{"slug": "p"}]}
        config.save()
        post_launch = make_new_journey_org(marker=False)
        WrittenArticle.objects.create(organization=post_launch.organization, title="T", slug="t", category="c",
                                      primary_keyword="t")

        out = StringIO()
        call_command("founder_journey_cohort", "--apply", "--json", stdout=out)
        counts = json.loads(out.getvalue())
        self.assertEqual(counts, {"new_journey": 3, "legacy": 6, "skipped_existing_marker": 1,
                                  "skipped_launch_window": 1})

        for name, built in named.items():
            with self.subTest(name=name):
                marker = cohorts.read_marker(built.organization)
                self.assertEqual((marker["cohort"], marker["assignedBy"]), ("legacy", "report_apply"))
                self.assertIn("named_org", marker["reasons"])
                self.assertTrue(any(reason.startswith("inventory:") for reason in marker["reasons"]))
        self.assertEqual(cohorts.read_marker(fresh.organization)["cohort"], "new_journey")
        self.assertEqual(cohorts.read_marker(no_config)["cohort"], "new_journey")
        self.assertTrue(OrganizationContentConfig.objects.filter(organization=no_config).exists())
        frozen = cohorts.read_marker(with_inventory.organization)
        self.assertEqual(frozen["cohort"], "legacy")
        self.assertEqual(frozen["reasons"], ["inventory:notification_channel", "inventory:publish_settings"])
        self.assertEqual(cohorts.read_marker(existing.organization), existing_before)
        sticky_marker = cohorts.read_marker(sticky.organization)
        self.assertEqual(sticky_marker["cohort"], "new_journey")
        self.assertTrue(sticky_marker["ownerClickRequired"])
        self.assertEqual(OrganizationContentConfig.objects.get(organization=sticky.organization).pillar_strategy["pillars"],
                         [{"slug": "p"}])
        # A startup created after launch is left to the launch window.
        self.assertIsNone(cohorts.read_marker(post_launch.organization))
        self.assertTrue(cohorts.is_new_journey(post_launch.organization))

        snapshot = {config.organization_id: json.dumps(config.pillar_strategy, sort_keys=True)
                    for config in OrganizationContentConfig.objects.all()}
        out = StringIO()
        call_command("founder_journey_cohort", "--apply", "--json", stdout=out)
        self.assertEqual(json.loads(out.getvalue()), {"new_journey": 0, "legacy": 0, "skipped_existing_marker": 10,
                                                      "skipped_launch_window": 1})
        self.assertEqual({config.organization_id: json.dumps(config.pillar_strategy, sort_keys=True)
                          for config in OrganizationContentConfig.objects.all()}, snapshot)

        report_out, code = run_command()
        self.assertEqual(code, 0)
        self.assertEqual(report_out.strip().splitlines()[-1], "legacy_orgs_losing_an_action: 0")

    def test_plain_apply_output_lists_counts(self):
        make_history_free_org()
        out = StringIO()
        call_command("founder_journey_cohort", "--apply", stdout=out)
        self.assertIn("new_journey=1", out.getvalue())

    def test_report_and_apply_are_exclusive(self):
        with self.assertRaises(CommandError):
            call_command("founder_journey_cohort", "--report", "--apply", stdout=StringIO())

    def test_without_launched_at_apply_covers_every_org(self):
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=""):
            org = Organization.objects.create(domain="unset.example.test", name="Unset")
            out = StringIO()
            call_command("founder_journey_cohort", "--apply", "--json", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["new_journey"], 1)
        self.assertEqual(cohorts.read_marker(org)["cohort"], "new_journey")
        self.assertGreater(timezone.now(), LAUNCHED_AT)
