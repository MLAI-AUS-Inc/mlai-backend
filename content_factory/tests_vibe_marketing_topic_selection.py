from django.test import TestCase

from content_factory.models import KeywordStatus, OrganizationContentConfig, ResearchedKeyword
from content_factory.vibe_marketing_views import (
    _article_selection_conflicts,
    _is_custom_topic_run,
    _resolve_topic_selection_candidate,
    _topic_candidates_from_runs,
    _topic_selection_candidate_pool,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


class VibeMarketingTopicSelectionTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="MLAI-AUS-Inc/mlai-au",
        )
        ContentFactoryRun.objects.create(
            run_id="run-discovery-topics",
            workflow="auto_discovery",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "topic_candidates": [
                    {
                        "id": "2",
                        "keyword": "tech central sydney",
                        "title": "Tech Central Sydney",
                        "source_run_id": "run-discovery-topics",
                        "opportunityScore": 1000,
                    },
                    {
                        "id": "2",
                        "keyword": "how do ai detectors work",
                        "title": "How Do AI Detectors Work",
                        "source_run_id": "run-discovery-topics",
                        "opportunityScore": 900,
                    },
                ]
            },
        )

    def test_canonical_candidate_id_resolves_clicked_ai_detectors_topic(self):
        candidates = _topic_selection_candidate_pool(self.organization, self.config)
        ai_candidate = next(candidate for candidate in candidates if candidate["keyword"] == "how do ai detectors work")

        resolved = _resolve_topic_selection_candidate(self.organization, self.config, ai_candidate["id"])

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["keyword"], "how do ai detectors work")
        self.assertEqual(resolved["title"], "How Do AI Detectors Work")

    def test_duplicate_raw_candidate_id_fails_closed(self):
        resolved = _resolve_topic_selection_candidate(self.organization, self.config, "2")

        self.assertIsNone(resolved)

    def test_stored_keyword_canonical_candidate_id_resolves(self):
        keyword = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="doctor jobs sydney",
            volume=120,
            difficulty=0,
            opportunity_index=900,
            status=KeywordStatus.PENDING,
        )
        candidates = _topic_selection_candidate_pool(self.organization, self.config)
        stored_candidate = next(candidate for candidate in candidates if candidate["keyword"] == "doctor jobs sydney")

        self.assertEqual(stored_candidate["id"], f"topic:keyword{keyword.id}:doctor-jobs-sydney")
        resolved = _resolve_topic_selection_candidate(self.organization, self.config, stored_candidate["id"])

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["keyword"], "doctor jobs sydney")

    def test_frontend_hyphenated_stored_keyword_candidate_id_resolves_from_submission(self):
        keyword = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="doctor jobs sydney",
            volume=120,
            difficulty=0,
            opportunity_index=900,
            status=KeywordStatus.PENDING,
        )
        legacy_frontend_id = f"topic:keyword-{keyword.id}:doctor-jobs-sydney"

        resolved = _resolve_topic_selection_candidate(
            self.organization,
            self.config,
            legacy_frontend_id,
            submitted={
                "selected_title": "doctor jobs sydney",
                "target_keyword": "doctor jobs sydney",
            },
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], f"topic:keyword{keyword.id}:doctor-jobs-sydney")
        self.assertEqual(resolved["keyword"], "doctor jobs sydney")

    def test_stale_client_keyword_conflicts_with_resolved_candidate(self):
        candidates = _topic_selection_candidate_pool(self.organization, self.config)
        ai_candidate = next(candidate for candidate in candidates if candidate["keyword"] == "how do ai detectors work")

        conflicts = _article_selection_conflicts(
            submitted={
                "topic": "Tech Central Sydney",
                "selected_title": "Tech Central Sydney",
                "custom_title": "Tech Central Sydney",
                "target_keyword": "tech central sydney",
                "source_run_id": ai_candidate["sourceRunId"],
            },
            resolved={
                "topic": ai_candidate["title"],
                "selected_title": ai_candidate["title"],
                "custom_title": ai_candidate["title"],
                "target_keyword": ai_candidate["keyword"],
                "source_run_id": ai_candidate["sourceRunId"],
            },
        )

        self.assertIn("target_keyword", conflicts)
        self.assertEqual(conflicts["target_keyword"]["resolved"], "how do ai detectors work")


class CustomTopicResearchFallbackTest(TestCase):
    """When a user runs custom-idea research and every result falls below the
    dashboard quality bar, the single strongest candidate is still surfaced so the
    idea always yields a result (instead of an empty list)."""

    WEAK_CANDIDATES = [
        {"keyword": "obscure idea a", "title": "Obscure Idea A", "difficulty": 90, "volume": 5, "opportunityScore": 10},
        {"keyword": "obscure idea b", "title": "Obscure Idea B", "difficulty": 80, "volume": 10, "opportunityScore": 40},
    ]

    def _make_run(self, run_id, run_request):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="auto_discovery",
            domain="mlai.au",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            run_request=run_request,
            result={"topic_candidates": list(self.WEAK_CANDIDATES)},
        )

    def test_is_custom_topic_run_detects_free_form_idea(self):
        custom = self._make_run("run-custom", {"custom_topic_title": "obscure idea"})
        island = self._make_run("run-island", {"content_island_slug": "ai-community", "content_island_name": "AI"})
        daily = self._make_run("run-daily", {})
        self.assertTrue(_is_custom_topic_run(custom))
        self.assertFalse(_is_custom_topic_run(island))
        self.assertFalse(_is_custom_topic_run(daily))

    def test_custom_run_keeps_single_best_when_all_below_quality_bar(self):
        run = self._make_run("run-custom-weak", {"custom_topic_title": "obscure idea"})
        candidates = _topic_candidates_from_runs([run])
        self.assertEqual(len(candidates), 1)
        # Best by sort key: highest opportunityScore (40 > 10).
        self.assertEqual(candidates[0]["keyword"], "obscure idea b")

    def test_non_custom_run_still_drops_all_below_quality_bar(self):
        run = self._make_run("run-island-weak", {"content_island_slug": "ai-community", "content_island_name": "AI"})
        self.assertEqual(_topic_candidates_from_runs([run]), [])
