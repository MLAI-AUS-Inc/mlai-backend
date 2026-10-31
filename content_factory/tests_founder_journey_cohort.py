"""P1.12: the sticky founder-journey cohort (IF-P1-01, IF-P1-02).

The new journey applies only to startups created after launch (and to
history-free orgs once the report is applied). The five named orgs and every
org with publishing history keep today's path even with every P1 flag on, and
a lost verification proof can never move an org between cohorts.
"""
import json
import re
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import editorial_catalog
from . import founder_journey_cohort as cohorts
from .founder_journey_testing import (
    ALL_P1_FLAGS_ON, LAUNCHED_AT, NAMED_LEGACY_ORGS, P1_SETTING_DEFAULTS, make_history_free_org,
    make_named_legacy_org, make_new_journey_org,
)
from .hosted_article_models import HostedArticleSite
from .models import NotificationChannel, OrganizationContentConfig, WrittenArticle
from .services import island_refresh_scheduler
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget

FLAGS = ALL_P1_FLAGS_ON
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


def _set_marker(config, marker):
    strategy = dict(config.pillar_strategy or {})
    strategy[cohorts.MARKER_KEY] = marker
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def _marker(cohort_value, assigned_by="report_apply", **extra):
    return {**cohorts._base_marker(cohort_value, assigned_by, []), **extra}


def _org(domain=None, *, created_at=None, config=True):
    org = Organization.objects.create(domain=domain or f"org-{uuid4().hex[:8]}.example.test", name="Org")
    if created_at is not None:
        Organization.objects.filter(pk=org.pk).update(created_at=created_at)
        org.refresh_from_db()
    if config:
        OrganizationContentConfig.objects.create(organization=org)
    return org


def _config(org):
    return OrganizationContentConfig.objects.get(organization=org)


PRE_LAUNCH = LAUNCHED_AT - timedelta(days=30)


def _claiming_legacy(bad):
    """The same malformed shape, but claiming legacy instead of new_journey."""
    if isinstance(bad, dict):
        return {**bad, "cohort": "legacy" if bad.get("cohort") == "new_journey" else bad.get("cohort")}
    if isinstance(bad, list):
        return ["legacy"]
    return "legacy"


def _tables(queries):
    return {table for query in queries for table in re.findall(r'(?:FROM|JOIN)\s+"?(\w+)"?', query["sql"])}


@override_settings(**FLAGS)
class CohortPredicateTests(TestCase):
    def test_disabled_by_id_or_domain_beats_a_new_journey_marker(self):
        built = make_new_journey_org(domain="Disabled-Startup.example.test")
        org = built.organization
        self.assertEqual(cohorts.cohort(org), cohorts.NEW_JOURNEY)
        for entry in (str(org.pk), "DISABLED-startup.example.test", f" other.example.test , {org.pk} "):
            with self.subTest(entry=entry), override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=entry):
                self.assertEqual(cohorts.cohort(org), cohorts.LEGACY)
                self.assertFalse(cohorts.is_new_journey(org))
                # The raw marker survives the kill switch.
                self.assertEqual(cohorts.read_marker(org)["cohort"], cohorts.NEW_JOURNEY)

    def test_a_valid_marker_beats_launched_at(self):
        after_launch = _org(created_at=timezone.now())
        _set_marker(_config(after_launch), _marker(cohorts.LEGACY))
        self.assertEqual(cohorts.cohort(after_launch), cohorts.LEGACY)
        before_launch = _org(created_at=PRE_LAUNCH)
        _set_marker(_config(before_launch), _marker(cohorts.NEW_JOURNEY))
        self.assertEqual(cohorts.cohort(before_launch), cohorts.NEW_JOURNEY)

    def test_malformed_wrong_version_and_unknown_cohort_markers_are_ignored(self):
        bad_markers = ("new_journey", ["new_journey"], {"cohort": "new_journey"},
                       {"version": 2, "cohort": "new_journey"}, {"version": "1", "cohort": "new_journey"},
                       {"version": True, "cohort": "new_journey"}, {"version": 1, "cohort": "beta"})
        for bad in bad_markers:
            with self.subTest(marker=bad):
                before = _org(created_at=PRE_LAUNCH)
                _set_marker(_config(before), bad)
                self.assertEqual(cohorts.cohort(before), cohorts.LEGACY)
                after = _org(created_at=timezone.now())
                _set_marker(_config(after), _claiming_legacy(bad))
                self.assertEqual(cohorts.cohort(after), cohorts.NEW_JOURNEY)

    def test_no_marker_follows_launched_at(self):
        self.assertEqual(cohorts.cohort(_org(created_at=PRE_LAUNCH)), cohorts.LEGACY)
        self.assertEqual(cohorts.cohort(_org(created_at=timezone.now())), cohorts.NEW_JOURNEY)
        self.assertEqual(cohorts.cohort(_org(created_at=timezone.now(), config=False)), cohorts.NEW_JOURNEY)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=""):
            self.assertEqual(cohorts.cohort(_org(created_at=timezone.now())), cohorts.LEGACY)

    def test_unparsable_or_naive_launched_at_counts_as_unset_and_warns_once(self):
        org = _org(created_at=timezone.now())
        for raw in ("not-a-date", "2026-10-01T09:00:00"):
            with self.subTest(raw=raw), override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=raw):
                cohorts._warned_launch_values.discard(raw)
                with self.assertLogs(cohorts.logger, "WARNING") as logs:
                    self.assertIsNone(cohorts.launched_at())
                    self.assertEqual(cohorts.cohort(org), cohorts.LEGACY)
                    self.assertEqual(cohorts.new_startup_cohort(), cohorts.LEGACY)
                self.assertEqual(len([line for line in logs.output if "founder_journey_launched_at_ignored" in line]), 1)

    def test_is_new_journey_needs_v2_even_with_a_new_journey_marker(self):
        org = make_new_journey_org().organization
        self.assertTrue(cohorts.is_new_journey(org))
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertEqual(cohorts.cohort(org), cohorts.NEW_JOURNEY)
            self.assertFalse(cohorts.is_new_journey(org))

    def test_new_startup_cohort_before_and_after_launch(self):
        self.assertEqual(cohorts.new_startup_cohort(), cohorts.NEW_JOURNEY)
        self.assertEqual(cohorts.new_startup_cohort(now=LAUNCHED_AT - timedelta(seconds=1)), cohorts.LEGACY)
        later = (timezone.now() + timedelta(hours=2)).isoformat()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=later):
            self.assertEqual(cohorts.new_startup_cohort(), cohorts.LEGACY)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertEqual(cohorts.new_startup_cohort(), cohorts.LEGACY)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=""):
            self.assertEqual(cohorts.new_startup_cohort(), cohorts.LEGACY)


