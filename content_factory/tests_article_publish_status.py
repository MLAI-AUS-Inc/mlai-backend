from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from content_factory.article_publish_status import (
    advance_publish_status,
    derive_publish_status_from_evidence,
    refresh_publish_statuses,
)
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from content_factory.vibe_marketing_views import (
    _apply_publish_child_evidence_to_article,
    _persist_article_memory_from_run,
    _serialize_written_article,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://mlai.au/articles/featured/live-article</loc></url>
  <url><loc>https://mlai.au/about</loc></url>
</urlset>"""


def _mock_response(status_code=200, content=b"", json_payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = json_payload if json_payload is not None else {}
    return response


class DerivePublishStatusFromEvidenceTest(TestCase):
    def test_no_evidence_means_written(self):
        self.assertEqual(derive_publish_status_from_evidence({}), ArticlePublishStatus.WRITTEN)
        self.assertEqual(derive_publish_status_from_evidence(None), ArticlePublishStatus.WRITTEN)

    def test_pr_url_means_pr_open(self):
        evidence = {"prUrl": "https://github.com/o/r/pull/1"}
        self.assertEqual(derive_publish_status_from_evidence(evidence), ArticlePublishStatus.PR_OPEN)

    def test_merge_status_merged_wins(self):
        evidence = {"prUrl": "https://github.com/o/r/pull/1", "mergeStatus": "merged"}
        self.assertEqual(derive_publish_status_from_evidence(evidence), ArticlePublishStatus.MERGED)

    def test_closed_pr_maps_to_pr_closed(self):
        evidence = {"prUrl": "https://github.com/o/r/pull/1", "mergeStatus": "closed"}
        self.assertEqual(derive_publish_status_from_evidence(evidence), ArticlePublishStatus.PR_CLOSED)


class AdvancePublishStatusTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        self.article = WrittenArticle.objects.create(
            organization=self.organization,
            title="Article",
            slug="article",
            category="featured",
            primary_keyword="article",
        )

    def test_advances_forward(self):
        changed = advance_publish_status(self.article, ArticlePublishStatus.PR_OPEN)
        self.assertIn("publish_status", changed)
        self.assertEqual(self.article.publish_status, ArticlePublishStatus.PR_OPEN)

    def test_never_downgrades_from_live(self):
        self.article.publish_status = ArticlePublishStatus.LIVE
        changed = advance_publish_status(self.article, ArticlePublishStatus.WRITTEN)
        self.assertNotIn("publish_status", changed)
        self.assertEqual(self.article.publish_status, ArticlePublishStatus.LIVE)

    def test_pr_open_and_pr_closed_flip_both_ways(self):
        self.article.publish_status = ArticlePublishStatus.PR_OPEN
        advance_publish_status(self.article, ArticlePublishStatus.PR_CLOSED)
        self.assertEqual(self.article.publish_status, ArticlePublishStatus.PR_CLOSED)
        advance_publish_status(self.article, ArticlePublishStatus.PR_OPEN)
        self.assertEqual(self.article.publish_status, ArticlePublishStatus.PR_OPEN)

    def test_merged_records_timestamp_once(self):
        merged_at = timezone.now()
        advance_publish_status(self.article, ArticlePublishStatus.MERGED, pr_merged_at=merged_at)
        self.assertEqual(self.article.pr_merged_at, merged_at)
        later = timezone.now()
        advance_publish_status(self.article, ArticlePublishStatus.MERGED, pr_merged_at=later)
        self.assertEqual(self.article.pr_merged_at, merged_at)

    def test_live_sets_verification_fields(self):
        changed = advance_publish_status(
            self.article,
            ArticlePublishStatus.LIVE,
            live_url="https://mlai.au/articles/featured/article",
        )
        self.assertIn("live_verified_at", changed)
        self.assertEqual(self.article.live_url, "https://mlai.au/articles/featured/article")


class PersistArticleMemoryStatusTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def _completed_run(self, run_id, result):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="article_generation",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result=result,
        )

    def test_completed_run_without_pr_is_written_not_published(self):
        run = self._completed_run(
            "run-no-pr",
            {"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        self.assertIsNotNone(article)
        self.assertEqual(article.publish_status, ArticlePublishStatus.WRITTEN)
        self.assertIsNotNone(article.published_at)
        self.assertEqual(article.pr_url, "")

    def test_completed_run_with_pr_evidence_is_pr_open(self):
        run = self._completed_run(
            "run-with-pr",
            {
                "delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"},
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
                "pr_number": 990,
            },
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        self.assertEqual(article.publish_status, ArticlePublishStatus.PR_OPEN)
        self.assertEqual(article.pr_url, "https://github.com/MLAI-AUS-Inc/mlai-au/pull/990")
        self.assertEqual(article.pr_number, 990)

    def test_evidence_less_repersist_keeps_status_and_urls(self):
        run_with_pr = self._completed_run(
            "run-with-pr",
            {
                "delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"},
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            },
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run_with_pr)
        article.publish_status = ArticlePublishStatus.LIVE
        article.save(update_fields=["publish_status"])

        run_revision = self._completed_run(
            "run-revision",
            {"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run_revision)
        self.assertEqual(article.publish_status, ArticlePublishStatus.LIVE)
        self.assertEqual(article.pr_url, "https://github.com/MLAI-AUS-Inc/mlai-au/pull/990")

    def test_publish_child_evidence_lands_on_source_article(self):
        source_run = self._completed_run(
            "run-source",
            {"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=source_run)
        self.assertEqual(article.publish_status, ArticlePublishStatus.WRITTEN)

        child_run = ContentFactoryRun.objects.create(
            run_id="run-source-publish",
            workflow="article_generation",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={"pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/991", "pr_number": 991},
        )
        updated = _apply_publish_child_evidence_to_article(self.organization, source_run, child_run)
        self.assertIsNotNone(updated)
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.PR_OPEN)
        self.assertEqual(article.pr_url, "https://github.com/MLAI-AUS-Inc/mlai-au/pull/991")
        self.assertEqual(article.pr_number, 991)

    def test_serializer_exposes_publish_status(self):
        run = self._completed_run(
            "run-serialize",
            {"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        payload = _serialize_written_article(article)
        self.assertEqual(payload["publishStatus"], ArticlePublishStatus.WRITTEN)
        self.assertEqual(payload["liveUrl"], "")
        self.assertIn("prNumber", payload)


class RefreshPublishStatusesTest(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="MLAI-AUS-Inc/mlai-au",
            github_installation_id="12345",
        )

    def _article(self, slug, **overrides):
        fields = {
            "organization": self.organization,
            "title": slug,
            "slug": slug,
            "category": "featured",
            "primary_keyword": slug,
        }
        fields.update(overrides)
        return WrittenArticle.objects.create(**fields)

    def test_sitemap_match_marks_article_live(self):
        article = self._article("live-article")
        with mock.patch("content_factory.article_publish_status.http_requests.get") as get:
            get.return_value = _mock_response(200, content=SITEMAP_XML)
            refreshed = refresh_publish_statuses(self.organization, self.config)
        article.refresh_from_db()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(article.publish_status, ArticlePublishStatus.LIVE)
        self.assertEqual(article.live_url, "https://mlai.au/articles/featured/live-article")
        self.assertIsNotNone(article.live_verified_at)
        self.assertIsNotNone(article.live_checked_at)

    def test_open_pr_confirmed_merged_via_github(self):
        article = self._article(
            "pending-article",
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            publish_status=ArticlePublishStatus.PR_OPEN,
        )

        def fake_get(url, **kwargs):
            if "sitemap.xml" in url:
                return _mock_response(200, content=SITEMAP_XML)
            if "api.github.com" in url:
                return _mock_response(
                    200,
                    json_payload={"merged": True, "merged_at": "2026-06-09T01:02:03Z", "state": "closed"},
                )
            raise AssertionError(f"unexpected URL {url}")

        token = mock.Mock()
        token.token = "ghs_test"
        with mock.patch("content_factory.article_publish_status.http_requests.get", side_effect=fake_get):
            with mock.patch(
                "content_factory.article_publish_status.create_installation_access_token",
                return_value=token,
            ):
                refresh_publish_statuses(self.organization, self.config)
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNotNone(article.pr_merged_at)
        self.assertEqual(article.pr_number, 990)

    def test_recently_checked_articles_are_throttled(self):
        self._article("fresh-article", live_checked_at=timezone.now())
        with mock.patch("content_factory.article_publish_status.http_requests.get") as get:
            refreshed = refresh_publish_statuses(self.organization, self.config)
        self.assertEqual(refreshed, [])
        get.assert_not_called()

    def test_live_articles_are_not_rechecked(self):
        self._article("done-article", publish_status=ArticlePublishStatus.LIVE)
        with mock.patch("content_factory.article_publish_status.http_requests.get") as get:
            refreshed = refresh_publish_statuses(self.organization, self.config)
        self.assertEqual(refreshed, [])
        get.assert_not_called()

    def test_sitemap_failure_is_best_effort(self):
        article = self._article("unreachable-article")
        with mock.patch(
            "content_factory.article_publish_status.http_requests.get",
            side_effect=Exception("boom"),
        ):
            refreshed = refresh_publish_statuses(self.organization, self.config)
        article.refresh_from_db()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(article.publish_status, ArticlePublishStatus.WRITTEN)
        self.assertIsNotNone(article.live_checked_at)
