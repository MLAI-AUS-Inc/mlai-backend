import os

from django.conf import settings
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import ResearchedKeyword
from content_factory.vibe_marketing_views import _topic_candidate_from_keyword
from organizations.models import Organization


class TopicDifficultyMetricsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test-topic-difficulty-key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.org = Organization.objects.create(name="Acme", domain="acme.example.com")

    def test_bulk_upsert_stores_verified_difficulty_source(self):
        response = self.client.post(
            "/api/seo/keywords/bulk/",
            {
                "domain": self.org.domain,
                "keywords": [
                    {
                        "keyword": "startup equity",
                        "volume": 320,
                        "difficulty": 31,
                        "difficulty_source": "dataforseo_bulk",
                        "related_keywords": ["startup equity guide"],
                        "monthly_searches": [120, 160, 220, 320],
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        keyword = ResearchedKeyword.objects.get(organization=self.org, keyword_normalized="startup equity")
        self.assertEqual(keyword.difficulty, 31)
        self.assertEqual(keyword.difficulty_source, "dataforseo_bulk")
        self.assertEqual(keyword.related_keywords, ["startup equity guide"])
        self.assertEqual(keyword.monthly_searches, [120, 160, 220, 320])

    def test_bulk_upsert_marks_missing_source_as_legacy_default(self):
        response = self.client.post(
            "/api/seo/keywords/bulk/",
            {
                "domain": self.org.domain,
                "keywords": [{"keyword": "legacy topic", "volume": 100, "difficulty": 50}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        keyword = ResearchedKeyword.objects.get(organization=self.org, keyword_normalized="legacy topic")
        self.assertEqual(keyword.difficulty_source, "legacy_default")

    def test_topic_candidate_marks_legacy_default_difficulty_as_pending(self):
        keyword = ResearchedKeyword.objects.create(
            organization=self.org,
            keyword="legacy difficulty",
            keyword_normalized="legacy difficulty",
            volume=100,
            difficulty=50,
            difficulty_source="legacy_default",
        )

        candidate = _topic_candidate_from_keyword(keyword)

        self.assertEqual(candidate["difficultySource"], "legacy_default")
        self.assertIn("difficulty pending", candidate["reason"])
        self.assertNotIn("difficulty 50/100", candidate["reason"])

    def test_topic_candidate_keeps_verified_true_50_difficulty(self):
        keyword = ResearchedKeyword.objects.create(
            organization=self.org,
            keyword="verified difficulty",
            keyword_normalized="verified difficulty",
            volume=100,
            difficulty=50,
            difficulty_source="dataforseo_labs",
            related_keywords=["verified difficulty examples"],
            monthly_searches=[80, 90, 100],
        )

        candidate = _topic_candidate_from_keyword(keyword)

        self.assertEqual(candidate["difficultySource"], "dataforseo_labs")
        self.assertIn("difficulty 50/100", candidate["reason"])
        self.assertEqual(candidate["relatedKeywords"], ["verified difficulty examples"])
        self.assertEqual(candidate["monthlySearches"], [80, 90, 100])