@override_settings(**FLAGS)
class FlapTests(TestCase):
    """G2: a lost proof, a paused or disconnected connection never moves an org."""

    def _flap(self, org, expected):
        def check(step):
            with self.subTest(step=step):
                config = _config(org)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(cohorts.cohort(org), expected)
                self.assertEqual(len(queries), 1)
                self.assertEqual(_tables(queries.captured_queries), {"content_factory_org_config"})
                with self.assertNumQueries(0):
                    self.assertEqual(cohorts.cohort(org, config), expected)
        check("start")
        WebsiteConnection.objects.filter(organization=org).update(state=WebsiteConnection.State.PAUSED)
        check("paused")
        WebsiteConnection.objects.filter(organization=org).update(state=WebsiteConnection.State.DISCONNECTED)
        check("disconnected")
        WebsiteConnectionTarget.objects.filter(connection__organization=org).update(verified_at=None)
        WebsiteConnection.objects.filter(organization=org).update(last_verified_at=None, verified_sha="")
        check("verification_dropped")
        HostedArticleSite.objects.filter(organization=org).delete()
        check("hosted_site_removed")
        WebsiteConnection.objects.filter(organization=org).delete()
        check("connection_deleted")

    def test_legacy_org_stays_legacy(self):
        for marker in (True, False):
            built = make_named_legacy_org("pawshare", marker=marker,
                                          domain=None if marker else "pawshare-no-marker.example.test")
            self._flap(built.organization, cohorts.LEGACY)

    def test_new_journey_org_stays_new_journey(self):
        for marker in (True, False):
            built = make_new_journey_org(marker=marker)
            org = built.organization
            connection_row = WebsiteConnection.objects.create(organization=org, github_repo="example/new",
                state=WebsiteConnection.State.CONNECTED)
            WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=1,
                verified_at=timezone.now())
            HostedArticleSite.objects.create(organization=org, domain=org.domain, state=HostedArticleSite.State.ACTIVE)
            self._flap(org, cohorts.NEW_JOURNEY)


