"""E11: the read-only re-engagement report on a fixture cohort (plan section 6.7, decision D14).

Acceptance: the report is deterministic on a fixture cohort, and every deep
link lands in a setup page the organisation's owner can open, legacy cohort
included. The command never sends: a send refuses without the owner's
go-ahead and is still a dry run with it.
"""
import json
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from crm.models import Contact
from founder_tools.models import (
    StartupMembership,
    VibeRaisingCompany,
    VibeRaisingProfile,
)
from organizations.models import Organization
from system_email.models import SystemEmailSuppression

from . import founder_journey_cohort as cohorts
from . import founder_reengagement as reengagement
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection

NOW = datetime(2026, 11, 19, 9, tzinfo=dt_timezone.utc)
WORKSHOP = datetime(2026, 10, 15, 3, tzinfo=dt_timezone.utc)
CHAT = "https://chat.example.test"


@override_settings(COMMUNITY_CHAT_FRONTEND_URL=CHAT, FOUNDER_REENGAGEMENT_SEND_APPROVED=False)
class ReengagementReportTests(TestCase):
    def user(self, email, first_name=""):
        return get_user_model().objects.create_user(email=email, first_name=first_name)

    def startup(self, owner, domain, *, name=None, created=None):
        org = Organization.objects.create(domain=domain, name=name or domain)
        if created:
            Organization.objects.filter(pk=org.pk).update(created_at=created)
            org.refresh_from_db()
        profile = None
        if owner is not None:
            profile, _ = VibeRaisingProfile.objects.get_or_create(user=owner, defaults={"role": "founder"})
        company = VibeRaisingCompany.objects.create(profile=profile, organization=org, domain=domain,
                                                    name=name or domain)
        return org, company

    def setUp(self):
        self.ada = self.user("ada@example.test", "Ada")
        self.bo = self.user("bo@example.test", "Bo")
        self.cy = self.user("cy@example.test")
        self.di = self.user("di@example.test", "Di")
        self.ed = self.user("ed@example.test", "Ed")
        self.fay = self.user("fay@example.test", "Fay")
        self.gus = self.user("gus@example.test", "Gus")
        self.hal = self.user("hal@example.test", "Hal")
        self.ivy = self.user("ivy@example.test", "Ivy")
        self.jo = self.user("jo@example.test", "Jo")
        self.kai = self.user("kai@example.test", "Kai")
        self.lu = self.user("lu@example.test", "Lu")
        self.max = self.user("max@example.test", "Max")
        self.orgs = {}
        self.orgs["real"], _ = self.startup(self.ada, "acme-example.com.au", name="Acme", created=WORKSHOP)
        self.orgs["typo"], _ = self.startup(self.bo, "reidandcoconsuliting.com.au", name="Reid and Co")
        self.orgs["lovable"], _ = self.startup(self.cy, "lazarus.lovable.app", name="Lazarus")
        self.orgs["framer"], _ = self.startup(self.di, "soco.framer.website", name="SoCo")
        self.orgs["railway"], _ = self.startup(self.ed, "auditmind-prod.up.railway.app", name="AuditMind")
        placeholder_org = Organization.objects.create(domain="startup-placeholder.invalid", name="No Site")
        profile, _ = VibeRaisingProfile.objects.get_or_create(user=self.fay, defaults={"role": "founder"})
        VibeRaisingCompany.objects.create(profile=profile, organization=placeholder_org, domain="", name="No Site")
        self.orgs["nosite"] = placeholder_org
        self.orgs["unisub"], _ = self.startup(self.gus, "startup.unimelb.edu.au", name="Uni Startup")
        Organization.objects.create(domain="bigco.com", name="BigCo")  # another owner's domain, no startup
        self.orgs["othersub"], _ = self.startup(self.hal, "shop.bigco.com", name="Shop")
        self.orgs["test"], _ = self.startup(self.ivy, "test-org.example", name="Test")
        self.orgs["optout"], _ = self.startup(self.jo, "optout-example.com", name="Opted")
        Contact.objects.create(email="jo@example.test", source="import", email_status="unsubscribed")
        self.orgs["dup"], _ = self.startup(self.ada, "acme-two-example.com", name="Acme Two")
        self.orgs["ownerless"], _ = self.startup(None, "ownerless-example.com", name="Ownerless")
        # Legacy cohort, shared workspace: the owner is the first active member.
        shared_org = Organization.objects.create(domain="shared-example.com", name="Shared")
        shared = VibeRaisingCompany.objects.create(profile=None, organization=shared_org, domain="shared-example.com",
                                                   name="Shared", membership_policy=VibeRaisingCompany.SHARED_ACCESS)
        StartupMembership.objects.create(company=shared, user=self.kai, joined_at=NOW - timedelta(days=40),
                                         source="test")
        StartupMembership.objects.create(company=shared, user=self.lu, joined_at=NOW - timedelta(days=20),
                                         source="test")
        self.orgs["shared"] = shared_org
        # New-journey cohort, authorised GitHub but never chose a repository.
        self.orgs["norepo"], _ = self.startup(self.max, "norepo-example.com", name="No Repo")
        OrganizationContentConfig.objects.create(
            organization=self.orgs["norepo"],
            pillar_strategy={cohorts.MARKER_KEY: cohorts._base_marker(cohorts.NEW_JOURNEY, "create", [])})
        WebsiteConnection.objects.create(organization=self.orgs["norepo"], github_repo="")
        # Connected or configured startups are not in the cohort.
        connected, _ = self.startup(self.user("zed@example.test"), "connected-example.com")
        WebsiteConnection.objects.create(organization=connected, github_repo="zed/site")
        configured, _ = self.startup(self.user("yu@example.test"), "configured-example.com")
        OrganizationContentConfig.objects.create(organization=configured, github_repo="yu/site")
        self.excluded = {connected.pk, configured.pk}
        self.overrides = {str(self.orgs["typo"].pk): {"bucket": "domain_typo",
                                                      "suggestion": "reidandcoconsulting.com.au"},
                          str(self.orgs["test"].pk): {"bucket": "test"}}

    def report(self, **kwargs):
        kwargs.setdefault("overrides", self.overrides)
        return reengagement.build_report(now=NOW, workshop_days=["2026-10-15"], **kwargs)

    def rows(self, report):
        return {row["organizationId"]: row for row in report["organizations"]}

    def test_buckets_the_fixture_cohort(self):
        rows = self.rows(self.report())
        self.assertFalse(self.excluded & set(rows))
        expected = {"real": "connect_github", "typo": "domain_typo", "lovable": "ai_builder",
                    "framer": "no_export_builder", "railway": "platform_url", "nosite": "no_domain",
                    "unisub": "subdomain_other_owner", "othersub": "subdomain_other_owner", "test": "test",
                    "optout": "connect_github", "dup": "connect_github", "ownerless": "no_owner",
                    "shared": "connect_github", "norepo": "connect_github"}
        self.assertEqual({key: rows[org.pk]["bucket"] for key, org in self.orgs.items()}, expected)
        self.assertEqual(rows[self.orgs["nosite"].pk]["setup"]["ask"], "add_website")
        self.assertEqual(rows[self.orgs["norepo"].pk]["setup"]["ask"], "choose_repository")
        self.assertEqual(rows[self.orgs["real"].pk]["setup"]["current"], "github")
        self.assertIn(rows[self.orgs["real"].pk]["setup"]["source"], {"local", "e6"})
        self.assertEqual(rows[self.orgs["norepo"].pk]["cohort"], "new_journey")
        self.assertEqual(rows[self.orgs["real"].pk]["cohort"], "legacy")

    def test_report_and_drafts_are_deterministic(self):
        first = reengagement.dumps(self.report())
        second = reengagement.dumps(self.report())
        self.assertEqual(first, second)
        self.assertEqual(reengagement.dumps(reengagement.render_drafts(self.report())),
                         reengagement.dumps(reengagement.render_drafts(self.report())))
        ids = [row["organizationId"] for row in json.loads(first)["organizations"]]
        self.assertEqual(ids, sorted(ids))

    def test_every_deep_link_lands_in_setup_the_owner_can_open(self):
        report = self.report()
        linked = [row for row in report["organizations"] if row["link"]]
        self.assertTrue(linked)
        for row in report["organizations"]:
            org = Organization.objects.get(pk=row["organizationId"])
            if row["bucket"] == "no_owner":
                self.assertIsNone(row["link"])
                continue
            owner = get_user_model().objects.get(pk=row["ownerUserId"])
            parts = urlsplit(row["link"])
            query = parse_qs(parts.query)
            self.assertEqual(f"{parts.scheme}://{parts.netloc}", CHAT)
            # Never the cohort-gated /my-startup/start, which sends legacy accounts to the old form.
            self.assertEqual(parts.path, "/my-startup/onboarding")
            self.assertEqual(query["company_id"], [row["companyId"]])
            self.assertTrue(row["linkVisibleToOwner"], row)
            self.assertTrue(reengagement.link_visible_to(row["link"], owner, org))
            self.assertFalse(reengagement.link_visible_to(row["link"], self.user(f"x{org.pk}@example.test"), org))
        rows = self.rows(report)
        self.assertEqual(parse_qs(urlsplit(rows[self.orgs["nosite"].pk]["link"]).query)["step"], ["startup"])
        self.assertEqual(parse_qs(urlsplit(rows[self.orgs["typo"].pk]["link"]).query)["step"], ["startup"])
        self.assertEqual(parse_qs(urlsplit(rows[self.orgs["real"].pk]["link"]).query)["step"], ["repository"])
        self.assertEqual(rows[self.orgs["shared"].pk]["ownerEmail"], "kai@example.test")

    def test_link_check_refuses_other_pages_and_other_orgs(self):
        rows = self.rows(self.report())
        row = rows[self.orgs["real"].pk]
        start = row["link"].replace("/my-startup/onboarding", "/my-startup/start")
        self.assertFalse(reengagement.link_visible_to(start, self.ada, self.orgs["real"]))
        foreign = row["link"].replace(CHAT, "https://evil.example.test")
        self.assertFalse(reengagement.link_visible_to(foreign, self.ada, self.orgs["real"]))
        self.assertFalse(reengagement.link_visible_to(row["link"], self.ada, self.orgs["dup"]))
        with self.assertRaises(ValueError):
            reengagement.setup_link(row["companyId"], "articles")

    def test_opt_out_hold_and_one_email_per_person(self):
        rows = self.rows(self.report())
        self.assertEqual(rows[self.orgs["optout"].pk]["contactBlock"], "opted_out")
        self.assertEqual(rows[self.orgs["optout"].pk]["contact"]["stage"], "skip")
        self.assertEqual(rows[self.orgs["framer"].pk]["contact"]["stage"], "skip")
        self.assertTrue(rows[self.orgs["framer"].pk]["hold"])
        self.assertEqual(rows[self.orgs["test"].pk]["contact"]["stage"], "skip")
        self.assertEqual(rows[self.orgs["dup"].pk]["sendWith"], self.orgs["real"].pk)
        drafts = reengagement.render_drafts(self.report())
        recipients = [draft["to"] for draft in drafts["drafts"]]
        self.assertEqual(len(recipients), len(set(recipients)))
        self.assertNotIn("jo@example.test", recipients)
        self.assertNotIn("di@example.test", recipients)
        self.assertNotIn("ivy@example.test", recipients)
        acme = next(draft for draft in drafts["drafts"] if draft["to"] == "ada@example.test")
        self.assertEqual(acme["alsoCovers"], [self.orgs["dup"].pk])
        self.assertIn("short call", acme["body"])  # workshop sign-up gets the session offer
        self.assertIn('reply "stop"', acme["body"])
        self.assertIn(rows[self.orgs["real"].pk]["link"], acme["body"])
        typo = next(draft for draft in drafts["drafts"] if draft["to"] == "bo@example.test")
        self.assertIn("reidandcoconsulting.com.au", typo["subject"])
        lovable = next(draft for draft in drafts["drafts"] if draft["to"] == "cy@example.test")
        self.assertIn("Lovable", lovable["body"])
        self.assertIn("Hi there", lovable["body"])
        self.assertIn("review", lovable)

    def test_bounced_and_withdrawn_contacts_are_skipped(self):
        Contact.objects.create(email="ed@example.test", source="import", email_status="bounced")
        Contact.objects.create(email="gus@example.test", source="import", marketing_consent="withdrawn")
        rows = self.rows(self.report())
        self.assertEqual(rows[self.orgs["railway"].pk]["contactBlock"], "undeliverable")
        self.assertEqual(rows[self.orgs["unisub"].pk]["contactBlock"], "opted_out")

    def test_suppression_list_blocks_in_any_case(self):
        SystemEmailSuppression.objects.create(email="Bo@Example.test", reason="complaint")
        SystemEmailSuppression.objects.create(email="ED@example.test", reason="hard_bounce")
        SystemEmailSuppression.objects.create(email="cy@example.test", reason="complaint")
        SystemEmailSuppression.objects.create(email="hal@example.test", reason="hard_bounce", cleared_at=NOW)
        rows = self.rows(self.report())
        self.assertEqual(rows[self.orgs["typo"].pk]["contactBlock"], "opted_out")
        self.assertEqual(rows[self.orgs["railway"].pk]["contactBlock"], "undeliverable")
        self.assertEqual(rows[self.orgs["lovable"].pk]["contactBlock"], "opted_out")
        self.assertIsNone(rows[self.orgs["othersub"].pk]["contactBlock"])  # a cleared suppression no longer blocks
        recipients = {draft["to"] for draft in reengagement.render_drafts(self.report())["drafts"]}
        self.assertFalse(recipients & {"bo@example.test", "ed@example.test", "cy@example.test"})

    def test_unreadable_ledger_never_resends(self):
        ledger = {str(self.orgs["real"].pk): {"first": (NOW - timedelta(days=9)).isoformat(), "follow_up": "yes"},
                  str(self.orgs["railway"].pk): {"first": "last tuesday"}}
        rows = self.rows(self.report(ledger=ledger))
        self.assertEqual(rows[self.orgs["real"].pk]["contact"]["stage"], "stop")
        self.assertEqual(rows[self.orgs["railway"].pk]["contact"]["stage"], "stop")

    def test_cap_is_per_person_across_startups_and_email_keys(self):
        # The first email went out recorded under the owner's second startup.
        ledger = {str(self.orgs["dup"].pk): {"first": (NOW - timedelta(days=2)).isoformat()},
                  "Kai@Example.test": {"first": (NOW - timedelta(days=10)).isoformat()},
                  "999999": {"first": (NOW - timedelta(days=10)).isoformat()}}
        report = self.report(ledger=ledger)
        rows = self.rows(report)
        self.assertEqual(rows[self.orgs["real"].pk]["contact"]["stage"], "wait")
        self.assertEqual(rows[self.orgs["shared"].pk]["contact"]["stage"], "follow_up")
        self.assertEqual(report["unmatchedLedgerKeys"], ["999999"])
        drafts = reengagement.render_drafts(report)["drafts"]
        self.assertNotIn("ada@example.test", [draft["to"] for draft in drafts])
        self.assertEqual([d["stage"] for d in drafts if d["to"] == "kai@example.test"], ["follow_up"])

    def test_bad_overrides_are_refused(self):
        with self.assertRaisesMessage(ValueError, "unknown bucket"):
            self.report(overrides={str(self.orgs["test"].pk): {"bucket": "tset"}})
        with self.assertRaisesMessage(ValueError, "without a suggestion"):
            self.report(overrides={str(self.orgs["typo"].pk): {"bucket": "domain_typo"}})

    def test_one_follow_up_a_week_later_then_stop(self):
        ledger = {str(self.orgs["real"].pk): {"first": (NOW - timedelta(days=8)).isoformat()},
                  str(self.orgs["railway"].pk): {"first": (NOW - timedelta(days=2)).isoformat()},
                  str(self.orgs["shared"].pk): {"first": (NOW - timedelta(days=20)).isoformat(),
                                                "follow_up": (NOW - timedelta(days=12)).isoformat()}}
        report = self.report(ledger=ledger)
        rows = self.rows(report)
        self.assertEqual(rows[self.orgs["real"].pk]["contact"]["stage"], "follow_up")
        self.assertEqual(rows[self.orgs["railway"].pk]["contact"]["stage"], "wait")
        self.assertEqual(rows[self.orgs["shared"].pk]["contact"]["stage"], "stop")
        drafts = {draft["organizationId"]: draft for draft in reengagement.render_drafts(report)["drafts"]}
        self.assertEqual(drafts[self.orgs["real"].pk]["stage"], "follow_up")
        self.assertIn("last time", drafts[self.orgs["real"].pk]["body"])
        self.assertNotIn("short call", drafts[self.orgs["real"].pk]["body"])
        self.assertNotIn(self.orgs["railway"].pk, drafts)
        self.assertNotIn(self.orgs["shared"].pk, drafts)

    def test_next_step_is_swappable(self):
        calls = []

        def fake(org):
            calls.append(org.pk)
            return {"current": "github", "ask": "add_website", "source": "e6"}

        rows = self.rows(self.report(setup_next_fn=fake, organization_ids=[self.orgs["real"].pk]))
        self.assertEqual(calls, [self.orgs["real"].pk])
        self.assertEqual(rows[self.orgs["real"].pk]["bucket"], "no_domain")
        self.assertEqual(rows[self.orgs["real"].pk]["setup"]["source"], "e6")

    def test_unusable_e6_state_falls_back_to_local_facts(self):
        import sys
        import types
        module = types.ModuleType("content_factory.founder_setup_state")
        module.setup_state = lambda org: ["not", "a", "dict"]
        previous = sys.modules.get("content_factory.founder_setup_state")
        sys.modules["content_factory.founder_setup_state"] = module
        try:
            step = reengagement.setup_next(self.orgs["norepo"])
        finally:
            if previous is None:
                sys.modules.pop("content_factory.founder_setup_state", None)
            else:
                sys.modules["content_factory.founder_setup_state"] = previous
        self.assertEqual(step, {"current": "github", "ask": "choose_repository", "source": "local"})

    def test_classify_host_offline(self):
        self.assertEqual(reengagement.classify_host("https://x.lovable.app/").host_class, "ai_builder")
        self.assertEqual(reengagement.classify_host("y.chatgpt.site").host_class, "no_export")
        self.assertTrue(reengagement.classify_host("a.workers.dev").deployment_address)
        self.assertTrue(reengagement.classify_host("a.herokuapp.com").deployment_address)
        self.assertFalse(reengagement.classify_host("acme.com.au").deployment_address)
        self.assertEqual(reengagement.registrable_domain("platform.tailoredagents.ai"), "tailoredagents.ai")
        self.assertEqual(reengagement.registrable_domain("a.b.example.com.au"), "example.com.au")


