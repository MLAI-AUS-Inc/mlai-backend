"""Sanitized business-outcome aggregation without database or network access."""

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from content_factory.website_health import summarize_website_runs


class WebsiteHealthSummaryTests(TestCase):
    def test_failed_run_is_visible_even_without_a_task_exception(self):
        start = datetime(2026, 10, 4, tzinfo=timezone.utc)
        report = summarize_website_runs([
            {"status": "failed", "result": {"success": False, "error_code": "TEMPLATE_VALIDATION_FAILED", "secret": "never report"}, "created_at": start, "updated_at": start + timedelta(seconds=90)},
            {"status": "running", "result": {}, "created_at": start, "updated_at": start + timedelta(seconds=300)},
        ])
        self.assertEqual(report["statuses"], {"failed": 1, "running": 1})
        self.assertEqual(report["failureCodes"], {"TEMPLATE_VALIDATION_FAILED": 1})
        self.assertEqual(report["meanLifecycleSeconds"], 90)
        self.assertNotIn("never report", repr(report))

    def test_arbitrary_error_bodies_and_invalid_durations_are_not_reported(self):
        now = datetime.now(timezone.utc)
        report = summarize_website_runs([{"status": "failed", "result": {"error_code": "https://secret.example/token?key=private"}, "created_at": now, "updated_at": now - timedelta(seconds=1)}])
        self.assertEqual(report["failureCodes"], {})
        self.assertIsNone(report["meanLifecycleSeconds"])
