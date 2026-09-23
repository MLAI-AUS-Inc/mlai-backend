"""Database-free regression checks for Vibe Marketing bootstrap work reuse."""

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
from django.apps import apps
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import vibe_marketing_views as views


class VibeMarketingBootstrapPerformanceTests(SimpleTestCase):
    def test_multiple_runs_reuse_one_candidate_pass_and_one_serialization_each(self):
        organization = SimpleNamespace(
            id=1, name="Example", domain="example.test", company_linkedin_url="",
            competitors=[], seed_keywords=[],
        )
        company = SimpleNamespace(
            id=2, name="Example", domain="example.test", location="", abn="", avatar_url="",
        )
        context = SimpleNamespace(
            organization=organization, company=company,
            profile=SimpleNamespace(user=SimpleNamespace(id=3)),
        )
        config = SimpleNamespace(
            brand_name="", company_context="", github_repo="", daily_discovery_enabled=False,
            daily_discovery_priority="", default_timezone="UTC", github_connection_state="",
            authors=[], default_author_id=None,
        )
        now = datetime.now(timezone.utc)

        def run(workflow, run_id):
            return SimpleNamespace(
                workflow=workflow, run_id=run_id, domain="example.test", github_repo="",
                status="completed", current_step="", approval_state="", resume_available=False,
                created_at=now, updated_at=now, step_order=[], acceptance_summary={},
                result={}, error="",
            )

        runs = [run("auto_discovery", "first"), run("auto_discovery", "second"), run("article", "third")]
        candidates = [{"keyword": "example"}]
        candidate_calculation = Mock(
            side_effect=lambda *args, **kwargs: [] if kwargs.get("include_written") else candidates
        )
        progress = Mock(return_value={})
        stubs = {
            "_get_config": config,
            "_latest_runs_for_org": runs,
            "_recent_discovery_topic_runs_for_org": runs,
            "list_topic_feedback": [],
            "build_topic_coverage_memory": {},
            "_written_topic_memory": {},
            "_topic_candidates_from_runs": candidate_calculation,
            "_canonicalize_topic_candidate_ids": lambda items, namespace: items,
            "_topic_pillars_for_bootstrap": [],
            "_canonicalize_topic_pillars": [],
            "_island_graph_for_bootstrap": None,
            "_latest_baseline_snapshot": None,
            "_profile_checks": {"scaffold": {"generationReady": False}},
            "_article_setup_state_for_config": {},
            "_guided_steps": ([], None),
            "_latest_run_matching": None,
            "google_baseline_connection_status": {},
            "_google_baseline_connect_url": "",
            "_has_completed_article_flow": False,
            "_bootstrap_topic_picker_ready": False,
            "_serialize_startup_profile": {},
            "_serialize_baseline_snapshot": {},
            "_stored_article_delivery_mode": None,
            "_effective_article_delivery_mode": "content_only",
            "_pending_article_system_setup_from_config": None,
            "normalize_authors": [],
            "_recent_article_drafts": [],
            "_recent_written_topics": [],
            "_publish_evidence_from_run": {},
            "_workflow_progress": progress,
            "_serialize_run_steps": [],
            "_run_blocking_detail": {"reason": "", "code": ""},
            "_humanized_run_failure_message": None,
            "_live_preview_from_run": {},
            "_article_setup_state": {},
            "_scan_progress_payloads": ({}, {}),
            "_run_content_island_payload": None,
            "_run_source_run_id": "",
            "_article_restart_available": False,
            "_compact_result_for_run": {},
        }
        with ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(patch.object(
                    views, name, value if callable(value) else Mock(return_value=value)
                ))
            payload = views._compute_bootstrap_payload(context, view="summary")

        self.assertEqual(candidate_calculation.call_count, 2)  # available + hidden
        self.assertEqual(progress.call_count, len(runs) + 1)  # each run + bootstrap
        self.assertTrue(all(
            call.kwargs.get("topic_candidates") is candidates for call in progress.call_args_list
        ))
        self.assertEqual(len(payload["latestRuns"]), len(runs))
        self.assertIs(payload["latestRunsByWorkflow"]["auto_discovery"], payload["latestRuns"][0])

    def test_precomputed_candidates_bypass_progress_recalculation(self):
        checks = {"research": {"passed": True}}
        with patch.object(views, "_workflow_progress_context", return_value=(None, None, [], checks)), patch.object(
            views, "_topic_candidates_from_runs", side_effect=AssertionError("recomputed")
        ):
            progress = views._workflow_progress(
                checks=checks, topic_candidates=[{"keyword": "example"}]
            )
        choose_topic = next(step for step in progress["steps"] if step["id"] == "choose_topic")
        self.assertEqual(choose_topic["status"], "needs_action")

