"""founder_funnel_report on synthetic rows: the audit's funnel definitions (P0.1, IF-16)."""
import json
from datetime import datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection
from django.test import TestCase

from community_chat.onboarding_models import CommunityMemberProfile
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .management.commands import founder_funnel_report

IN_WINDOW = datetime(2026, 10, 10, 9, tzinfo=dt_timezone.utc)
BEFORE = datetime(2026, 9, 28, 23, tzinfo=dt_timezone.utc)


class FounderFunnelReportTests(TestCase):
    def joined(self, email, *, at=IN_WINDOW, staff=False, submitted=False):
        user = get_user_model().objects.create_user(email=email, is_staff=staff)
        get_user_model().objects.filter(pk=user.pk).update(date_joined=at)
        if submitted:
            CommunityMemberProfile.objects.update_or_create(user=user, defaults={"submitted_at": at, "status": "approved"})
        return user

    def startup(self, user, domain):
        profile, _ = VibeRaisingProfile.objects.get_or_create(user=user, defaults={"role": "founder"})
        org = Organization.objects.create(domain=domain, name=domain)
        VibeRaisingCompany.objects.create(profile=profile, organization=org, domain=domain, name=domain)
        return org

    def make_run(self, org, workflow, status, at=IN_WINDOW):
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, status=status,
                                               domain=org.domain if org else "", organization=org)
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=at)

    def setUp(self):
        drafter = self.joined("drafter@example.test", submitted=True)
        stuck = self.joined("stuck@example.test", submitted=True)
        self.joined("browser@example.test")
        self.joined("staff@example.test", staff=True, submitted=True)
        self.joined("early@example.test", at=BEFORE, submitted=True)
        drafted = self.startup(drafter, "drafted.example.test")
        self.startup(drafter, "second.example.test")
        stuck_org = self.startup(stuck, "stuck.example.test")
        self.make_run(drafted, "confirmed_topic", "awaiting_approval")
        self.make_run(stuck_org, "confirmed_topic", "cancelled")
        self.make_run(stuck_org, "startup_autofill", "completed")
        self.make_run(stuck_org, "island_refresh", "failed")
        self.make_run(stuck_org, "island_refresh", "denied")
        self.make_run(drafted, "island_refresh", "denied")
        self.make_run(drafted, "island_refresh", "completed")
        self.make_run(drafted, "island_refresh", "failed", at=BEFORE)

    def report(self, *args):
        out = StringIO()
        call_command("founder_funnel_report", "--since", "2026-09-29", "--until", "2026-10-29", "--json", *args, stdout=out)
        return json.loads(out.getvalue())

    def test_funnel_follows_the_audit_definitions(self):
        report = self.report()
        self.assertEqual(report["funnel"], {"users": 3, "member_submitted": 2, "startups": 2, "articles": 1})
        self.assertEqual(report["startup_companies_created"], 3)
        self.assertEqual(report["island_refresh_platform_wide"], {"failed": 1, "denied": 2, "failed_or_denied": 3})

    def test_cancelled_or_unfinished_article_runs_are_not_articles(self):
        ContentFactoryRun.objects.filter(workflow="confirmed_topic", status="awaiting_approval").update(status="failed")
        self.assertEqual(self.report()["funnel"]["articles"], 0)

    def test_until_date_is_inclusive_and_datetimes_are_exact(self):
        out = StringIO()
        call_command("founder_funnel_report", "--since", "2026-10-10T09:00:00+00:00", "--until", "2026-10-10",
                     "--json", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["funnel"]["users"], 3)
        out = StringIO()
        call_command("founder_funnel_report", "--since", "2026-10-10T09:00:01+00:00", "--json", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["funnel"]["users"], 0)

    def test_table_output_names_each_step(self):
        out = StringIO()
        call_command("founder_funnel_report", "--since", "2026-09-29", "--until", "2026-10-29", stdout=out)
        text = out.getvalue()
        for label in ("Signed up", "Member questions submitted", "Created a startup", "Reached an article", "island_refresh"):
            self.assertIn(label, text)

    def test_the_report_never_writes(self):
        real = founder_funnel_report.founder_funnel

        def writes_then_reports(**kwargs):
            Organization.objects.create(domain="written-by-report.example.test", name="must roll back")
            return real(**kwargs)

        with patch.object(founder_funnel_report, "founder_funnel", side_effect=writes_then_reports):
            if connection.vendor == "postgresql":
                # Production's path: the READ ONLY transaction refuses the write outright.
                with self.assertRaises(DatabaseError):
                    self.report()
            else:
                # SQLite has no READ ONLY transactions; the forced rollback discards the write.
                self.report()
        self.assertFalse(Organization.objects.filter(domain="written-by-report.example.test").exists())

    def test_bad_window_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("founder_funnel_report", "--since", "29/09/2026", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("founder_funnel_report", "--since", "2026-10-29", "--until", "2026-09-29", stdout=StringIO())
