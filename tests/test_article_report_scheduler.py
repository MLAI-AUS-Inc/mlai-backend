from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    ArticleBehaviorDaily,
    ArticlePerformanceReport,
)
from content_analytics.services.report_scheduler import (
    generate_report_for_domain,
    run_daily_article_report_scheduler,
)
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from organizations.models import Organization

SCHEDULER_SETTINGS = {
    "CONTENT_ANALYTICS_REPORTS_ENABLED": True,
    "CONTENT_ANALYTICS_REPORT_LOCAL_HOUR": 7,
    "CONTENT_ANALYTICS_REPORT_DEFAULT_TIMEZONE": "Australia/Melbourne",
    "CONTENT_ANALYTICS_REPORT_WINDOW_DAYS": 7,
    "CONTENT_ANALYTICS_REPORT_MIN_VISITS": 20,
}

# 09:30 UTC on 2026-07-21: Melbourne (UTC+10) is 19:30 the same day — due;
# New York (UTC-4, DST) is 05:30 — before the 07:00 local gate.
TICK_NOW = datetime(2026, 7, 21, 9, 30, tzinfo=dt_timezone.utc)


@override_settings(**SCHEDULER_SETTINGS)
class ArticleReportSchedulerTests(TestCase):
    def _org(self, domain: str, *, tz: str = "", enabled=True, provisioned=True):
        organization = Organization.objects.create(name=domain, domain=domain)
        OrganizationContentConfig.objects.create(
            organization=organization, default_timezone=tz
        )
        AnalyticsSite.objects.create(
            organization=organization,
            domain=domain,
            enabled=enabled,
            provision_status=(
                AnalyticsProvisionStatus.PROVISIONED
                if provisioned
                else AnalyticsProvisionStatus.PENDING
            ),
        )
        return organization

    def test_kill_switch_off_is_a_no_op(self):
        self._org("melbourne.example")
        with override_settings(CONTENT_ANALYTICS_REPORTS_ENABLED=False):
            result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result, {"status": "disabled", "generated": 0})
        self.assertEqual(ArticlePerformanceReport.objects.count(), 0)

    def test_generates_once_per_local_date_after_local_hour(self):
        self._org("melbourne.example")
        self._org("newyork.example", tz="America/New_York")

        result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["not_due"], 1)
        report = ArticlePerformanceReport.objects.get()
        self.assertEqual(report.organization.domain, "melbourne.example")
        self.assertEqual(report.report_date, date(2026, 7, 21))

        again = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(again["generated"], 0)
        self.assertEqual(again["existing"], 1)
        self.assertEqual(again["not_due"], 1)
        self.assertEqual(ArticlePerformanceReport.objects.count(), 1)

        # Once New York passes 07:00 local, its report generates for its own
        # local date (2026-07-21 in NY = 2026-07-21 report).
        later = datetime(2026, 7, 21, 11, 30, tzinfo=dt_timezone.utc)
        third = run_daily_article_report_scheduler(now=later)
        self.assertEqual(third["generated"], 1)
        ny_report = ArticlePerformanceReport.objects.get(
            organization__domain="newyork.example"
        )
        self.assertEqual(ny_report.report_date, date(2026, 7, 21))

    def test_invalid_timezone_falls_back_to_default(self):
        self._org("broken-tz.example", tz="Not/AZone")
        result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["generated"], 1)
        report = ArticlePerformanceReport.objects.get()
        # Default Melbourne zone applied: due at 19:30 local.
        self.assertEqual(report.report_date, date(2026, 7, 21))

    def test_only_enabled_provisioned_sites_are_ticked(self):
        self._org("disabled.example", enabled=False)
        self._org("pending.example", provisioned=False)
        result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["existing"], 0)
        self.assertEqual(result["not_due"], 0)
        self.assertEqual(ArticlePerformanceReport.objects.count(), 0)

    def test_one_failing_org_does_not_block_others(self):
        self._org("failing.example")
        self._org("healthy.example")

        from content_analytics.services import report_scheduler as module

        original = module.generate_article_performance_report

        def flaky(organization, report_date, **kwargs):
            if organization.domain == "failing.example":
                raise RuntimeError("boom")
            return original(organization, report_date, **kwargs)

        with patch.object(module, "generate_article_performance_report", side_effect=flaky):
            result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(
            ArticlePerformanceReport.objects.get().organization.domain,
            "healthy.example",
        )
        statuses = {row["domain"]: row["status"] for row in result["results"]}
        self.assertEqual(statuses["failing.example"], "failed")
        self.assertEqual(statuses["healthy.example"], "generated")

    def test_manual_domain_generation_bypasses_kill_switch_and_hour(self):
        organization = self._org("manual.example")
        article = WrittenArticle.objects.create(
            organization=organization,
            title="Manual",
            slug="manual",
            category="featured",
            primary_keyword="manual",
            publish_status=ArticlePublishStatus.LIVE,
        )
        ArticleBehaviorDaily.objects.create(
            organization=organization,
            article=article,
            date=date(2026, 7, 15),
            visits=40,
            pageviews=40,
            visitors=40,
        )
        with override_settings(CONTENT_ANALYTICS_REPORTS_ENABLED=False):
            result = generate_report_for_domain("manual.example", report_date=date(2026, 7, 21))
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["report_date"], "2026-07-21")
        self.assertEqual(result["articles"], 1)

        repeat = generate_report_for_domain("manual.example", report_date=date(2026, 7, 21))
        self.assertEqual(repeat["status"], "existing")

        ArticleBehaviorDaily.objects.create(
            organization=organization,
            article=article,
            date=date(2026, 7, 16),
            visits=60,
            pageviews=60,
            visitors=60,
        )
        forced = generate_report_for_domain(
            "manual.example", report_date=date(2026, 7, 21), force=True
        )
        self.assertEqual(forced["status"], "regenerated")
        report = ArticlePerformanceReport.objects.get()
        self.assertEqual(report.payload["headline"]["humanVisits"], 100)

    def test_unknown_domain_fails_cleanly(self):
        result = generate_report_for_domain("nope.example")
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unknown organization domain", result["error"])

    def test_management_command_paths(self):
        self._org("command.example")
        out = StringIO()
        call_command(
            "run_scheduled_analytics_reports",
            "--domain",
            "command.example",
            "--date",
            "2026-07-21",
            stdout=out,
        )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "generated")

        # The plain tick honors the kill switch.
        with override_settings(CONTENT_ANALYTICS_REPORTS_ENABLED=False):
            out = StringIO()
            call_command("run_scheduled_analytics_reports", stdout=out)
            self.assertEqual(json.loads(out.getvalue()), {"status": "disabled", "generated": 0})

    def test_runner_is_registered_in_scheduler_loop(self):
        from core.management.commands.run_scheduled_discovery import Command

        source = inspect.getsource(Command.handle)
        self.assertIn("article_performance_reports", source)
        self.assertIn("run_daily_article_report_scheduler", source)
