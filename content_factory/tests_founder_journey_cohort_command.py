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
from .management.commands.founder_journey_cohort import apply_markers
from .models import (
    ContentIsland, ContentIslandStatus, NotificationChannel, OrganizationContentConfig, ResearchedKeyword,
    WrittenArticle,
)
from .services import island_refresh_scheduler

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
            "inventory", "priorState", "entitlement", "inFlightRuns", "losingAnAction"})
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
                                  "skipped_launch_window": 1, "legacy_no_config": 0})

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
                                                      "skipped_launch_window": 1, "legacy_no_config": 0})
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


def written_article(org, *, created_at=None):
    article = WrittenArticle.objects.create(organization=org, title="T", slug=f"t-{uuid4().hex[:6]}", category="c",
                                            primary_keyword="t")
    if created_at is not None:
        WrittenArticle.objects.filter(pk=article.pk).update(created_at=created_at)
    return article


def row_for(org):
    out, code = run_command("--json")
    report = json.loads(out)
    return next(row for row in report["organizations"] if row["id"] == org.pk), report, code


@override_settings(**ALL_P1_FLAGS_ON)
class LaunchWindowHistoryTests(TestCase):
    """An org created after LAUNCHED_AT that has history from before it is flagged, then frozen as legacy."""

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT="2026-01-01T00:00:00+00:00")
    def test_a_named_org_created_after_a_mis_set_launch_time_is_flagged_and_frozen(self):
        pawshare = make_named_legacy_org("pawshare", marker=False)
        self.assertTrue(cohorts.is_new_journey(pawshare.organization))  # what --apply must fix
        row, _, code = row_for(pawshare.organization)
        self.assertEqual(code, 1)
        self.assertTrue(row["losingAnAction"])
        self.assertIn("named_org", row["priorState"])

        counts = apply_markers()
        self.assertEqual((counts["legacy"], counts["skipped_launch_window"]), (1, 0))
        marker = cohorts.read_marker(pawshare.organization)
        self.assertEqual((marker["cohort"], marker["assignedBy"]), ("legacy", "report_apply"))
        self.assertIn("before_launch:named_org", marker["reasons"])
        self.assertIn("hosted_site", marker["reasons"])
        self.assertFalse(cohorts.is_new_journey(pawshare.organization))
        _, report, code = row_for(pawshare.organization)
        self.assertEqual((code, report["legacy_orgs_losing_an_action"]), (0, 0))

    def test_an_org_created_after_launch_with_earlier_state_is_frozen(self):
        late = make_new_journey_org(marker=False)
        written_article(late.organization, created_at=LAUNCHED_AT - timedelta(days=5))
        row, _, code = row_for(late.organization)
        self.assertEqual((code, row["priorState"]), (1, ["written_article"]))

        counts = apply_markers()
        self.assertEqual(counts["legacy"], 1)
        marker = cohorts.read_marker(late.organization)
        self.assertEqual(marker["cohort"], "legacy")
        self.assertIn("before_launch:written_article", marker["reasons"])
        _, _, code = row_for(late.organization)
        self.assertEqual(code, 0)

    def test_state_added_after_launch_leaves_the_launch_window_alone(self):
        late = make_new_journey_org(marker=False)
        written_article(late.organization)
        created = make_new_journey_org()
        written_article(created.organization, created_at=LAUNCHED_AT - timedelta(days=5))
        _, report, code = row_for(late.organization)
        self.assertEqual((code, report["legacy_orgs_losing_an_action"]), (0, 0))  # create markers never count
        counts = apply_markers()
        self.assertEqual((counts["skipped_launch_window"], counts["skipped_existing_marker"]), (1, 1))
        self.assertIsNone(cohorts.read_marker(late.organization))
        self.assertTrue(cohorts.is_new_journey(late.organization))


@override_settings(**ALL_P1_FLAGS_ON)
class AfterApplyTests(TestCase):
    def test_new_journey_state_added_after_apply_never_counts(self):
        fresh = make_history_free_org()
        self.assertEqual(apply_markers()["new_journey"], 1)
        assigned = parse_assigned_at(fresh.organization)
        later = assigned + timedelta(minutes=1)
        written_article(fresh.organization, created_at=later)
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:10]}", workflow="article_generation",
                                               domain=fresh.organization.domain, organization=fresh.organization,
                                               status="awaiting_approval")
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=later)
        channel = NotificationChannel.objects.create(organization=fresh.organization, channel_type="email",
                                                     route_id="a@synthetic.example.test")
        NotificationChannel.objects.filter(pk=channel.pk).update(created_at=later)
        row, report, code = row_for(fresh.organization)
        self.assertEqual((row["effectiveCohort"], row["priorState"], row["losingAnAction"]), ("new_journey", [], False))
        self.assertEqual((code, report["legacy_orgs_losing_an_action"]), (0, 0))
        out, code = run_command()
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "legacy_orgs_losing_an_action: 0"))


def parse_assigned_at(org):
    from django.utils.dateparse import parse_datetime
    return parse_datetime(cohorts.read_marker(org)["assignedAt"])


@override_settings(**ALL_P1_FLAGS_ON)
class IslandRefreshEnrolmentTests(TestCase):
    """--apply never creates a config row that would enrol an org in the island refresh."""

    def _research(self, org):
        ResearchedKeyword.objects.create(organization=org, keyword="kw", keyword_normalized="kw", volume=100)
        ContentIsland.objects.create(organization=org, slug="island", name="Island", pillar_keyword="island",
                                     status=ContentIslandStatus.VISIBLE, centroid_embedding=[1.0, 0.0])

    def _no_config_org(self, domain):
        org = Organization.objects.create(domain=domain, name="No config")
        Organization.objects.filter(pk=org.pk).update(created_at=PRE_LAUNCH)
        org.refresh_from_db()
        return org

    def test_apply_leaves_eligibility_unchanged(self):
        legacy = self._no_config_org("legacy-research.example.test")
        written_article(legacy)
        self._research(legacy)
        quiet = self._no_config_org("quiet-research.example.test")
        self._research(quiet)
        eligible_before = [config.organization_id for config in island_refresh_scheduler._eligible_configs()]

        counts = apply_markers()

        self.assertEqual(counts["legacy_no_config"], 2)
        self.assertFalse(OrganizationContentConfig.objects.filter(organization__in=[legacy, quiet]).exists())
        self.assertEqual([config.organization_id for config in island_refresh_scheduler._eligible_configs()],
                         eligible_before)
        for org in (legacy, quiet):
            with self.subTest(domain=org.domain):
                self.assertEqual(cohorts.cohort(org), "legacy")
                self.assertFalse(cohorts.is_new_journey(org))
        _, report, code = row_for(legacy)
        self.assertEqual((code, report["legacy_orgs_losing_an_action"]), (0, 0))
        self.assertEqual(apply_markers()["legacy_no_config"], 2)  # still idempotent
