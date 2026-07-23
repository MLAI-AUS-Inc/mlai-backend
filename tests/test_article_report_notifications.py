from __future__ import annotations

import json
from datetime import date, datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    ArticlePerformanceReport,
)
from content_analytics.services import report_notifications, report_scheduler
from content_analytics.services.report_notifications import send_report_ready
from content_analytics.services.report_scheduler import run_daily_article_report_scheduler
from content_factory.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
    OrganizationContentConfig,
)
from organizations.models import Organization

TICK_NOW = datetime(2026, 7, 21, 9, 30, tzinfo=dt_timezone.utc)

NOTIFY_SETTINGS = {
    "CONTENT_ANALYTICS_REPORTS_ENABLED": True,
    "CONTENT_ANALYTICS_REPORT_NOTIFICATIONS_ENABLED": True,
    "CONTENT_ANALYTICS_REPORT_LOCAL_HOUR": 7,
    "CONTENT_ANALYTICS_REPORT_DEFAULT_TIMEZONE": "Australia/Melbourne",
    "FOUNDER_TOOLS_URL": "https://mlai.au",
}

SENT = (True, "provider-1", {"ok": True})
FAILED = (False, "", {"error": "provider unavailable"})


@override_settings(**NOTIFY_SETTINGS)
class ReportNotificationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Notify Co", domain="notify.example")

    def _channel(self, channel_type, route_id, *, consent=NotificationConsentState.ACTIVE, enabled=True):
        return NotificationChannel.objects.create(
            organization=self.organization,
            channel_type=channel_type,
            route_id=route_id,
            consent_state=consent,
            delivery_enabled=enabled,
        )

    def _report(self, report_date=date(2026, 7, 21)):
        return ArticlePerformanceReport.objects.create(
            organization=self.organization,
            report_date=report_date,
            window_start=date(2026, 7, 14),
            window_end=date(2026, 7, 20),
            prior_window_start=date(2026, 7, 7),
            prior_window_end=date(2026, 7, 13),
            payload={
                "headline": {
                    "humanVisits": 120,
                    "engagedReaderRate": 0.35,
                    "ctaClickers": 9,
                    "ctaConversionRate": 0.075,
                    "visitsDelta": 30,
                    "ctaClickersDelta": 2,
                },
                "categoriesSummary": {
                    "top_performer": 2,
                    "high_interest": 1,
                    "needs_attention": 3,
                    "gathering_data": 4,
                },
                "window": {"days": 7, "start": "2026-07-14", "end": "2026-07-20"},
                "articles": [
                    {
                        "title": "Best article",
                        "metrics": {"visits": 50, "ctaConversionRate": 0.1},
                        "categoryLabel": "Top performer",
                    }
                ],
            },
        )

    def test_delivery_row_requires_a_subject(self):
        channel = self._channel(NotificationChannelType.SLACK, "U1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.create(
                channel=channel,
                event_type="report_ready",
                idempotency_key="orphan-key",
            )

    def test_send_targets_only_active_enabled_channels(self):
        self._channel(NotificationChannelType.SLACK, "U1")
        self._channel(NotificationChannelType.EMAIL, "founder@notify.example")
        self._channel(
            NotificationChannelType.WHATSAPP, "+61400000000",
            consent=NotificationConsentState.OPTED_OUT,
        )
        self._channel(NotificationChannelType.EMAIL, "muted@notify.example", enabled=False)
        report = self._report()

        with patch.object(report_notifications, "_send_slack", return_value=SENT) as slack, patch.object(
            report_notifications, "_send_email", return_value=SENT
        ) as email, patch.object(report_notifications, "_send_whatsapp", return_value=SENT) as whatsapp:
            deliveries = send_report_ready(report)

        self.assertEqual(len(deliveries), 2)
        self.assertEqual(slack.call_count, 1)
        self.assertEqual(email.call_count, 1)
        self.assertEqual(whatsapp.call_count, 0)
        for delivery in deliveries:
            self.assertEqual(delivery.status, NotificationDeliveryStatus.SENT)
            self.assertEqual(delivery.performance_report_id, report.pk)
            self.assertIsNone(delivery.automation_run_id)
            self.assertTrue(delivery.idempotency_key.startswith(f"report:{report.pk}:"))
        self.assertEqual(NotificationDelivery.objects.count(), 2)

    def test_idempotent_sent_and_retry_failed(self):
        self._channel(NotificationChannelType.SLACK, "U1")
        self._channel(NotificationChannelType.EMAIL, "founder@notify.example")
        report = self._report()

        with patch.object(report_notifications, "_send_slack", return_value=FAILED), patch.object(
            report_notifications, "_send_email", return_value=SENT
        ):
            first = send_report_ready(report)
        statuses = {d.channel.channel_type: d.status for d in first}
        self.assertEqual(statuses[NotificationChannelType.SLACK], NotificationDeliveryStatus.FAILED)
        self.assertEqual(statuses[NotificationChannelType.EMAIL], NotificationDeliveryStatus.SENT)

        with patch.object(report_notifications, "_send_slack", return_value=SENT) as slack, patch.object(
            report_notifications, "_send_email", return_value=SENT
        ) as email:
            second = send_report_ready(report)
        self.assertEqual(slack.call_count, 1)
        self.assertEqual(email.call_count, 0)
        statuses = {d.channel.channel_type: d.status for d in second}
        self.assertEqual(statuses[NotificationChannelType.SLACK], NotificationDeliveryStatus.SENT)
        self.assertEqual(NotificationDelivery.objects.count(), 2)

    def test_email_uses_template_when_configured(self):
        self._channel(NotificationChannelType.EMAIL, "founder@notify.example")
        report = self._report()

        with patch.object(report_notifications, "_send_email", return_value=SENT) as email:
            send_report_ready(report)
        kwargs = email.call_args.kwargs
        self.assertEqual(kwargs["transactional_message_id"], "")
        self.assertIsNone(kwargs["message_data"])

        NotificationDelivery.objects.all().delete()
        with override_settings(CUSTOMERIO_REPORT_TEMPLATE_ID="tpl-report-1"):
            with patch.object(report_notifications, "_send_email", return_value=SENT) as email:
                send_report_ready(report)
        kwargs = email.call_args.kwargs
        self.assertEqual(kwargs["transactional_message_id"], "tpl-report-1")
        data = kwargs["message_data"]
        self.assertEqual(data["domain"], "notify.example")
        self.assertEqual(data["human_visits"], 120)
        self.assertEqual(data["conversion_display"], "7.5%")
        self.assertEqual(data["top_articles"][0]["title"], "Best article")
        self.assertEqual(data["brief_url"], "https://mlai.au/founder-tools/marketing#analytics")

    def test_report_text_contents(self):
        report = self._report()
        text = report_notifications._report_text(report)
        self.assertIn("notify.example — 2026-07-21", text)
        self.assertIn("120 visits (+30 vs prior)", text)
        self.assertIn("35% engaged", text)
        self.assertIn("9 CTA clickers (7.5% conversion)", text)
        self.assertIn("2 top performers · 1 high-interest · 3 needs attention · 4 gathering data", text)
        self.assertIn("https://mlai.au/founder-tools/marketing#analytics", text)


