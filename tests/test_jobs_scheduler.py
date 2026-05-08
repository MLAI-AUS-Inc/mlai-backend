import io
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from jobs.models import JobListing, JobRun
from jobs.services import job_pipeline
from jobs.services.slack import format_slack_message


@override_settings(
    JOBS_SCHEDULER_ENABLED=True,
    JOBS_SCHEDULE_TIMEZONE="Australia/Melbourne",
    JOBS_SCHEDULE_HOUR=7,
    JOBS_SCHEDULE_MINUTE=0,
    JOBS_SCHEDULER_POST_TO_SLACK=False,
    JOBS_SCHEDULER_POST_TO_NOTION=False,
    JOBS_SCHEDULER_MAX_PAGES=1,
    JOBS_SCHEDULER_PER_KEYWORD_LIMIT=1,
    JOBS_TOP_PICK_LIMIT=7,
    JOBS_TRIGGER_TOKEN="jobs-trigger-secret",
)
class JobsSchedulerTests(TestCase):
    @staticmethod
    def _melbourne_at(hour, minute):
        return datetime(2026, 5, 4, hour, minute, tzinfo=ZoneInfo("Australia/Melbourne"))

    @patch("jobs.services.job_pipeline.run_daily_jobs")
    def test_scheduler_runs_at_7am_melbourne(self, mock_run_daily_jobs):
        def complete_run(run_id, *args, **kwargs):
            run = JobRun.objects.get(run_id=run_id)
            run.status = "completed"
            run.started_at = timezone.now()
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "started_at", "completed_at", "updated_at"])

        mock_run_daily_jobs.side_effect = complete_run

        before = job_pipeline.run_daily_jobs_scheduler(now=self._melbourne_at(6, 59))
        at_schedule = job_pipeline.run_daily_jobs_scheduler(now=self._melbourne_at(7, 0))

        self.assertEqual(before["status"], "skipped")
        self.assertEqual(before["reason"], "before_schedule_window")
        self.assertEqual(at_schedule["status"], "completed")
        self.assertEqual(at_schedule["run_date"], "2026-05-04")
        self.assertTrue(
            JobRun.objects.filter(run_date="2026-05-04", trigger_source="daily_scheduler").exists()
        )

    @patch("core.management.commands.run_scheduled_discovery.run_daily_jobs_scheduler")
    @patch("core.management.commands.run_scheduled_discovery.run_daily_discovery_scheduler")
    def test_management_command_runs_jobs_even_if_discovery_fails(self, mock_discovery, mock_jobs):
        mock_discovery.side_effect = RuntimeError("discovery boom")
        mock_jobs.return_value = {"status": "skipped", "reason": "before_schedule_window"}
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("run_scheduled_discovery", stdout=stdout)

        mock_jobs.assert_called_once()
        output = stdout.getvalue()
        self.assertIn('"daily_discovery": {"error": "discovery boom", "status": "failed"}', output)
        self.assertIn('"jobs": {"reason": "before_schedule_window", "status": "skipped"}', output)

    def test_slack_payload_is_capped_to_seven_jobs(self):
        run = JobRun.objects.create(run_date="2026-05-04", run_id="2026-05-04-slack-test")
        for rank in range(1, 10):
            JobListing.objects.create(
                run=run,
                run_date=run.run_date,
                title=f"AI role {rank}",
                company_name=f"Company {rank}",
                location="Melbourne VIC",
                job_url=f"https://example.com/jobs/{rank}",
                source_name="SEEK",
                dedupe_key=f"ai-role-{rank}|company-{rank}|melbourne",
                is_top_pick=True,
                rank=rank,
                why_selected="strong AI relevance, Australia fit",
            )

        payload = format_slack_message(run.run_date, list(run.jobs.order_by("rank")), "https://example.com/all")

        self.assertIn("7. AI role 7", payload["text"])
        self.assertNotIn("8. AI role 8", payload["text"])
        self.assertNotIn("9. AI role 9", payload["text"])

    def test_daily_run_trigger_accepts_jobs_trigger_bearer_token(self):
        client = APIClient()

        response = client.post(
            "/api/v1/jobs/daily-run",
            {"collect_live": False, "post_to_slack": False, "post_to_notion": False},
            format="json",
            HTTP_AUTHORIZATION="Bearer jobs-trigger-secret",
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(JobRun.objects.count(), 1)
        run = JobRun.objects.get()
        self.assertEqual(run.status, "queued")
        self.assertFalse(run.collect_live)

    def test_daily_run_trigger_rejects_invalid_bearer_token(self):
        client = APIClient()

        response = client.post(
            "/api/v1/jobs/daily-run",
            {"collect_live": False, "post_to_slack": False, "post_to_notion": False},
            format="json",
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(JobRun.objects.count(), 0)

    @patch("jobs.services.job_pipeline.post_failure_alert")
    @patch("jobs.services.job_pipeline.select_top_jobs")
    @patch("jobs.services.job_pipeline.insert_matched_jobs")
    @patch("jobs.services.job_pipeline.fetch_raw_jobs")
    def test_partial_source_errors_do_not_send_failure_alert(
        self,
        mock_fetch_raw_jobs,
        mock_insert_matched_jobs,
        mock_select_top_jobs,
        mock_post_failure_alert,
    ):
        run = JobRun.objects.create(
            run_date="2026-05-04",
            run_id="2026-05-04-partial-source-errors",
            post_to_notion=False,
            post_to_slack=False,
        )
        job = JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="AI Engineer",
            company_name="Example Co",
            location="Melbourne VIC",
            job_url="https://example.com/jobs/ai-engineer",
            source_name="SEEK",
            dedupe_key="ai-engineer|example-co|melbourne",
        )
        mock_fetch_raw_jobs.return_value = (
            [{"title": "AI Engineer", "job_url": "https://example.com/jobs/ai-engineer"}],
            ["CareerOne: 403 Client Error: Forbidden"],
        )
        mock_insert_matched_jobs.return_value = [job]
        mock_select_top_jobs.return_value = [job]

        job_pipeline.run_daily_jobs(
            run.run_id,
            collect_live=True,
            post_to_slack=False,
            post_to_notion=False,
        )

        run.refresh_from_db()
        self.assertEqual(run.status, "completed_with_source_errors")
        self.assertIn("CareerOne", run.error_message)
        mock_post_failure_alert.assert_not_called()
