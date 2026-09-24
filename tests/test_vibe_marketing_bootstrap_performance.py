"""Database-free regression checks for Vibe Marketing bootstrap work reuse."""

from contextlib import ExitStack
from datetime import datetime, timezone
from difflib import SequenceMatcher
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
from django.apps import apps
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import topic_coverage, vibe_marketing_views as views


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
                result={}, error="", verification_summary={},
            )

        runs = [
            run("auto_discovery", "first"),
            run("auto_discovery", "second"),
            run("article", "third"),
            run("repo_scan", "fourth"),
            run("article_system_setup", "fifth"),
        ]
        candidates = [{"keyword": "example"}]
        candidate_calculation = Mock(
            side_effect=lambda *args, **kwargs: [] if kwargs.get("include_written") else candidates
        )
        progress = Mock(return_value={})
        bootstrap_setup_state = Mock(return_value={"source": "bootstrap"})
        run_setup_state = Mock(side_effect=lambda **kwargs: {"source": kwargs["run"].workflow})
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
            "_article_setup_state_for_config": bootstrap_setup_state,
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
            "_article_setup_state": run_setup_state,
            "_scan_progress_payloads": ({}, {}),
            "_run_content_island_payload": None,
            "_run_source_run_id": "",
            "_article_restart_available": False,
            "_compact_result_for_run": {},
            "_content_package_from_run": {},
            "_component_manifest_from_run": {},
            "_component_feedback_from_run": {},
        }
        with ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(patch.object(
                    views, name, value if callable(value) else Mock(return_value=value)
                ))
            payload = views._compute_bootstrap_payload(context, view="summary")
            standalone = views._serialize_run(runs[0], context=context, mode="full")

        self.assertEqual(candidate_calculation.call_count, 2)  # available + hidden
        self.assertEqual(progress.call_count, len(runs) + 2)  # each run + bootstrap + standalone
        self.assertTrue(all(
            call.kwargs.get("topic_candidates") is candidates for call in progress.call_args_list
            if call.kwargs.get("topic_candidates") is not None
        ))
        bootstrap_setup_state.assert_called_once()
        self.assertEqual(run_setup_state.call_count, 3)  # scan, setup, standalone run
        self.assertEqual(
            [call.kwargs["run"].workflow for call in run_setup_state.call_args_list],
            ["repo_scan", "article_system_setup", "auto_discovery"],
        )
        self.assertTrue(all(
            item["articleSetupState"] == {"source": "bootstrap"}
            for item in payload["latestRuns"][:3]
        ))
        self.assertIsNot(
            payload["latestRuns"][0]["articleSetupState"],
            payload["latestRuns"][1]["articleSetupState"],
        )
        self.assertEqual(payload["latestRuns"][3]["articleSetupState"], {"source": "repo_scan"})
        self.assertEqual(payload["latestRuns"][4]["articleSetupState"], {"source": "article_system_setup"})
        self.assertEqual(standalone["articleSetupState"], {"source": "auto_discovery"})
        self.assertEqual(len(payload["latestRuns"]), len(runs))
        self.assertIs(payload["latestRunsByWorkflow"]["auto_discovery"], payload["latestRuns"][0])

    def test_precomputed_candidates_bypass_progress_recalculation(self):
        checks = {"research": {"passed": True}}
        with patch.object(views, "_workflow_progress_context", return_value=(None, None, [], checks)), patch.object(
            views, "_topic_candidates_from_runs", side_effect=AssertionError("recomputed")
        ):
            for visible_candidates, expected_status in (
                ([{"keyword": "example"}], "needs_action"),
                ([], "ready"),
            ):
                with self.subTest(expected_status=expected_status):
                    progress = views._workflow_progress(
                        checks=checks, topic_candidates=visible_candidates
                    )
                    choose_topic = next(
                        step for step in progress["steps"] if step["id"] == "choose_topic"
                    )
                    self.assertEqual(choose_topic["status"], expected_status)

    def test_topic_coverage_snapshot_reuses_positive_and_negative_matches(self):
        record = topic_coverage._record_for_text(
            text="AI assistant for small business",
            source="written_article",
            reason="written_article",
        )
        memory = {
            "records": [record],
            "exact": {record.normalized: record},
            "slugs": {record.slug: record},
            "_match_cache": {},
        }
        with patch.object(
            topic_coverage, "_close_topic_match", wraps=topic_coverage._close_topic_match
        ) as compare:
            first = topic_coverage.match_covered_topic(
                keyword="small business AI assistant", memory=memory
            )
            repeated = topic_coverage.match_covered_topic(
                keyword="Small-business artificial intelligence assistant", memory=memory
            )
            self.assertEqual(compare.call_count, 1)
            self.assertEqual(first.match_type, "lexical_variant")
            self.assertIs(first.record, repeated.record)
            self.assertIsNot(first, repeated)

            self.assertIsNone(topic_coverage.match_covered_topic(keyword="unrelated topic", memory=memory))
            comparisons_after_miss = compare.call_count
            self.assertIsNone(topic_coverage.match_covered_topic(keyword="Unrelated-topic", memory=memory))
            self.assertEqual(compare.call_count, comparisons_after_miss)

        other_memory = {"records": [], "exact": {}, "slugs": {}, "_match_cache": {}}
        self.assertIsNone(topic_coverage.match_covered_topic(
            keyword="small business AI assistant", memory=other_memory
        ))

    def test_lexical_upper_bounds_skip_full_ratio_without_losing_close_variants(self):
        class CountingMatcher(SequenceMatcher):
            ratio_calls = 0

            def ratio(self):
                type(self).ratio_calls += 1
                return super().ratio()

        cases = (
            ("alpha beta gamma", "alpha beta delta epsilon zeta eta theta iota kappa", False),
            ("alpha beta delta gamma", "alpha beta zeta theta", False),
            ("business automation workflow", "business automation workflows", True),
        )
        with patch.object(topic_coverage, "SequenceMatcher", CountingMatcher):
            for candidate, existing, expected_match in cases:
                with self.subTest(candidate=candidate):
                    CountingMatcher.ratio_calls = 0
                    record = topic_coverage._record_for_text(
                        text=existing, source="written_article", reason="written_article"
                    )
                    match = topic_coverage._close_topic_match(
                        candidate, topic_coverage.topic_content_tokens(candidate), record
                    )
                    if expected_match:
                        self.assertGreaterEqual(match, 0.9)
                        self.assertEqual(CountingMatcher.ratio_calls, 1)
                    else:
                        self.assertIsNone(match)
                        self.assertEqual(CountingMatcher.ratio_calls, 0)

            near_record = topic_coverage._record_for_text(
                text="business automation workflows",
                source="written_article", reason="written_article",
            )
            memory = {
                "records": [near_record],
                "exact": {near_record.normalized: near_record},
                "slugs": {near_record.slug: near_record},
            }
            covered = topic_coverage.match_covered_topic(
                keyword="business automation workflow", memory=memory
            )
            self.assertEqual(covered.match_type, "lexical_variant")
            self.assertIs(covered.record, near_record)

    def test_precomputed_coverage_result_avoids_repeating_topic_match(self):
        keyword = SimpleNamespace(
            keyword="small business AI assistant", status="pending",
            written_article_id=None, cooldown_until=None,
        )
        with patch.object(views, "match_covered_topic", side_effect=AssertionError("repeated")):
            self.assertFalse(views._keyword_is_available_for_topic_picker(
                keyword, coverage_memory={"records": []}, coverage_match=object()
            ))
            self.assertTrue(views._keyword_is_available_for_topic_picker(
                keyword, coverage_memory={"records": []}, coverage_match=None
            ))
        with patch.object(views, "match_covered_topic", return_value=None) as matcher:
            self.assertTrue(views._keyword_is_available_for_topic_picker(
                keyword, coverage_memory={"records": []}
            ))
            matcher.assert_called_once()
