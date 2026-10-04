"""Database integration for the read-only operations report."""

from datetime import timedelta
from io import StringIO
import json

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from .website_health import website_connection_health
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteTemplateRevision


class WebsiteHealthTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic canary", domain="health.example.test")
        self.connection = WebsiteConnection.objects.create(organization=self.org, github_repo="fixture/site", repository_id=31,
            state="disconnected", capabilities={"publishingReady": True})
        OrganizationContentConfig.objects.create(organization=self.org, github_repo="fixture/site", website_connection=self.connection, articles_scaffolded=True)

    def test_disconnected_historical_ready_is_not_canonical_ready(self):
        report = website_connection_health(domain=self.org.domain)
        self.assertEqual(report["shadowReadiness"], {"legacyScaffolded": 1, "canonicalVerified": 0, "grantsAuthority": False})
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.state, "disconnected")

    def test_monitor_exits_nonzero_on_failed_business_outcomes_and_overdue_cleanup(self):
        operation = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=1, idempotency_key="health-check", action="disconnect", state="pending")
        WebsiteConnectionOperation.objects.filter(pk=operation.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        ContentFactoryRun.objects.create(run_id="health-scan", organization=self.org, domain=self.org.domain, workflow="repo_scan", status="failed", result={"success": False, "error_code": "TEMPLATE_VALIDATION_FAILED", "secret": "do-not-output"})
        WebsiteTemplateRevision.objects.create(connection=self.connection, generation=1, purpose="article_template", digest="a" * 64, provenance="legacy_saved", status="quarantined", body="do-not-output", validation={})
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("report_website_connections", domain=self.org.domain, check=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual({item["code"] for item in report["alerts"]}, {"repository_scan_failed", "website_reconciliation_overdue"})
        self.assertEqual(report["operations"]["overdue"], 1)
        self.assertEqual(report["quarantinedTemplates"], 1)
        self.assertNotIn("do-not-output", output.getvalue())
        self.assertEqual(WebsiteConnectionOperation.objects.get(pk=operation.pk).attempts, 0)

    def test_domain_filter_excludes_other_company_failures(self):
        ContentFactoryRun.objects.create(run_id="other-scan", domain="other.example.test", workflow="repo_scan", status="failed")
        report = website_connection_health(domain=self.org.domain)
        self.assertEqual(report["scans"]["statuses"], {})
        self.assertEqual(report["alerts"], [])