@override_settings(**FLAGS)
class PayloadTests(TestCase):
    def test_kill_switch_hides_payloads_but_keeps_the_raw_marker(self):
        built = make_new_journey_org()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertIsNone(cohorts.public_payload(built.organization, user=built.owner))
            self.assertIsNone(cohorts.account_payload(built.owner))
            self.assertEqual(cohorts.read_marker(built.organization)["cohort"], cohorts.NEW_JOURNEY)

    def test_public_payload_shapes(self):
        new = make_new_journey_org()
        self.assertEqual(cohorts.public_payload(new.organization, user=new.owner), {
            "version": 1, "cohort": "new_journey", "optInAvailable": False, "staffView": False,
            "copyReadyBeforeVerification": True})
        legacy = make_named_legacy_org("vyavos")
        self.assertEqual(cohorts.public_payload(legacy.organization, user=legacy.owner), {
            "version": 1, "cohort": "legacy", "optInAvailable": True, "staffView": False,
            "copyReadyBeforeVerification": False})
        staff = User.objects.create_user(email="staff@synthetic.example.test", is_staff=True)
        payload = cohorts.public_payload(legacy.organization, user=staff)
        self.assertTrue(payload["staffView"])
        self.assertFalse(payload["optInAvailable"])  # not the owner
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED=False):
            self.assertFalse(cohorts.public_payload(legacy.organization, user=legacy.owner)["optInAvailable"])
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS="vyavos.com"):
            self.assertFalse(cohorts.public_payload(legacy.organization, user=legacy.owner)["optInAvailable"])
        with override_settings(ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED=False):
            self.assertFalse(cohorts.public_payload(new.organization, user=new.owner)["copyReadyBeforeVerification"])
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=str(new.organization.pk)):
            self.assertEqual(cohorts.public_payload(new.organization, user=new.owner)["cohort"], "legacy")

    def test_account_payload_lists_only_new_journey_companies(self):
        new = make_new_journey_org()
        legacy_org = _org(created_at=PRE_LAUNCH)
        second = VibeRaisingCompany.objects.create(profile=new.owner.vibe_raising_profile, organization=legacy_org,
                                                   name="Old", domain=legacy_org.domain)
        launch_only = _org(created_at=timezone.now(), config=False)
        third = VibeRaisingCompany.objects.create(profile=new.owner.vibe_raising_profile, organization=launch_only,
                                                  name="Launch", domain=launch_only.domain)
        payload = cohorts.account_payload(new.owner)
        self.assertEqual(payload, {"version": 1, "newStartupCohort": "new_journey",
            "companies": {str(new.company.pk): "new_journey", str(third.pk): "new_journey"}})
        self.assertNotIn(str(second.pk), payload["companies"])
        self.assertTrue(cohorts.user_has_legacy_org(new.owner))
        loner = make_new_journey_org()
        self.assertFalse(cohorts.user_has_legacy_org(loner.owner))
        nobody = User.objects.create_user(email="nobody@synthetic.example.test")
        self.assertEqual(cohorts.account_payload(nobody)["companies"], {})
        self.assertFalse(cohorts.user_has_legacy_org(nobody))


@override_settings(**FLAGS)
class UpdateMarkerTests(TestCase):
    def test_merges_only_allowlisted_keys_and_keeps_the_rest(self):
        built = make_new_journey_org()
        config = built.config
        strategy = dict(config.pillar_strategy)
        strategy.update(pillars=[{"slug": "a"}], startup_profile_details={"x": 1}, founder_first_profile={"y": 2})
        config.pillar_strategy = strategy
        config.save()
        before = json.dumps({k: v for k, v in strategy.items() if k != cohorts.MARKER_KEY}, sort_keys=True)
        old_marker = cohorts.read_marker(built.organization)
        marker = cohorts.update_marker(built.organization, github_return={"reason": "connected", "at": "t"},
            publish_automatically=None, owner_click_required=True,
            setup_approval={"runId": "r", "merge": "manual", "at": "t", "by": 1})
        self.assertEqual(marker["githubReturn"], {"reason": "connected", "at": "t"})
        self.assertTrue(marker["ownerClickRequired"])
        self.assertEqual(marker["setupApproval"]["merge"], "manual")
        self.assertIsNone(marker["publishAutomatically"])
        for key in ("cohort", "assignedAt", "assignedBy", "createdCompanyId", "reasons"):
            self.assertEqual(marker[key], old_marker[key], key)
        stored = _config(built.organization).pillar_strategy
        self.assertEqual(json.dumps({k: v for k, v in stored.items() if k != cohorts.MARKER_KEY}, sort_keys=True), before)
        self.assertEqual(stored[cohorts.MARKER_KEY], marker)
        # Only the arguments passed change.
        again = cohorts.update_marker(built.organization, owner_click_required=False)
        self.assertEqual(again["githubReturn"], {"reason": "connected", "at": "t"})
        self.assertFalse(again["ownerClickRequired"])
        with self.assertRaises(TypeError):
            cohorts.update_marker(built.organization, cohort="legacy")

    def test_is_a_no_op_for_legacy_orgs(self):
        for built in (make_named_legacy_org("talathrive"), make_history_free_org()):
            before = json.dumps(_config(built.organization).pillar_strategy, sort_keys=True)
            self.assertIsNone(cohorts.update_marker(built.organization, owner_click_required=True))
            self.assertEqual(json.dumps(_config(built.organization).pillar_strategy, sort_keys=True), before)
        # A disabled org without a raw new_journey marker stays untouched too.
        launch_only = _org(created_at=timezone.now())
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=launch_only.domain):
            self.assertIsNone(cohorts.update_marker(launch_only, owner_click_required=True))
        self.assertIsNone(cohorts.read_marker(launch_only))

    def test_still_writes_for_a_raw_new_journey_marker_while_v2_is_off_or_disabled(self):
        built = make_new_journey_org()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertTrue(cohorts.update_marker(built.organization, owner_click_required=True)["ownerClickRequired"])
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=built.organization.domain):
            marker = cohorts.update_marker(built.organization, github_return={"reason": "r", "at": "t"})
        self.assertTrue(marker["ownerClickRequired"])
        self.assertEqual(cohorts.read_marker(built.organization)["githubReturn"]["reason"], "r")

    def test_creates_a_launch_window_marker_and_a_missing_config(self):
        org = _org(created_at=timezone.now(), config=False)
        marker = cohorts.update_marker(org, owner_click_required=True)
        self.assertEqual(marker["cohort"], "new_journey")
        self.assertEqual(marker["assignedBy"], "launch_window")
        self.assertEqual(marker["reasons"], ["created_after_launch"])
        self.assertTrue(marker["ownerClickRequired"])
        self.assertEqual(_config(org).pillar_strategy[cohorts.MARKER_KEY], marker)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            other = _org(created_at=timezone.now(), config=False)
            self.assertIsNone(cohorts.update_marker(other, owner_click_required=True))
            self.assertFalse(OrganizationContentConfig.objects.filter(organization=other).exists())


