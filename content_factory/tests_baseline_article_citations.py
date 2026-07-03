from django.test import TestCase
from django.utils import timezone

from content_factory.models import ArticlePublishStatus, WrittenArticle, WebsiteBaselineSnapshot
from content_factory.vibe_marketing_views import (
    _baseline_published_articles,
    _serialize_baseline_snapshot,
)
from organizations.models import Organization


class BaselinePublishedArticlesTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="acme.com", name="Acme")

    def _article(self, *, slug, status, live_url=None, article_url=None, keyword="topic"):
        return WrittenArticle.objects.create(
            organization=self.organization,
            title=f"Article {slug}",
            slug=slug,
            category="guides",
            publish_status=status,
            live_url=live_url,
            article_url=article_url,
            primary_keyword=keyword,
        )

    def test_only_publicly_reachable_articles_qualify(self):
        self._article(slug="live", status=ArticlePublishStatus.LIVE, live_url="https://acme.com/articles/live", keyword="live kw")
        self._article(slug="merged", status=ArticlePublishStatus.MERGED, article_url="https://acme.com/articles/merged")
        self._article(slug="written", status=ArticlePublishStatus.WRITTEN, article_url="https://acme.com/articles/written")
        self._article(slug="pr-open", status=ArticlePublishStatus.PR_OPEN)

        articles = _baseline_published_articles(self.organization)
        urls = {item["url"] for item in articles}
        self.assertEqual(urls, {"https://acme.com/articles/live", "https://acme.com/articles/merged"})
        live = next(item for item in articles if item["url"].endswith("/live"))
        self.assertEqual(live["title"], "Article live")
        self.assertEqual(live["keyword"], "live kw")

    def test_prefers_live_url_and_dedupes(self):
        self._article(
            slug="both",
            status=ArticlePublishStatus.LIVE,
            live_url="https://acme.com/articles/both",
            article_url="https://acme.com/articles/both-old",
        )
        self._article(slug="dupe", status=ArticlePublishStatus.LIVE, live_url="https://acme.com/articles/both")

        articles = _baseline_published_articles(self.organization)
        self.assertEqual([item["url"] for item in articles], ["https://acme.com/articles/both"])

    def test_caps_article_count(self):
        for index in range(12):
            self._article(
                slug=f"a{index}",
                status=ArticlePublishStatus.LIVE,
                live_url=f"https://acme.com/articles/a{index}",
            )
        self.assertEqual(len(_baseline_published_articles(self.organization, limit=8)), 8)


class CompactKeepsAiInsightFieldsTest(TestCase):
    def test_compact_metric_keeps_citation_insight_fields(self):
        organization = Organization.objects.create(domain="acme.com", name="Acme")
        snapshot = WebsiteBaselineSnapshot.objects.create(
            organization=organization,
            domain="acme.com",
            run_id="run-1",
            status="completed",
            collected_at=timezone.now(),
            overall_score=60,
            metrics={
                "aiVisibility": {
                    "status": "measured",
                    "score": 50,
                    "aiQuotes": [{"provider": "chatgpt", "text": "Acme is recommended.", "kind": "recommended"}],
                    "citedPages": [{"url": "https://acme.com/articles/guide", "citations": 3, "isArticle": True}],
                    "articleCitations": {"status": "measured", "checkedCount": 2, "citedCount": 1, "articles": []},
                    "queries": [{"query": "internal-detail-dropped"}],
                }
            },
            source_status={"aiVisibility": "measured"},
        )
        payload = _serialize_baseline_snapshot(snapshot, compact=True)
        ai = payload["metrics"]["aiVisibility"]
        self.assertEqual(ai["aiQuotes"][0]["kind"], "recommended")
        self.assertEqual(ai["citedPages"][0]["citations"], 3)
        self.assertEqual(ai["articleCitations"]["citedCount"], 1)
        self.assertNotIn("queries", ai)
