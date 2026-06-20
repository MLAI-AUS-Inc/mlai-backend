from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from content_factory.article_publish_status import (
    advance_publish_status,
    article_bucket,
    derive_publish_status_from_evidence,
    refresh_publish_statuses,
)
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from content_factory.vibe_marketing_views import (
    _apply_publish_child_evidence_to_article,
    _article_publish_attempts,
    _deterministic_publish_child_run_id,
    _persist_article_memory_from_run,
    _recent_written_topics,
    _serialize_article_draft,
    _serialize_written_article,
    _written_article_identity_keys,
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


class ArticleBucketTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def _article(self, **overrides):
        fields = {
            "organization": self.organization,
            "title": "Article",
            "slug": "article",
            "category": "featured",
            "primary_keyword": "article",
        }
        fields.update(overrides)
        return WrittenArticle.objects.create(**fields)

    def test_written_article_is_publishing(self):
        self.assertEqual(article_bucket(self._article(publish_status=ArticlePublishStatus.WRITTEN)), "publishing")

    def test_merged_without_on_main_is_publishing(self):
        self.assertEqual(article_bucket(self._article(publish_status=ArticlePublishStatus.MERGED)), "publishing")

    def test_on_main_verified_is_published(self):
        article = self._article(publish_status=ArticlePublishStatus.MERGED, on_main_verified_at=timezone.now())
        self.assertEqual(article_bucket(article), "published")

    def test_legacy_sitemap_live_is_published(self):
        self.assertEqual(article_bucket(self._article(publish_status=ArticlePublishStatus.LIVE)), "published")


class OnMainVerificationTest(TestCase):
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

    def _run_refresh(self, payload):
        def fake_get(url, **kwargs):
            if "sitemap.xml" in url:
                return _mock_response(200, content=SITEMAP_XML)
            if "api.github.com" in url:
                return _mock_response(200, json_payload=payload)
            raise AssertionError(f"unexpected URL {url}")

        token = mock.Mock()
        token.token = "ghs_test"
        with mock.patch("content_factory.article_publish_status.http_requests.get", side_effect=fake_get):
            with mock.patch(
                "content_factory.article_publish_status.create_installation_access_token",
                return_value=token,
            ):
                refresh_publish_statuses(self.organization, self.config)

    def test_merge_into_main_sets_on_main_verified(self):
        article = self._article(
            "pending-article",
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            publish_status=ArticlePublishStatus.PR_OPEN,
        )
        self._run_refresh(
            {
                "merged": True,
                "merged_at": "2026-06-09T01:02:03Z",
                "state": "closed",
                "merge_commit_sha": "abc123",
                "base": {"ref": "main"},
            }
        )
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNotNone(article.on_main_verified_at)
        self.assertEqual(article.on_main_commit_sha, "abc123")
        self.assertEqual(article.merge_commit_sha, "abc123")
        self.assertEqual(article_bucket(article), "published")

    def test_merge_into_non_default_branch_does_not_verify_on_main(self):
        article = self._article(
            "feature-branch-article",
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/991",
            publish_status=ArticlePublishStatus.PR_OPEN,
        )
        self._run_refresh(
            {
                "merged": True,
                "merged_at": "2026-06-09T01:02:03Z",
                "state": "closed",
                "merge_commit_sha": "def456",
                "base": {"ref": "staging"},
            }
        )
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNone(article.on_main_verified_at)
        # Merge commit is still captured as evidence even off the default branch.
        self.assertEqual(article.merge_commit_sha, "def456")
        self.assertEqual(article_bucket(article), "publishing")

    def test_on_main_verified_article_is_not_repolled(self):
        self._article(
            "done-article",
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/992",
            publish_status=ArticlePublishStatus.MERGED,
            on_main_verified_at=timezone.now(),
        )
        with mock.patch("content_factory.article_publish_status.http_requests.get") as get:
            get.return_value = _mock_response(200, content=SITEMAP_XML)
            refresh_publish_statuses(self.organization, self.config)
        # Sitemap may be fetched once, but the PR must not be polled again.
        for call in get.call_args_list:
            self.assertNotIn("api.github.com", call.args[0])


class WrittenArticleSerializerBucketTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def test_serializer_exposes_bucket_and_on_main_facts(self):
        article = WrittenArticle.objects.create(
            organization=self.organization,
            title="Article",
            slug="article",
            category="featured",
            primary_keyword="article",
            publish_status=ArticlePublishStatus.MERGED,
            on_main_verified_at=timezone.now(),
            merge_commit_sha="abc123",
        )
        payload = _serialize_written_article(article)
        self.assertEqual(payload["bucket"], "published")
        self.assertTrue(payload["onMain"])
        self.assertIsNotNone(payload["onMainAt"])
        self.assertEqual(payload["mergeCommitSha"], "abc123")


class ContentPathCaptureTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def test_content_path_captured_from_changed_files(self):
        run = ContentFactoryRun.objects.create(
            run_id="run-content-path",
            workflow="article_generation",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"},
                "changed_files": ["src/content/articles/my-article.mdx", "src/registry.ts"],
            },
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        self.assertEqual(article.content_path, "src/content/articles/my-article.mdx")


class ArticleDraftMutualExclusionTest(TestCase):
    """Phase 1: a topic with a WrittenArticle must not also surface as a draft."""

    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        WrittenArticle.objects.create(
            organization=self.organization,
            title="My Article",
            slug="my-article",
            category="featured",
            primary_keyword="my keyword",
        )
        self.written_keys = _written_article_identity_keys(self.organization)

    def _run(self, run_id, status):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="direct_generate",
            domain="mlai.au",
            status=status,
            result={"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )

    def test_failed_publish_run_matching_article_is_hidden(self):
        run = self._run("run-failed", ContentFactoryRunStatus.FAILED)
        self.assertIsNone(_serialize_article_draft(run, written_keys=self.written_keys))

    def test_awaiting_approval_run_matching_article_is_hidden(self):
        run = self._run("run-awaiting", ContentFactoryRunStatus.AWAITING_APPROVAL)
        self.assertIsNone(_serialize_article_draft(run, written_keys=self.written_keys))

    def test_running_run_matching_article_stays_visible(self):
        run = self._run("run-running", ContentFactoryRunStatus.RUNNING)
        self.assertIsNotNone(_serialize_article_draft(run, written_keys=self.written_keys))

    def test_unmatched_failed_draft_stays_visible(self):
        run = ContentFactoryRun.objects.create(
            run_id="run-other",
            workflow="direct_generate",
            domain="mlai.au",
            status=ContentFactoryRunStatus.FAILED,
            result={
                "delivery_package": {
                    "title": "Totally Different",
                    "slug": "totally-different",
                    "target_keyword": "different keyword",
                }
            },
        )
        self.assertIsNotNone(_serialize_article_draft(run, written_keys=self.written_keys))


class ArticleStateReportCommandTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def test_report_counts_buckets_and_flags_divergences(self):
        WrittenArticle.objects.create(
            organization=self.organization, title="Live", slug="live", category="featured",
            primary_keyword="live", publish_status=ArticlePublishStatus.LIVE,
        )
        WrittenArticle.objects.create(
            organization=self.organization, title="OnMain", slug="on-main", category="featured",
            primary_keyword="on main", publish_status=ArticlePublishStatus.MERGED,
            on_main_verified_at=timezone.now(),
        )
        WrittenArticle.objects.create(
            organization=self.organization, title="Pending", slug="pending", category="featured",
            primary_keyword="pending", publish_status=ArticlePublishStatus.PR_OPEN,
            pr_url="https://github.com/o/r/pull/5",
        )
        out = StringIO()
        call_command("article_state_report", "--domain", "mlai.au", stdout=out)
        text = out.getvalue()
        self.assertIn("published (on main / live): 2", text)
        self.assertIn("publishing (not yet on main): 1", text)
        self.assertIn("LIVE_UNVERIFIED_ON_MAIN", text)
        self.assertIn("PR_NUMBER_MISSING", text)

    def test_report_detects_ghost_drafts(self):
        WrittenArticle.objects.create(
            organization=self.organization, title="My Article", slug="my-article", category="featured",
            primary_keyword="my keyword",
        )
        ContentFactoryRun.objects.create(
            run_id="ghost", workflow="direct_generate", domain="mlai.au",
            status=ContentFactoryRunStatus.FAILED,
            result={"delivery_package": {"title": "My Article", "slug": "my-article", "target_keyword": "my keyword"}},
        )
        out = StringIO()
        call_command("article_state_report", "--domain", "mlai.au", "--ghost-drafts", stdout=out)
        self.assertIn("ghost drafts: 1", out.getvalue())


class ArticlePublishAttemptTest(TestCase):
    """Phase 2: surface a stuck/failed publish child on the publishing card."""

    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def _article(self, slug="a", source_run_id="src-1", **overrides):
        fields = {
            "organization": self.organization,
            "title": slug,
            "slug": slug,
            "category": "featured",
            "primary_keyword": slug,
            "source_run_id": source_run_id,
        }
        fields.update(overrides)
        return WrittenArticle.objects.create(**fields)

    def _child(self, source_run_id, status, result=None):
        return ContentFactoryRun.objects.create(
            run_id=_deterministic_publish_child_run_id(source_run_id),
            workflow="article_generation",
            domain="mlai.au",
            status=status,
            result=result or {},
            run_request={"source_run_id": source_run_id, "delivery_mode": "publish_code"},
        )

    def test_no_child_means_no_attempt(self):
        article = self._article()
        self.assertEqual(_article_publish_attempts([article]), {})

    def test_article_without_source_run_is_skipped(self):
        article = self._article(source_run_id="")
        self.assertEqual(_article_publish_attempts([article]), {})

    def test_failed_child_is_recoverable_failure(self):
        article = self._article()
        self._child("src-1", ContentFactoryRunStatus.FAILED)
        attempt = _article_publish_attempts([article])[article.id]
        self.assertEqual(attempt["state"], "failed")
        self.assertTrue(attempt["recoverable"])

    def test_running_child_is_in_progress(self):
        article = self._article()
        self._child("src-1", ContentFactoryRunStatus.RUNNING)
        self.assertEqual(_article_publish_attempts([article])[article.id]["state"], "in_progress")

    def test_awaiting_approval_child_needs_approval(self):
        article = self._article()
        self._child("src-1", ContentFactoryRunStatus.AWAITING_APPROVAL)
        self.assertEqual(_article_publish_attempts([article])[article.id]["state"], "needs_approval")

    def test_awaiting_confirmation_without_pr_is_stuck(self):
        article = self._article()
        self._child("src-1", ContentFactoryRunStatus.AWAITING_CONFIRMATION)
        attempt = _article_publish_attempts([article])[article.id]
        self.assertEqual(attempt["state"], "stuck")
        self.assertTrue(attempt["recoverable"])

    def test_completed_child_with_pr_has_no_overlay(self):
        article = self._article()
        self._child(
            "src-1",
            ContentFactoryRunStatus.COMPLETED,
            result={"pr_url": "https://github.com/o/r/pull/3", "pr_number": 3},
        )
        self.assertEqual(_article_publish_attempts([article]), {})

    def test_completed_child_without_pr_is_failed(self):
        article = self._article()
        self._child("src-1", ContentFactoryRunStatus.COMPLETED, result={})
        self.assertEqual(_article_publish_attempts([article])[article.id]["state"], "failed")

    def test_serializer_includes_publish_attempt(self):
        article = self._article()
        payload = _serialize_written_article(
            article, publish_attempt={"state": "failed", "reason": "x", "recoverable": True}
        )
        self.assertEqual(payload["publishAttempt"]["state"], "failed")
        self.assertIsNone(_serialize_written_article(article)["publishAttempt"])

    def test_recent_topics_attaches_attempt_to_publishing_only(self):
        publishing = self._article(slug="pub", source_run_id="src-1")
        self._child("src-1", ContentFactoryRunStatus.FAILED)
        published = self._article(
            slug="done", source_run_id="src-2", on_main_verified_at=timezone.now()
        )
        self._child("src-2", ContentFactoryRunStatus.FAILED)
        topics = {topic["slug"]: topic for topic in _recent_written_topics(self.organization)}
        self.assertEqual(topics["pub"]["publishAttempt"]["state"], "failed")
        self.assertIsNone(topics["done"]["publishAttempt"])