@override_settings(**FLAGS)
class AssignOnCreateTests(TestCase):
    def test_writes_only_when_all_four_conditions_hold(self):
        org = _org(created_at=timezone.now())
        self.assertEqual(cohorts.assign_on_create(org, company_id="c-1", create_key_sha256="f" * 64), "new_journey")
        marker = cohorts.read_marker(org)
        self.assertEqual((marker["cohort"], marker["assignedBy"], marker["reasons"]),
                         ("new_journey", "create", ["created_after_launch"]))
        self.assertEqual((marker["createKeySha256"], marker["createdCompanyId"]), ("f" * 64, "c-1"))

        older = _org(created_at=PRE_LAUNCH)
        self.assertEqual(cohorts.assign_on_create(older, company_id="c-2"), "legacy")
        self.assertIsNone(cohorts.read_marker(older))
        for override in ({"MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False},
                         {"MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT": ""}):
            with self.subTest(override=override), override_settings(**override):
                fresh = _org(created_at=timezone.now())
                cohorts.assign_on_create(fresh, company_id="c-3")
                self.assertIsNone(cohorts.read_marker(fresh))
        marked = _org(created_at=timezone.now())
        _set_marker(_config(marked), _marker(cohorts.LEGACY))
        self.assertEqual(cohorts.assign_on_create(marked, company_id="c-4"), "legacy")
        self.assertEqual(cohorts.read_marker(marked)["assignedBy"], "report_apply")
        missing_config = _org(created_at=timezone.now(), config=False)
        self.assertEqual(cohorts.assign_on_create(missing_config, company_id="c-5"), "new_journey")
        self.assertEqual(cohorts.read_marker(missing_config)["createdCompanyId"], "c-5")

    def test_find_company_for_create_key(self):
        key = "a" * 64
        built = make_new_journey_org(create_key_sha256=key)
        profile = built.owner.vibe_raising_profile
        self.assertEqual(cohorts.find_company_for_create_key(profile, key), built.company)
        self.assertIsNone(cohorts.find_company_for_create_key(profile, "b" * 64))
        other = make_new_journey_org(create_key_sha256=key)
        self.assertEqual(cohorts.find_company_for_create_key(other.owner.vibe_raising_profile, key), other.company)
        stranger = VibeRaisingProfile.objects.create(user=User.objects.create_user(email="s@synthetic.example.test"),
                                                     role="founder")
        self.assertIsNone(cohorts.find_company_for_create_key(stranger, key))
        config = _config(built.organization)
        marker = dict(config.pillar_strategy[cohorts.MARKER_KEY])
        marker["assignedAt"] = (timezone.now() - timedelta(hours=25)).isoformat()
        _set_marker(config, marker)
        self.assertIsNone(cohorts.find_company_for_create_key(profile, key))
        self.assertEqual(cohorts.find_company_for_create_key(profile, key, within_hours=48), built.company)


@override_settings(**FLAGS)
class OptInTests(TestCase):
    def test_existing_publisher_keeps_review_drafts(self):
        built = make_named_legacy_org("talathrive")
        reasons = cohorts.read_marker(built.organization)["reasons"]
        with self.assertLogs(cohorts.logger, "INFO") as logs:
            payload = cohorts.opt_in(built.organization, built.owner)
        self.assertIn(f"founder_journey_opt_in org={built.organization.pk}", "\n".join(logs.output))
        self.assertEqual(payload["cohort"], "new_journey")
        self.assertFalse(payload["optInAvailable"])
        marker = cohorts.read_marker(built.organization)
        self.assertEqual((marker["cohort"], marker["assignedBy"], marker["optedInBy"]), ("new_journey", "opt_in", built.owner.pk))
        self.assertEqual(marker["reasons"], reasons)
        self.assertEqual(marker["publishAutomatically"]["origin"], "opt_in_existing_publisher")
        self.assertEqual(marker["publishAutomatically"]["enabledBy"], built.owner.pk)
        self.assertTrue(cohorts.is_new_journey(built.organization))

    def test_history_free_org_gets_no_publish_opt_in(self):
        built = make_history_free_org()
        cohorts.opt_in(built.organization, built.owner)
        marker = cohorts.read_marker(built.organization)
        self.assertEqual((marker["version"], marker["cohort"], marker["assignedBy"]), (1, "new_journey", "opt_in"))
        self.assertIsNone(marker["publishAutomatically"])
        self.assertIsNotNone(marker["optedInAt"])

    def test_refuses_when_unavailable(self):
        legacy = make_history_free_org()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            with self.assertRaises(cohorts.FounderJourneyOptInUnavailable):
                cohorts.opt_in(legacy.organization, legacy.owner)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS=legacy.organization.domain):
            with self.assertRaises(cohorts.FounderJourneyOptInUnavailable):
                cohorts.opt_in(legacy.organization, legacy.owner)
        self.assertIsNone(cohorts.read_marker(legacy.organization))
        new = make_new_journey_org()
        with self.assertRaises(cohorts.FounderJourneyOptInUnavailable):
            cohorts.opt_in(new.organization, new.owner)
        launch_only = _org(created_at=timezone.now())
        with self.assertRaises(cohorts.FounderJourneyOptInUnavailable):
            cohorts.opt_in(launch_only, new.owner)