@override_settings(COMMUNITY_CHAT_FRONTEND_URL=CHAT, FOUNDER_REENGAGEMENT_SEND_APPROVED=False,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ReengagementCommandTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(email="ada@example.test", first_name="Ada")
        profile = VibeRaisingProfile.objects.create(user=owner, role="founder")
        org = Organization.objects.create(domain="acme-example.com", name="Acme")
        VibeRaisingCompany.objects.create(profile=profile, organization=org, domain="acme-example.com", name="Acme")
        self.org = org

    def run_command(self, *args):
        out = StringIO()
        call_command("founder_reengagement_report", "--now", NOW.isoformat(), *args, stdout=out)
        return out.getvalue()

    def test_json_output_is_deterministic_and_writes_files(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path, drafts_path, text_path = (Path(folder) / name for name in
                                                   ("report.json", "drafts.json", "drafts.txt"))
            first = self.run_command("--json", "--out", str(report_path), "--drafts-out", str(drafts_path))
            second = self.run_command("--json", "--drafts-out", str(text_path))
            self.assertEqual(first, second)
            self.assertEqual(json.loads(report_path.read_text()), json.loads(first))
            drafts = json.loads(drafts_path.read_text())
            self.assertEqual(drafts["cap"], {"emails": 1, "followUps": 1, "followUpAfterDays": 7, "then": "stop"})
            self.assertEqual([draft["to"] for draft in drafts["drafts"]], ["ada@example.test"])
            self.assertIn("Subject: One step left for Acme", text_path.read_text())
        self.assertEqual(mail.outbox, [])

    def test_table_says_nothing_is_sent(self):
        output = self.run_command()
        self.assertIn("connect_github", output)
        self.assertIn("Nothing is sent by this command.", output)
        self.assertEqual(mail.outbox, [])

    def test_send_refuses_without_go_ahead(self):
        with self.assertRaisesMessage(CommandError, "FOUNDER_REENGAGEMENT_SEND_APPROVED"):
            self.run_command("--send")
        with self.assertRaises(CommandError):
            self.run_command("--no-dry-run")
        self.assertEqual(mail.outbox, [])

    @override_settings(FOUNDER_REENGAGEMENT_SEND_APPROVED=True)
    def test_send_with_go_ahead_is_still_a_dry_run(self):
        output = self.run_command("--send")
        self.assertIn("DRY RUN: nothing was sent", output)
        self.assertIn("ada@example.test", output)
        with self.assertRaisesMessage(CommandError, "no sender"):
            self.run_command("--send", "--no-dry-run")
        self.assertEqual(mail.outbox, [])

    def test_bad_inputs(self):
        with self.assertRaises(CommandError):
            self.run_command("--overrides", "/nonexistent/overrides.json")
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            handle.write("[]")
            handle.flush()
            with self.assertRaises(CommandError):
                self.run_command("--sent-ledger", handle.name)
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            handle.write(json.dumps({str(self.org.pk): {"bucket": "tset"}}))
            handle.flush()
            with self.assertRaisesMessage(CommandError, "unknown bucket"):
                self.run_command("--overrides", handle.name)
