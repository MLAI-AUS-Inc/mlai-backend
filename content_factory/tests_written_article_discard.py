import importlib
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import (
    ArticlePublishStatus,
    KeywordStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
    WrittenArticle,
)
from content_factory.topic_coverage import build_topic_coverage_memory
from content_factory.vibe_marketing_views import (
    _bootstrap_state_fingerprint,
    _keyword_is_available_for_topic_picker,
    _persist_article_memory_from_run,
    _recent_article_drafts,
    _serialize_written_article,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()

REMOTE_OK = {"status": "cancelled"}
REMOTE_TERMINAL_409 = {"error": "Run is terminal.", "content_factory_status_code": 409, "retryable": False}


def _delivery_result(slug, title, keyword, **extra):
    result = {
        "delivery_package": {"title": title, "slug": slug, "target_keyword": keyword},
    }
    result.update(extra)
    return result


class WrittenArticleDiscardTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-discard@example.com",
            password="password",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au"
        )
        self.client.force_authenticate(user=self.user)

        # Don't hit GitHub/sitemap during view tests; the refresh has its own suite.
        refresh_patcher = mock.patch("content_factory.vibe_marketing_views.refresh_publish_statuses")
        self.mock_refresh = refresh_patcher.start()
        self.addCleanup(refresh_patcher.stop)
        remote_patcher = mock.patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_action",
            return_value=REMOTE_OK,
        )
        self.mock_remote = remote_patcher.start()
        self.addCleanup(remote_patcher.stop)

    def _discard_url(self, article):
        return f"/api/v1/vibe-marketing/written-articles/{article.id}/discard/"

    def _article(self, slug="meetup-article", title="AI Meetup Article", keyword="ai meetup", **overrides):
        fields = {
            "organization": self.organization,
            "title": title,
            "slug": slug,
            "category": "featured",
            "primary_keyword": keyword,
        }
        fields.update(overrides)
        return WrittenArticle.objects.create(**fields)

    def _keyword(self, article, text="ai meetup", **overrides):
        fields = {
            "organization": self.organization,
            "keyword": text,
            "keyword_normalized": text,
            "status": KeywordStatus.WRITTEN,
            "written_article": article,
            "cooldown_until": timezone.now() + timedelta(days=30),
        }
        fields.update(overrides)
        return ResearchedKeyword.objects.create(**fields)

    def _run(self, run_id, *, status=ContentFactoryRunStatus.COMPLETED, result=None, workflow="article_generation"):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow=workflow,
            domain="mlai.au",
            status=status,
            result=result if result is not None else {},
        )

    def test_discard_written_article_full_cleanup(self):
        article = self._article(source_run_id="run-root")
        keyword = self._keyword(article)
        root = self._run("run-root", result=_delivery_result("meetup-article", "AI Meetup Article", "ai meetup"))
        revision = self._run(
            "run-rev",
            status=ContentFactoryRunStatus.RUNNING,
            workflow="article_revision",
            result={"source_run_id": "run-root"},
        )

        response = self.client.post(self._discard_url(article))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["discarded"])
        self.assertCountEqual(response.data["cancelledRunIds"], ["run-root", "run-rev"])
        self.assertFalse(WrittenArticle.objects.filter(pk=article.pk).exists())

        root.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(root.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(revision.status, ContentFactoryRunStatus.CANCELLED)

        keyword.refresh_from_db()
        self.assertEqual(keyword.status, KeywordStatus.PENDING)
        self.assertIsNone(keyword.written_article)
        self.assertIsNone(keyword.cooldown_until)

        coverage = build_topic_coverage_memory(self.organization)
        self.assertTrue(_keyword_is_available_for_topic_picker(keyword, coverage_memory=coverage))
        self.assertEqual(_recent_article_drafts(self.organization), [])

    def test_discard_pr_open_blocked(self):
        article = self._article(publish_status=ArticlePublishStatus.PR_OPEN)
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 409)
        self.assertIn("open GitHub pull request", response.data["detail"])
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_discard_merged_and_live_blocked(self):
        for status_value in (ArticlePublishStatus.MERGED, ArticlePublishStatus.LIVE):
            article = self._article(slug=f"slug-{status_value}", publish_status=status_value)
            response = self.client.post(self._discard_url(article))
            self.assertEqual(response.status_code, 409)
            self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_discard_pr_closed_with_pr_evidence_succeeds(self):
        # Anti-PROTECTED regression: the drafts group-cancel would skip runs
        # carrying PR evidence; discard gates on publish_status instead.
        article = self._article(
            publish_status=ArticlePublishStatus.PR_CLOSED,
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            source_run_id="run-root",
        )
        run = self._run(
            "run-root",
            result=_delivery_result(
                "meetup-article",
                "AI Meetup Article",
                "ai meetup",
                pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            ),
        )
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_stale_written_with_open_pr_evidence_advances_then_blocks(self):
        article = self._article(publish_status=ArticlePublishStatus.WRITTEN, source_run_id="run-root")
        self._run(
            "run-root",
            result=_delivery_result(
                "meetup-article",
                "AI Meetup Article",
                "ai meetup",
                pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/991",
            ),
        )
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 409)
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.PR_OPEN)

    def test_remote_terminal_409_still_cancels_locally(self):
        self.mock_remote.return_value = REMOTE_TERMINAL_409
        article = self._article(source_run_id="run-root")
        run = self._run("run-root", result=_delivery_result("meetup-article", "AI Meetup Article", "ai meetup"))
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)

    def test_multiple_keywords_reset(self):
        article = self._article()
        first = self._keyword(article, text="ai meetup")
        second = self._keyword(article, text="melbourne ai meetup")
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        for keyword in (first, second):
            keyword.refresh_from_db()
            self.assertEqual(keyword.status, KeywordStatus.PENDING)
            self.assertIsNone(keyword.written_article)

    def test_fkless_written_keyword_released(self):
        article = self._article()
        orphan = self._keyword(article, text="ai meetup", written_article=None)
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, KeywordStatus.PENDING)

    def test_fkless_keyword_kept_when_other_article_covers_it(self):
        article = self._article(slug="first-article")
        self._article(slug="second-article", title="Second Article", keyword="ai meetup")
        orphan = self._keyword(article, text="ai meetup", written_article=None)
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, KeywordStatus.WRITTEN)

    def test_article_without_keyword_discards(self):
        article = self._article()
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_second_discard_returns_404(self):
        article = self._article()
        first = self.client.post(self._discard_url(article))
        self.assertEqual(first.status_code, 200)
        second = self.client.post(self._discard_url(article))
        self.assertEqual(second.status_code, 404)

    def test_other_org_article_is_404(self):
        other_org = Organization.objects.create(name="Other", domain="other.example")
        article = WrittenArticle.objects.create(
            organization=other_org,
            title="Foreign",
            slug="foreign",
            category="featured",
            primary_keyword="foreign",
        )
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_bootstrap_fingerprint_changes_after_discard(self):
        article = self._article()
        before = _bootstrap_state_fingerprint(self.organization, self.company, self.config)
        response = self.client.post(self._discard_url(article))
        self.assertEqual(response.status_code, 200)
        after = _bootstrap_state_fingerprint(self.organization, self.company, self.config)
        self.assertNotEqual(before, after)


class SourceRunIdPersistenceTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")

    def _completed_run(self, run_id, result, workflow="article_generation"):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow=workflow,
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result=result,
        )

    def test_persist_sets_source_run_id_on_create(self):
        run = self._completed_run("run-write", _delivery_result("my-article", "My Article", "my keyword"))
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        self.assertEqual(article.source_run_id, "run-write")

    def test_revision_run_overwrites_source_run_id(self):
        first = self._completed_run("run-write", _delivery_result("my-article", "My Article", "my keyword"))
        _persist_article_memory_from_run(organization=self.organization, run=first)
        revision = self._completed_run(
            "run-rev",
            {**_delivery_result("my-article", "My Article", "my keyword"), "source_run_id": "run-write"},
            workflow="article_revision",
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=revision)
        self.assertEqual(article.source_run_id, "run-rev")

    def test_publish_child_does_not_overwrite_source_run_id(self):
        first = self._completed_run("run-write", _delivery_result("my-article", "My Article", "my keyword"))
        _persist_article_memory_from_run(organization=self.organization, run=first)
        publish_child = self._completed_run(
            "run-publish",
            {
                **_delivery_result("my-article", "My Article", "my keyword"),
                "source_run_id": "run-write",
                "resolved_delivery_mode": "publish_code",
                "pr_url": "https://github.com/MLAI-AUS-Inc/mlai-au/pull/990",
            },
        )
        article = _persist_article_memory_from_run(organization=self.organization, run=publish_child)
        self.assertEqual(article.source_run_id, "run-write")
        self.assertEqual(article.publish_status, ArticlePublishStatus.PR_OPEN)

    def test_serializer_exposes_run_id(self):
        run = self._completed_run("run-write", _delivery_result("my-article", "My Article", "my keyword"))
        article = _persist_article_memory_from_run(organization=self.organization, run=run)
        payload = _serialize_written_article(article)
        self.assertEqual(payload["runId"], "run-write")


class BackfillMigrationTest(TestCase):
    def setUp(self):
        self.migration = importlib.import_module(
            "content_factory.migrations.0017_backfill_writtenarticle_source_run_id"
        )
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")

    def test_extract_run_slug_precedence(self):
        extract = self.migration.extract_run_slug
        self.assertEqual(extract({"delivery_package": {"slug": "a"}, "slug": "z"}), "a")
        self.assertEqual(extract({"article_meta": {"slug": "b"}}), "b")
        self.assertEqual(extract({}, {"evidence_summary": {"content_package_slug": "c"}}), "c")
        self.assertEqual(extract({"slug": "d"}), "d")
        self.assertEqual(extract(None), "")
        self.assertEqual(extract({"delivery_package": "not-a-dict"}), "")

    def test_backfill_links_newest_matching_run(self):
        article = WrittenArticle.objects.create(
            organization=self.organization,
            title="My Article",
            slug="my-article",
            category="featured",
            primary_keyword="my keyword",
        )
        ContentFactoryRun.objects.create(
            run_id="run-old",
            workflow="article_generation",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result=_delivery_result("my-article", "My Article", "my keyword"),
        )
        newest = ContentFactoryRun.objects.create(
            run_id="run-new",
            workflow="article_revision",
            domain="mlai.au",
            status=ContentFactoryRunStatus.COMPLETED,
            result=_delivery_result("my-article", "My Article", "my keyword"),
        )
        from django.apps import apps

        self.migration.backfill_source_run_ids(apps, None)
        article.refresh_from_db()
        self.assertEqual(article.source_run_id, newest.run_id)

    def test_backfill_leaves_unmatched_articles_empty(self):
        article = WrittenArticle.objects.create(
            organization=self.organization,
            title="Orphan",
            slug="orphan-article",
            category="featured",
            primary_keyword="orphan",
        )
        from django.apps import apps

        self.migration.backfill_source_run_ids(apps, None)
        article.refresh_from_db()
        self.assertEqual(article.source_run_id, "")