@override_settings(**FLAGS)
class ReasonAndInventoryTests(TestCase):
    def setUp(self):
        self.org = _org(created_at=PRE_LAUNCH)

    def _run(self, workflow, *, status="completed", created_at=None, **extra):
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", workflow=workflow,
            domain=self.org.domain, organization=self.org, status=status, **extra)
        if created_at is not None:
            ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=created_at)
        return run

    def test_history_free_org_has_no_reasons_and_no_inventory(self):
        self.assertEqual(cohorts.legacy_reasons(self.org), [])
        self.assertEqual(cohorts.founder_state_inventory(self.org), [])
        self.assertEqual(cohorts.founder_state_inventory(_org(config=False)), [])

    def test_each_legacy_reason(self):
        cases = {
            "website_connection": lambda: WebsiteConnection.objects.create(
                organization=self.org, github_repo="e/r", state=WebsiteConnection.State.REVOKED),
            "hosted_site": lambda: HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain),
            "setup_run_90d": lambda: self._run("repo_scan", created_at=timezone.now() - timedelta(days=10)),
        }
        for reason, make in cases.items():
            with self.subTest(reason=reason):
                self.org = _org(created_at=PRE_LAUNCH)
                make()
                self.assertIn(reason, cohorts.legacy_reasons(self.org))
        self.org = _org(created_at=PRE_LAUNCH)
        self._run("article_system_setup", created_at=timezone.now() - timedelta(days=91))
        self.assertNotIn("setup_run_90d", cohorts.legacy_reasons(self.org))
        self.assertIn("setup_run", cohorts.founder_state_inventory(self.org))
        for domain in NAMED_LEGACY_ORGS.values():
            with self.subTest(domain=domain):
                self.assertIn("named_org", cohorts.legacy_reasons(_org(domain=domain)))

    def test_published_article_and_established_publisher(self):
        WrittenArticle.objects.create(organization=self.org, title="T", slug="t", category="c", primary_keyword="t")
        reasons = cohorts.legacy_reasons(self.org)
        self.assertNotIn("published_article", reasons)
        self.assertIn("established_publisher", reasons)
        WrittenArticle.objects.filter(organization=self.org).update(published_at=timezone.now())
        self.assertIn("published_article", cohorts.legacy_reasons(self.org))
        charged = _org(created_at=PRE_LAUNCH)
        self.org = charged
        self._run("article_generation", run_request={"roo_points_billing_status": "charged"})
        self.assertEqual(cohorts.legacy_reasons(charged), ["established_publisher"])

    def test_each_inventory_signal(self):
        def saved_repo():
            OrganizationContentConfig.objects.filter(organization=self.org).update(github_repo="e/r")

        def saved_repo_in_article_system():
            OrganizationContentConfig.objects.filter(organization=self.org).update(
                article_system={"scan": {"repository": "e/r"}})

        def channel():
            NotificationChannel.objects.create(organization=self.org, channel_type="email", route_id="a@b.test")

        def operation():
            connection_row = WebsiteConnection.objects.create(organization=self.org, github_repo="e/r")
            WebsiteConnectionOperation.objects.create(connection=connection_row, generation=1,
                idempotency_key=uuid4().hex, action="prepare", state="needs_user")

        cases = [
            ("saved_repo", saved_repo),
            ("saved_repo", saved_repo_in_article_system),
            ("written_article", lambda: WrittenArticle.objects.create(
                organization=self.org, title="T", slug="t", category="c", primary_keyword="t")),
            ("open_article_run", lambda: self._run("article_generation", status="running")),
            ("open_article_run", lambda: self._run("direct_generate", status="awaiting_approval")),
            ("open_article_run", lambda: self._run("article_generation", approval_state="approval_required")),
            ("open_website_operation", operation),
            ("notification_channel", channel),
            ("publish_settings", lambda: OrganizationContentConfig.objects.filter(organization=self.org).update(auto_publish=True)),
            ("publish_settings", lambda: OrganizationContentConfig.objects.filter(organization=self.org).update(requires_review=True)),
            ("setup_run", lambda: self._run("content_factory_scan", created_at=timezone.now() - timedelta(days=400))),
            ("setup_run", lambda: OrganizationContentConfig.objects.filter(organization=self.org).update(
                article_system={"pending_article_system_setup": {"mode": "scaffold"}})),
        ]
        for signal, make in cases:
            with self.subTest(signal=signal, make=make.__name__ if hasattr(make, "__name__") else signal):
                self.org = _org(created_at=PRE_LAUNCH)
                make()
                self.assertIn(signal, cohorts.founder_state_inventory(self.org))
        # A finished, approved article run and a completed operation are not open.
        self.org = _org(created_at=PRE_LAUNCH)
        self._run("article_generation", status="failed")
        connection_row = WebsiteConnection.objects.create(organization=self.org, github_repo="e/r")
        WebsiteConnectionOperation.objects.create(connection=connection_row, generation=1,
            idempotency_key=uuid4().hex, action="prepare", state="completed")
        inventory = cohorts.founder_state_inventory(self.org)
        self.assertNotIn("open_article_run", inventory)
        self.assertNotIn("open_website_operation", inventory)

    def test_runs_matched_by_domain_before_the_org_existed(self):
        ContentFactoryRun.objects.create(run_id="run-domain-only", workflow="repo_scan", domain="late.example.test")
        org = _org(domain="late.example.test", created_at=PRE_LAUNCH)
        ContentFactoryRun.objects.filter(run_id="run-domain-only").update(organization=None)
        self.assertIn("setup_run_90d", cohorts.legacy_reasons(org))

    def test_website_predicates(self):
        for domain, expected in (("example.test", True), ("startup-12.invalid", False), ("", False),
                                 ("Startup-12.INVALID", False)):
            with self.subTest(domain=domain):
                self.assertEqual(cohorts.organization_has_website(Organization(domain=domain)), expected)
        self.assertFalse(cohorts.organization_has_website(None))
        connected = WebsiteConnection.objects.create(organization=self.org, github_repo="e/r",
                                                     state=WebsiteConnection.State.CONNECTED)
        target = WebsiteConnectionTarget.objects.create(connection=connected, target_key="articles", generation=1)
        self.assertFalse(cohorts.has_connected_website(self.org))  # unverified
        target.verified_at = timezone.now()
        target.save()
        self.assertTrue(cohorts.has_connected_website(self.org))
        for state in (WebsiteConnection.State.PAUSED, WebsiteConnection.State.DISCONNECTED, WebsiteConnection.State.REVOKED):
            with self.subTest(state=state):
                WebsiteConnection.objects.filter(pk=connected.pk).update(state=state)
                self.assertFalse(cohorts.has_connected_website(self.org))


