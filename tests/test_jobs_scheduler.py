import io
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from jobs.models import JobListing, JobRun, SourceRunLog
from jobs.conf import settings as jobs_settings
from jobs.services import job_pipeline
from jobs.services.company_metadata import enrich_company_metadata
from jobs.services.job_scoring import ai_relevance_score, is_target_role_title, rerank_for_relevance, score_job
from jobs.services.summaries import build_job_summary
from jobs.services.location_eligibility import apply_disqualification_scan, classify_location_eligibility
from jobs.services.slack import format_slack_message, post_slack_message


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
                title=f"AI Engineer {rank}",
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

        self.assertIn("7. AI Engineer 7", payload["text"])
        self.assertNotIn("8. AI Engineer 8", payload["text"])
        self.assertNotIn("9. AI Engineer 9", payload["text"])
        self.assertIn("blocks", payload)
        self.assertEqual(payload["blocks"][0]["type"], "header")
        self.assertIn("Job link:", str(payload["blocks"]))
        self.assertIn("Apply now", str(payload["blocks"]))

    @patch("jobs.services.slack._slack_service")
    def test_slack_post_passes_block_layout_to_slack_service(self, mock_slack_service):
        service = mock_slack_service.return_value
        service.get_channel_id_by_name.return_value = "C123"
        service.send_message.return_value = (True, "123.456")
        payload = {
            "channel": "#jobs",
            "text": "fallback",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "structured"}}],
        }

        posted, error = post_slack_message(payload)

        self.assertTrue(posted)
        self.assertIsNone(error)
        service.send_message.assert_called_once_with("C123", "fallback", blocks=payload["blocks"])

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_final_screen_removes_invalid_stored_rows_before_ranking(self, _mock_judge):
        run = JobRun.objects.create(run_date="2026-05-30", run_id="2026-05-30-final-screen-ranking")
        common = {
            "run": run,
            "run_date": run.run_date,
            "location": "Melbourne, Australia",
            "source_name": "SEEK",
            "ai_score": 1.0,
            "ranking_score": 0.99,
        }
        JobListing.objects.create(
            **common,
            title="Senior Retail Category Manager",
            company_name="McCain Foods",
            description="Use AI insights in retail operations.",
            job_url="https://example.com/jobs/retail-manager",
            dedupe_key="retail-manager|mccain",
        )
        JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="AI Engineer",
            company_name="Example EU Co",
            location="Remote - worldwide",
            description="Build artificial intelligence systems. You can work from any European Union country.",
            job_url="https://example.com/jobs/eu-only-ai-engineer",
            source_name="Himalayas",
            dedupe_key="ai-engineer|eu-only",
            ai_score=1.0,
            ranking_score=0.98,
        )
        JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="Product Designer",
            company_name="Example SaaS Co",
            location="Remote (US or Canada)",
            description="Join our venture-backed SaaS software company and design its core product.",
            job_url="https://example.com/jobs/us-canada-product-designer",
            source_name="TopStartups.io",
            dedupe_key="product-designer|us-canada",
            startup_score=1.0,
            ranking_score=0.97,
        )
        valid = JobListing.objects.create(
            **common,
            title="AI Engineer",
            company_name="Example AI Co",
            description="Build artificial intelligence systems.",
            job_url="https://example.com/jobs/ai-engineer",
            dedupe_key="ai-engineer|example",
        )

        selected = job_pipeline.select_top_jobs(run)

        self.assertEqual(selected, [valid])

    def test_slack_final_screen_removes_invalid_stored_rows(self):
        run = JobRun.objects.create(run_date="2026-05-30", run_id="2026-05-30-final-screen-slack")
        retail = JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="Senior Retail Category Manager",
            company_name="McCain Foods",
            location="Melbourne, Australia",
            description="Use AI insights in retail operations.",
            job_url="https://example.com/jobs/retail-manager",
            source_name="SEEK",
            dedupe_key="retail-manager|mccain|slack",
            ai_score=1.0,
            ranking_score=0.99,
            is_top_pick=True,
            rank=1,
        )
        valid = JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="AI Engineer",
            company_name="Example AI Co",
            location="Melbourne, Australia",
            description="Build artificial intelligence systems.",
            job_url="https://example.com/jobs/ai-engineer",
            source_name="SEEK",
            dedupe_key="ai-engineer|example|slack",
            ai_score=1.0,
            ranking_score=0.8,
            is_top_pick=True,
            rank=2,
        )

        payload = format_slack_message(run.run_date, [retail, valid], "https://example.com/all")

        self.assertNotIn("Senior Retail Category Manager", payload["text"])
        self.assertIn("1. AI Engineer", payload["text"])

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_previous_top_pick_is_not_repeated_even_when_url_changes(self, _mock_judge):
        previous_run = JobRun.objects.create(run_date="2026-05-30", run_id="2026-05-30-history")
        current_run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-history")
        common = {
            "run_date": current_run.run_date,
            "title": "AI Engineer",
            "company_name": "Example AI Co",
            "location": "Melbourne, Australia",
            "description": "Build artificial intelligence systems.",
            "source_name": "SEEK",
        }
        JobListing.objects.create(
            run=previous_run,
            run_date=previous_run.run_date,
            title=common["title"],
            company_name=common["company_name"],
            location=common["location"],
            description=common["description"],
            job_url="https://example.com/jobs/ai-engineer",
            source_name=common["source_name"],
            dedupe_key="ai-engineer|example-ai|old-url",
            is_top_pick=True,
            rank=1,
        )
        JobListing.objects.create(
            run=current_run,
            job_url="https://example.com/jobs/ai-engineer",
            dedupe_key="ai-engineer|example-ai|old-url",
            **common,
        )
        JobListing.objects.create(
            run=current_run,
            job_url="https://example.com/jobs/ai-engineer-v2",
            dedupe_key="ai-engineer|example-ai|new-url",
            **common,
        )

        selected = job_pipeline.select_top_jobs(current_run)

        self.assertEqual(selected, [])

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_top_jobs_prefers_fewer_than_seven_over_repeating_history(self, _mock_judge):
        previous_run = JobRun.objects.create(run_date="2026-05-30", run_id="2026-05-30-repeat-history")
        current_run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-repeat-history")

        for index in range(7):
            JobListing.objects.create(
                run=previous_run,
                run_date=previous_run.run_date,
                title=f"AI Engineer {index + 1}",
                company_name=f"Example AI Company {index + 1}",
                location="Melbourne, Australia",
                description="Build artificial intelligence systems.",
                job_url=f"https://example.com/jobs/old-ai-engineer-{index + 1}",
                source_name="SEEK",
                dedupe_key=f"old-ai-engineer-{index + 1}|example",
                is_top_pick=True,
                rank=index + 1,
            )
            JobListing.objects.create(
                run=current_run,
                run_date=current_run.run_date,
                title=f"AI Engineer {index + 1}",
                company_name=f"Example AI Company {index + 1}",
                location="Melbourne, Australia",
                description="Build artificial intelligence systems.",
                job_url=f"https://example.com/jobs/new-ai-engineer-{index + 1}",
                source_name="SEEK",
                dedupe_key=f"new-ai-engineer-{index + 1}|example",
                ai_score=1.0,
                ranking_score=0.9 - index / 100,
            )

        fresh = JobListing.objects.create(
            run=current_run,
            run_date=current_run.run_date,
            title="Machine Learning Engineer",
            company_name="New AI Company",
            location="Sydney, Australia",
            description="Build machine learning systems.",
            job_url="https://example.com/jobs/new-ml-engineer",
            source_name="SEEK",
            dedupe_key="new-ml-engineer|new-ai-company",
            ai_score=1.0,
            ranking_score=0.7,
        )

        selected = job_pipeline.select_top_jobs(current_run)

        self.assertEqual(selected, [fresh])

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_top_jobs_fills_seven_slots_from_screened_candidates_when_source_mix_is_limited(self, _mock_judge):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-seven-screened-jobs")
        for index in range(7):
            JobListing.objects.create(
                run=run,
                run_date=run.run_date,
                title=f"AI Engineer {index + 1}",
                company_name=f"Example AI Company {index + 1}",
                location="Remote - worldwide",
                description="Build artificial intelligence and machine learning systems.",
                job_url=f"https://example.com/jobs/ai-engineer-{index + 1}",
                source_name="Himalayas",
                dedupe_key=f"ai-engineer-{index + 1}|example",
                ai_score=1.0,
                ranking_score=0.9 - index / 100,
            )

        selected = job_pipeline.select_top_jobs(run)

        self.assertEqual(len(selected), 7)
        self.assertEqual([job.rank for job in selected], list(range(1, 8)))
        self.assertTrue(all(job.is_top_pick for job in selected))

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_top_jobs_prefers_ai_roles_before_startup_only_broad_roles(self, _mock_judge):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-ai-before-startup-only")
        for index in range(7):
            JobListing.objects.create(
                run=run,
                run_date=run.run_date,
                title=f"AI Engineer {index + 1}",
                company_name=f"Example AI Company {index + 1}",
                location="Melbourne, Australia",
                description="Build artificial intelligence and machine learning systems.",
                job_url=f"https://example.com/jobs/ai-engineer-{index + 1}",
                source_name="SEEK",
                source_type="broad_board",
                dedupe_key=f"ai-engineer-{index + 1}|example",
                ai_score=1.0,
                ranking_score=0.65 - index / 100,
            )
        JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="Product Designer",
            company_name="Example SaaS Startup",
            location="Remote - worldwide",
            description="Join our venture-backed SaaS software company and design its core product.",
            job_url="https://example.com/jobs/product-designer",
            source_name="TopStartups.io",
            source_type="startup_board",
            dedupe_key="product-designer|example-saas-startup",
            ai_score=0.0,
            startup_score=1.0,
            ranking_score=0.95,
        )

        selected = job_pipeline.select_top_jobs(run)

        self.assertEqual(len(selected), 7)
        self.assertNotIn("Product Designer", [job.title for job in selected])

    @patch("jobs.services.job_pipeline.judge_top_candidates", side_effect=lambda jobs, candidate_limit: (jobs, {}))
    def test_top_jobs_uses_startup_only_broad_role_only_as_fill_slot(self, _mock_judge):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-startup-fill-slot")
        for index in range(6):
            JobListing.objects.create(
                run=run,
                run_date=run.run_date,
                title=f"AI Engineer {index + 1}",
                company_name=f"Example AI Company {index + 1}",
                location="Melbourne, Australia",
                description="Build artificial intelligence and machine learning systems.",
                job_url=f"https://example.com/jobs/fill-ai-engineer-{index + 1}",
                source_name="SEEK",
                source_type="broad_board",
                dedupe_key=f"fill-ai-engineer-{index + 1}|example",
                ai_score=1.0,
                ranking_score=0.65 - index / 100,
            )
        fallback = JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="Product Designer",
            company_name="Example SaaS Startup",
            location="Remote - worldwide",
            description="Join our venture-backed SaaS software company and design its core product.",
            job_url="https://example.com/jobs/fill-product-designer",
            source_name="TopStartups.io",
            source_type="startup_board",
            dedupe_key="fill-product-designer|example-saas-startup",
            ai_score=0.0,
            startup_score=1.0,
            ranking_score=0.95,
        )

        selected = job_pipeline.select_top_jobs(run)

        self.assertEqual(len(selected), 7)
        self.assertEqual(selected[-1], fallback)

    def test_final_screen_rejects_old_dated_listing(self):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-stale")
        stale = JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="AI Engineer",
            company_name="Example AI Co",
            location="Melbourne, Australia",
            description="Build artificial intelligence systems.",
            job_url="https://example.com/jobs/stale-ai-engineer",
            source_name="SEEK",
            dedupe_key="stale-ai-engineer|example",
            date_posted=timezone.now() - timedelta(hours=73),
        )

        payload = format_slack_message(run.run_date, [stale], "https://example.com/all")

        self.assertNotIn("AI Engineer", payload["text"])

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

    def test_jobs_history_endpoint_returns_runs_counts_errors_and_top_picks(self):
        JobRun.objects.create(run_date="x", run_id="x-history-placeholder")
        older = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-history-endpoint",
            fetched_count=12,
            matched_count=4,
            deduped_count=4,
            ranked_count=1,
        )
        newer = JobRun.objects.create(
            run_date="2026-05-31",
            run_id="2026-05-31-history-endpoint",
            fetched_count=20,
            matched_count=8,
            deduped_count=7,
            ranked_count=1,
        )
        SourceRunLog.objects.create(
            run=newer,
            source_name="CareerOne",
            status="error",
            error_message="403 Client Error",
        )
        JobListing.objects.create(
            run=newer,
            run_date=newer.run_date,
            title="AI Engineer",
            company_name="Example AI Co",
            location="Melbourne, Australia",
            job_url="https://example.com/jobs/history-ai-engineer",
            source_name="SEEK",
            dedupe_key="history-ai-engineer|example",
            is_top_pick=True,
            rank=1,
        )

        response = APIClient().get("/api/v1/jobs/history")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["run_id"] for row in response.data[:2]], [newer.run_id, older.run_id])
        self.assertNotIn("x-history-placeholder", [row["run_id"] for row in response.data])
        self.assertEqual(response.data[0]["counts"]["fetched"], 20)
        self.assertEqual(response.data[0]["source_errors"][0]["source_name"], "CareerOne")
        self.assertEqual(response.data[0]["top_jobs"][0]["title"], "AI Engineer")
        self.assertEqual(response.data[0]["status_url"], f"/api/v1/jobs/runs/{newer.run_id}")

    def test_daily_jobs_html_page_renders_populated_run(self):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-html-page")
        JobListing.objects.create(
            run=run,
            run_date=run.run_date,
            title="AI Engineer",
            company_name="Example AI Co",
            location="Melbourne, Australia",
            job_url="https://example.com/jobs/html-ai-engineer",
            source_name="SEEK",
            dedupe_key="html-ai-engineer|example",
            bucket="australian_ai",
            ranking_score=0.8,
        )

        response = APIClient().get(f"/api/v1/jobs/daily/{run.run_date}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("AI Engineer", response.content.decode())
        self.assertIn("JSON feed", response.content.decode())

    def test_jobs_history_html_page_renders_browser_friendly_links(self):
        run = JobRun.objects.create(run_date="2026-05-31", run_id="2026-05-31-history-html")

        response = APIClient().get("/api/v1/jobs/history/view")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Roo Jobs Daily History", response.content.decode())
        self.assertIn(f"/api/v1/jobs/daily/{run.run_date}", response.content.decode())

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

    @override_settings(SLACK_BOT_TOKEN="xoxb-primary", JOBS_SLACK_BOT_TOKEN="")
    def test_jobs_settings_accepts_primary_slack_bot_token(self):
        self.assertEqual(jobs_settings.slack_bot_token, "xoxb-primary")

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_himalayas_eu_only_description_is_filtered_even_when_location_says_worldwide(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-himalayas-eu-only",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Himalayas",
                "source_type": "remote_board",
                "source_quality_score": 0.82,
                "title": "Chief Technology Officer",
                "company_name": "saas.group",
                "location": "Remote - worldwide",
                "remote_region": "Remote - worldwide",
                "description": (
                    "Lead our AI/RAG platform and engineering team. "
                    "Ultimate flexibility: We're 100% remote. "
                    "You can work from any European Union country."
                ),
                "job_url": "https://himalayas.app/companies/saas-group/jobs/chief-technology-officer-cto",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_restricted_remote_job_from_other_source_is_filtered(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-other-source-eu-only",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "source_quality_score": 0.7,
                "title": "AI Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": (
                    "Build AI tooling for startup customers. "
                    "This is a remote role, but candidates must be based in Europe."
                ),
                "job_url": "https://example.com/jobs/ai-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_us_work_authorisation_job_is_suppressed(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-us-work-auth",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "source_quality_score": 0.7,
                "title": "Machine Learning Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": "Build LLM features. Candidates must be authorized to work in the United States.",
                "job_url": "https://example.com/jobs/ml-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_restrictions_win_when_job_also_mentions_australia(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-australia-negative-location",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "source_quality_score": 0.7,
                "title": "AI Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": "Build AI products. Remote role. US only. Not available in Australia.",
                "job_url": "https://example.com/jobs/ai-engineer-us-only-not-au",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_europe_restriction_wins_when_job_mentions_apac_customers(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-apac-mentioned-europe-restricted",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Himalayas",
                "source_type": "remote_board",
                "source_quality_score": 0.82,
                "title": "Machine Learning Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": "Build machine learning products for APAC customers. Candidates must be based in Europe.",
                "job_url": "https://example.com/jobs/ml-engineer-europe-only-apac-customers",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_remote_not_apac_is_filtered(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-not-apac",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "source_quality_score": 0.7,
                "title": "AI Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": "Build AI products. Remote, not APAC.",
                "job_url": "https://example.com/jobs/ai-engineer-not-apac",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_remote_only_from_europe_is_filtered(self):
        run = JobRun.objects.create(
            run_date="2026-05-29",
            run_id="2026-05-29-only-from-europe",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "source_quality_score": 0.7,
                "title": "Machine Learning Engineer",
                "company_name": "Example Co",
                "location": "Remote - worldwide",
                "description": "Build machine learning products. Remote, only from Europe.",
                "job_url": "https://example.com/jobs/ml-engineer-only-from-europe",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    def test_non_startup_and_seniority_signals_apply_ranking_penalties(self):
        base_job = {
            "title": "AI Engineer",
            "company_name": "Example Co",
            "location": "Remote - worldwide",
            "remote_region": "Global",
            "remote_eligibility": "australia_eligible",
            "remote_eligibility_score": 0.9,
            "source_name": "Example Remote Board",
            "source_type": "remote_board",
            "source_quality_score": 0.7,
            "description": "Build AI tooling for customers using machine learning and LLM systems.",
            "job_url": "https://example.com/jobs/ai-engineer",
            "apply_url": "https://example.com/jobs/ai-engineer",
        }
        clean_score = score_job(dict(base_job))["ranking_score"]
        penalized = apply_disqualification_scan(
            {
                **base_job,
                "description": (
                    "Build AI tooling for a global enterprise consulting firm. "
                    "This VP-level role requires 15+ years experience."
                ),
            }
        )
        penalized_score = score_job(penalized)["ranking_score"]

        self.assertEqual(penalized["screening_status"], "penalized")
        self.assertTrue(any(signal["category"] == "company_stage" for signal in penalized["disqualification_signals"]))
        self.assertIn("Very high years-of-experience requirement", penalized["screening_reasons"])
        self.assertLess(penalized_score, clean_score)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_retail_manager_is_filtered_even_when_description_mentions_ai_and_startups(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-retail-manager",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "source_quality_score": 0.9,
                "title": "Retail Manager",
                "company_name": "Example Co",
                "location": "Sydney, Australia",
                "description": "Join our AI startup and use data to improve retail operations.",
                "job_url": "https://example.com/jobs/retail-manager",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])
        self.assertEqual(JobListing.objects.filter(run=run).count(), 0)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_missing_company_name_can_be_inferred_from_description(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-company-from-description",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Workforce Australia",
                "source_type": "government_board",
                "source_quality_score": 0.62,
                "title": "Machine Learning Researcher",
                "company_name": None,
                "location": "Sydney, NSW",
                "description": "Susquehanna is expanding the Machine Learning group and seeking exceptional researchers.",
                "job_url": "https://www.workforceaustralia.gov.au/individuals/jobs/details/2350786688",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0].company_name, "Susquehanna")

    def test_target_role_title_gate_accepts_intended_job_families(self):
        self.assertTrue(is_target_role_title("Machine Learning Engineer"))
        self.assertTrue(is_target_role_title("Data Analyst"))
        self.assertTrue(is_target_role_title("Senior Software Engineer"))
        self.assertTrue(is_target_role_title("Backend Developer"))
        self.assertFalse(is_target_role_title("Retail Manager"))
        self.assertFalse(is_target_role_title("Project Manager (AI & Big Data)"))

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_technical_startup_role_still_requires_ai_relevance(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-ai-relevance",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "source_quality_score": 0.9,
                "title": "Senior Software Engineer",
                "company_name": "Example Co",
                "location": "Melbourne, Australia",
                "description": "Build checkout and inventory systems for our retail platform.",
                "job_url": "https://example.com/jobs/software-engineer",
            },
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "source_quality_score": 0.9,
                "title": "Senior Software Engineer",
                "company_name": "Example AI Co",
                "location": "Melbourne, Australia",
                "description": "Build machine learning infrastructure for our SaaS AI platform.",
                "job_url": "https://example.com/jobs/ai-software-engineer",
            },
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual([job.company_name for job in inserted], ["Example AI Co"])

    def test_post_score_rerank_only_boosts_credible_startup_relevance(self):
        established = rerank_for_relevance(
            score_job(
                enrich_company_metadata(
                    {
                        "source_name": "Example Startup Board",
                        "source_type": "startup_board",
                        "source_quality_score": 0.9,
                        "title": "AI Engineer",
                        "company_name": "McCain Foods",
                        "location": "Melbourne, Australia",
                        "description": "Build artificial intelligence systems for a global food manufacturer.",
                        "job_url": "https://example.com/jobs/mccain-ai-engineer",
                    }
                )
            )
        )
        startup = rerank_for_relevance(
            score_job(
                enrich_company_metadata(
                    {
                        "source_name": "Example Board",
                        "source_type": "broad_board",
                        "source_quality_score": 0.72,
                        "title": "AI Engineer",
                        "company_name": "Example AI Startup",
                        "location": "Melbourne, Australia",
                        "description": "Build artificial intelligence systems for our venture-backed startup.",
                        "job_url": "https://example.com/jobs/startup-ai-engineer",
                    }
                )
            )
        )

        self.assertEqual(established["startup_score"], 0.0)
        self.assertNotIn("startup signal", build_job_summary(established))
        self.assertGreater(startup["startup_score"], established["startup_score"])
        self.assertGreater(startup["ranking_score"], established["ranking_score"])

    def test_generic_growth_word_does_not_infer_startup_stage(self):
        enriched = enrich_company_metadata(
            {
                "title": "AI Engineer",
                "company_name": "McCain Foods",
                "description": "Support growth across a global food manufacturing business.",
            }
        )

        self.assertIsNone(enriched["company_stage"])

    def test_ai_keyword_groups_count_synonyms_once(self):
        self.assertEqual(ai_relevance_score("Work with LLMs and large language models."), 0.5)

    def test_approved_ai_concepts_qualify_technical_roles(self):
        for description in (
            "Build generative AI features.",
            "Ship computer vision models.",
            "Develop NLP pipelines.",
            "Operate MLOps infrastructure.",
        ):
            scored = score_job(
                {
                    "title": "Software Engineer",
                    "description": description,
                }
            )
            self.assertGreaterEqual(scored["ai_score"], 0.5)

        self.assertTrue(is_target_role_title("Generative AI Engineer"))
        self.assertTrue(is_target_role_title("Computer Vision Engineer"))

    def test_data_engineer_is_not_automatically_ai_relevant(self):
        plain_data = score_job(
            {
                "title": "Data Engineer",
                "description": "Build warehouse ingestion and reporting pipelines.",
            }
        )
        ml_data = score_job(
            {
                "title": "Data Engineer",
                "description": "Build machine learning feature pipelines.",
            }
        )

        self.assertEqual(plain_data["ai_score"], 0.0)
        self.assertGreaterEqual(ml_data["ai_score"], 0.5)

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_broad_software_role_rejects_weak_ai_mentions_without_substantive_ai(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-weak-ai-software",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "source_quality_score": 0.9,
                "title": "Software Engineer",
                "company_name": "Example SaaS Co",
                "location": "Sydney, Australia",
                "description": "Build workflow software for a SaaS company using AI tools to improve productivity.",
                "job_url": "https://example.com/jobs/weak-ai-software-engineer",
            },
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "source_quality_score": 0.9,
                "title": "Software Engineer",
                "company_name": "Example ML SaaS Co",
                "location": "Sydney, Australia",
                "description": "Build machine learning infrastructure for a SaaS AI platform.",
                "job_url": "https://example.com/jobs/ml-software-engineer",
            },
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual([job.company_name for job in inserted], ["Example ML SaaS Co"])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_data_analyst_requires_substantive_ai_concept(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-data-analyst-ai-concept",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Board",
                "source_type": "broad_board",
                "title": "Data Analyst",
                "company_name": "Example Retailer",
                "location": "Sydney, Australia",
                "description": "Create dashboards and use AI tools for reporting productivity.",
                "job_url": "https://example.com/jobs/weak-data-analyst",
            },
            {
                "source_name": "Example Board",
                "source_type": "broad_board",
                "title": "Data Analyst",
                "company_name": "Example Research Lab",
                "location": "Sydney, Australia",
                "description": "Analyse LLM evaluation results and machine learning model performance.",
                "job_url": "https://example.com/jobs/ml-data-analyst",
            },
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual([job.company_name for job in inserted], ["Example Research Lab"])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_scholarship_listing_is_suppressed_even_when_ai_relevant(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-scholarship-suppressed",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "CareerOne",
                "source_type": "broad_board",
                "title": "Machine Learning, AI and Data Analyst",
                "company_name": "Example University",
                "location": "Australia",
                "description": "PhD scholarship opportunity processing intelligence for green metals using machine learning.",
                "job_url": "https://example.com/jobs/phd-scholarship-machine-learning",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_us_timezone_restricted_remote_job_is_suppressed(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-us-timezone-restricted",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Himalayas",
                "source_type": "remote_board",
                "title": "AI Engineer",
                "company_name": "Example Remote Co",
                "location": "Remote - worldwide",
                "description": "Build artificial intelligence systems. Candidates must work US time zones.",
                "job_url": "https://example.com/jobs/us-timezone-ai-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_remote_usa_job_is_suppressed_for_australia_feed(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-remote-usa-restricted",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "TopStartups.io",
                "source_type": "startup_board",
                "title": "Machine Learning Engineer",
                "company_name": "Example US Remote Co",
                "location": "Remote, USA",
                "description": "Build machine learning systems for autonomous vehicles.",
                "job_url": "https://example.com/jobs/remote-usa-ml-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_remote_oceania_job_is_not_enough_for_australia_feed(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-remote-oceania-restricted",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Remote Board",
                "source_type": "remote_board",
                "title": "Machine Learning Engineer",
                "company_name": "Example Remote Co",
                "location": "Remote - Oceania",
                "description": "Build machine learning systems for remote candidates in Oceania.",
                "job_url": "https://example.com/jobs/remote-oceania-ml-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(inserted, [])

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_remote_worldwide_job_remains_eligible_for_australia_feed(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-remote-worldwide-eligible",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Himalayas",
                "source_type": "remote_board",
                "title": "Machine Learning Engineer",
                "company_name": "Example Global Co",
                "location": "Remote - worldwide",
                "description": "Build machine learning systems for a global remote team.",
                "job_url": "https://example.com/jobs/remote-worldwide-ml-engineer",
            }
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0].remote_eligibility, "australia_eligible")

    @override_settings(JOBS_LLM_LOCATION_CHECK_ENABLED=False)
    def test_broad_roles_require_an_it_startup_but_explicit_ai_analyst_does_not(self):
        run = JobRun.objects.create(
            run_date="2026-05-30",
            run_id="2026-05-30-expanded-role-families",
            post_to_notion=False,
            post_to_slack=False,
        )
        raw_jobs = [
            {
                "source_name": "Example Board",
                "source_type": "broad_board",
                "title": "AI Analyst",
                "company_name": "Example University",
                "location": "Sydney, Australia",
                "description": "Evaluate artificial intelligence models for a university research team.",
                "job_url": "https://example.com/jobs/university-ai-analyst",
            },
            {
                "source_name": "Example Board",
                "source_type": "broad_board",
                "title": "Data Analyst",
                "company_name": "Example Retailer",
                "location": "Sydney, Australia",
                "description": "Prepare sales dashboards and weekly reporting.",
                "job_url": "https://example.com/jobs/plain-data-analyst",
            },
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "title": "Product Designer",
                "company_name": "Example SaaS Co",
                "location": "Melbourne, Australia",
                "description": "Join our venture-backed SaaS software company and design its core product.",
                "job_url": "https://example.com/jobs/saas-product-designer",
            },
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "title": "Product Designer",
                "company_name": "Example Foods",
                "location": "Melbourne, Australia",
                "description": "Join our venture-backed food brand and redesign retail packaging.",
                "job_url": "https://example.com/jobs/food-product-designer",
            },
            {
                "source_name": "Example Startup Board",
                "source_type": "startup_board",
                "title": "Co-Founder",
                "company_name": "Example Cloud Co",
                "location": "Melbourne, Australia",
                "description": "Build a venture-backed SaaS cloud platform with the founding team.",
                "job_url": "https://example.com/jobs/saas-cofounder",
            },
        ]

        inserted = job_pipeline.insert_matched_jobs(run, raw_jobs)

        self.assertEqual(
            {job.title for job in inserted},
            {"AI Analyst", "Product Designer", "Co-Founder"},
        )

    def test_expanded_target_role_title_families(self):
        for title in (
            "AI Co-Founder",
            "Technical Co-Founder - AI",
            "AI Analyst",
            "Machine Learning Analyst",
            "Product Designer",
            "UI/UX Designer",
        ):
            self.assertTrue(is_target_role_title(title), title)

    @override_settings(
        JOBS_LLM_LOCATION_CHECK_ENABLED=True,
        JOBS_LLM_JUDGE_API_KEY="test-key",
    )
    @patch("jobs.services.location_eligibility.requests.post")
    def test_himalayas_location_classifier_uses_openai_for_ambiguous_worldwide_jobs(self, mock_post):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"status":"restricted_remote",'
                                    '"region":"European Union",'
                                    '"reason":"You can work from any European Union country"}'
                                )
                            }
                        }
                    ]
                }

        mock_post.return_value = FakeResponse()

        result = classify_location_eligibility(
            {
                "source_name": "Himalayas",
                "title": "CTO",
                "company_name": "saas.group",
                "location": "Remote - worldwide",
                "remote_region": "Remote - worldwide",
                "description": "Remote leadership role with unclear source metadata.",
            }
        )

        self.assertEqual(result.status, "restricted_remote")
        self.assertEqual(result.region, "European Union")
        mock_post.assert_called_once()