@override_settings(**NOTIFY_SETTINGS)
class SchedulerNotificationTests(TestCase):
    def _org_with_site(self, domain="sched-notify.example"):
        organization = Organization.objects.create(name=domain, domain=domain)
        OrganizationContentConfig.objects.create(organization=organization, default_timezone="")
        AnalyticsSite.objects.create(
            organization=organization,
            domain=domain,
            enabled=True,
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
        )
        return organization

    def test_scheduler_notifies_only_new_reports(self):
        self._org_with_site()
        with patch.object(report_scheduler, "send_report_ready", return_value=[]) as send:
            result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["notified"], 1)
        self.assertEqual(send.call_count, 1)

        with patch.object(report_scheduler, "send_report_ready", return_value=[]) as send:
            again = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(again["existing"], 1)
        self.assertEqual(again["notified"], 0)
        self.assertEqual(send.call_count, 0)

    def test_scheduler_notify_failure_is_isolated(self):
        self._org_with_site()
        with patch.object(
            report_scheduler, "send_report_ready", side_effect=RuntimeError("smtp down")
        ):
            result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["notify_failed"], 1)
        self.assertEqual(ArticlePerformanceReport.objects.count(), 1)

    def test_scheduler_respects_notification_kill_switch(self):
        self._org_with_site()
        with override_settings(CONTENT_ANALYTICS_REPORT_NOTIFICATIONS_ENABLED=False):
            with patch.object(report_scheduler, "send_report_ready", return_value=[]) as send:
                result = run_daily_article_report_scheduler(now=TICK_NOW)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["notified"], 0)
        self.assertEqual(send.call_count, 0)

    def test_manual_command_notify_flag(self):
        organization = self._org_with_site("manual-notify.example")
        NotificationChannel.objects.create(
            organization=organization,
            channel_type=NotificationChannelType.SLACK,
            route_id="U9",
            consent_state=NotificationConsentState.ACTIVE,
            delivery_enabled=True,
        )
        out = StringIO()
        with patch.object(report_notifications, "_send_slack", return_value=SENT):
            call_command(
                "run_scheduled_analytics_reports",
                "--domain",
                "manual-notify.example",
                "--date",
                "2026-07-21",
                "--notify",
                stdout=out,
            )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "generated")
        self.assertEqual(payload["deliveries"], [{"channel_type": "slack", "status": "sent"}])