class PrivateStrategyKeyTests(TestCase):
    KEYS = ("founder_journey", "founder_first_profile", "founder_posted_articles")

    def test_cf_strategy_sync_keeps_and_workers_never_see_founder_keys(self):
        existing = {"pillars": [{"slug": "old"}], **{key: {"kept": key} for key in self.KEYS}}
        incoming = {"pillars": [{"slug": "new"}], "founder_journey": {"cohort": "new_journey", "version": 1}}
        merged = editorial_catalog.merge_strategy(existing, incoming)
        self.assertEqual(merged["pillars"], [{"slug": "new"}])
        for key in self.KEYS:
            self.assertEqual(merged[key], {"kept": key})
        self.assertNotIn("founder_first_profile", editorial_catalog.merge_strategy({}, {"founder_first_profile": {"x": 1}}))
        public = editorial_catalog.public_strategy(merged)
        self.assertEqual(set(public), {"pillars"})

    def test_island_refresh_eligibility_ignores_founder_keys(self):
        config = OrganizationContentConfig(pillar_strategy={key: {"x": 1} for key in self.KEYS})
        self.assertFalse(island_refresh_scheduler._has_pillar_strategy(config))
        config.pillar_strategy = {**config.pillar_strategy, "other": 1}
        self.assertTrue(island_refresh_scheduler._has_pillar_strategy(config))


class SettingsTests(TestCase):
    def test_every_p1_setting_exists_with_its_documented_default(self):
        for name, default in P1_SETTING_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(settings, name), name)
                self.assertEqual(getattr(settings, name), default)
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["community_chat_invite_resolve"], "30/minute")
        self.assertEqual(set(ALL_P1_FLAGS_ON) - set(P1_SETTING_DEFAULTS), set())

    def test_every_flag_defaults_off(self):
        self.assertFalse(cohorts.v2_enabled())
        self.assertEqual(cohorts.new_startup_cohort(), "legacy")
        org = _org(created_at=timezone.now())
        self.assertFalse(cohorts.is_new_journey(org))
        self.assertIsNone(cohorts.public_payload(org, user=None))


class NamedLegacyOrgProofTests(TestCase):
    """Legacy proof: every named org is legacy with every P1 flag on, with or without its marker."""

    def _prove(self, built):
        org = built.organization
        self.assertLess(org.created_at, LAUNCHED_AT)
        self.assertEqual(cohorts.cohort(org), "legacy")
        self.assertFalse(cohorts.is_new_journey(org))
        self.assertTrue(cohorts.legacy_reasons(org))
        self.assertTrue(cohorts.founder_state_inventory(org))
        self.assertEqual(cohorts.public_payload(org, user=built.owner)["cohort"], "legacy")
        self.assertEqual(cohorts.account_payload(built.owner)["companies"], {})
        self.assertTrue(cohorts.user_has_legacy_org(built.owner))
        self.assertIsNone(cohorts.update_marker(org, owner_click_required=True))
        self.assertEqual(cohorts.assign_on_create(org, company_id=built.company.pk), "legacy")

    @override_settings(**FLAGS)
    def test_with_report_apply_marker(self):
        for name in NAMED_LEGACY_ORGS:
            with self.subTest(name=name):
                built = make_named_legacy_org(name)
                self.assertEqual(cohorts.read_marker(built.organization)["cohort"], "legacy")
                self.assertIn("named_org", cohorts.read_marker(built.organization)["reasons"])
                self._prove(built)

    @override_settings(**FLAGS)
    def test_without_marker(self):
        for name in NAMED_LEGACY_ORGS:
            with self.subTest(name=name):
                built = make_named_legacy_org(name, marker=False)
                self.assertIsNone(cohorts.read_marker(built.organization))
                self._prove(built)
                self.assertIsNone(cohorts.read_marker(built.organization))

    @override_settings(**FLAGS)
    def test_production_shapes(self):
        tala = make_named_legacy_org("talathrive")
        self.assertTrue(cohorts.has_connected_website(tala.organization))
        self.assertTrue(all(run.run_request["delivery_mode"] == "review_draft" for run in tala.runs))
        paw = make_named_legacy_org("pawshare")
        self.assertEqual(paw.hosted_site.state, HostedArticleSite.State.ACTIVE)
        la_vie = make_named_legacy_org("la_vie")
        self.assertEqual(la_vie.target.contract["mode"], "adopted_registry")
        self.assertIsNotNone(la_vie.target.verified_at)
        mlai = make_named_legacy_org("mlai")
        self.assertIn("published_article", cohorts.legacy_reasons(mlai.organization))
        from .billing import FREE_CONTENT_FACTORY_DOMAINS
        self.assertIn(mlai.organization.domain, FREE_CONTENT_FACTORY_DOMAINS)


@override_settings(**FLAGS)
class LockOrderTests(TestCase):
    """Every marker writer locks the Organization row before the config row.

    The Content Factory strategy sync and catalogue edits lock only the
    Organization row and then rewrite ``pillar_strategy``; a writer that
    locked only the config row could interleave with them and lose a key.
    """

    def _locks(self, call):
        calls = []
        org_lock, config_lock = Organization.objects.select_for_update, OrganizationContentConfig.objects.select_for_update

        def record(name, original):
            def wrapper(*args, **kwargs):
                calls.append(name)
                return original(*args, **kwargs)
            return wrapper

        with mock.patch.object(Organization.objects, "select_for_update", side_effect=record("organization", org_lock)), \
                mock.patch.object(OrganizationContentConfig.objects, "select_for_update",
                                  side_effect=record("config", config_lock)):
            call()
        return calls

    def assertOrganizationLockedFirst(self, calls):
        self.assertIn("config", calls)
        self.assertEqual(calls[0], "organization")
        for index, name in enumerate(calls):
            if name == "config":
                self.assertIn("organization", calls[:index])

    def test_update_marker(self):
        built = make_new_journey_org()
        calls = self._locks(lambda: cohorts.update_marker(built.organization, owner_click_required=True))
        self.assertOrganizationLockedFirst(calls)
        self.assertTrue(cohorts.read_marker(built.organization)["ownerClickRequired"])

    def test_assign_on_create(self):
        built = make_new_journey_org(marker=False)
        calls = self._locks(lambda: cohorts.assign_on_create(built.organization, company_id=built.company.pk))
        self.assertOrganizationLockedFirst(calls)
        self.assertEqual(cohorts.read_marker(built.organization)["assignedBy"], "create")

    def test_opt_in(self):
        built = make_history_free_org()
        calls = self._locks(lambda: cohorts.opt_in(built.organization, built.owner))
        self.assertOrganizationLockedFirst(calls)

    def test_apply(self):
        from content_factory.management.commands.founder_journey_cohort import apply_markers
        built = make_history_free_org()
        calls = self._locks(lambda: apply_markers([built.organization]))
        self.assertOrganizationLockedFirst(calls)
        self.assertEqual(cohorts.read_marker(built.organization)["cohort"], "new_journey")


class ConnectedWebsiteGenerationTests(TestCase):
    def test_only_the_current_generation_counts(self):
        org = _org()
        connection_row = WebsiteConnection.objects.create(organization=org, github_repo="e/r", generation=2,
                                                          state=WebsiteConnection.State.CONNECTED)
        WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=1,
                                               verified_at=timezone.now() - timedelta(days=9))
        current = WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=2)
        # A reconnect left the old generation's verified target behind.
        self.assertFalse(cohorts.has_connected_website(org))
        current.verified_at = timezone.now()
        current.save()
        self.assertTrue(cohorts.has_connected_website(org))

    @override_settings(**FLAGS)
    def test_opt_in_after_a_reconnect_does_not_publish_automatically(self):
        built = make_history_free_org()
        connection_row = WebsiteConnection.objects.create(organization=built.organization, github_repo="e/r",
                                                          generation=3, state=WebsiteConnection.State.CONNECTED)
        WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=2,
                                               verified_at=timezone.now())
        WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=3)
        cohorts.opt_in(built.organization, built.owner)
        self.assertIsNone(cohorts.read_marker(built.organization)["publishAutomatically"])


class WorkerStrategyTests(TestCase):
    """The founder-journey keys never reach a Content Factory scan, and a marker alone is no strategy."""

    def test_a_marker_alone_does_not_count_as_a_saved_strategy(self):
        from integrations.api_views_content_factory_app import scan_existing_artifacts
        built = make_history_free_org()
        _set_marker(built.config, _marker(cohorts.LEGACY))
        built.config.company_context = "We make things."
        built.config.save()
        artifacts = scan_existing_artifacts(built.config)
        self.assertNotIn("pillar_strategy", artifacts)
        self.assertEqual(artifacts["company_context"], "We make things.")

    def test_other_strategy_keys_reach_the_scan_unchanged(self):
        from integrations.api_views_content_factory_app import scan_existing_artifacts
        org = _org()
        config = _config(org)
        legacy_strategy = {"pillars": [{"slug": "a"}], "article_voice": {"tone": "warm"}, "ops_alerts": {"x": 1}}
        config.pillar_strategy = legacy_strategy
        config.save()
        # Without founder keys the payload is exactly what it was before P1.
        self.assertEqual(scan_existing_artifacts(config)["pillar_strategy"], legacy_strategy)
        _set_marker(config, _marker(cohorts.LEGACY))
        config.pillar_strategy = {**config.pillar_strategy, "founder_first_profile": {"y": 1},
                                  "founder_posted_articles": {"r": {}}}
        config.save()
        self.assertEqual(scan_existing_artifacts(config)["pillar_strategy"], legacy_strategy)
        self.assertEqual(scan_existing_artifacts(None), {})

    def test_without_private_keys(self):
        self.assertEqual(cohorts.without_private_keys({"pillars": [], "founder_journey": {}}), {"pillars": []})
        self.assertIsNone(cohorts.without_private_keys(None))
        self.assertEqual(cohorts.without_private_keys([1]), [1])
