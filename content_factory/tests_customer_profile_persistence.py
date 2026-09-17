"""Run with scripts/test_customer_profiles_database.py after migration approval."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event
import hashlib
import json
import time
import uuid
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from content_factory.models import WrittenArticle, OrganizationContentConfig
from content_factory.article_editorial import upsert_written_article, ArticleEditorialConflict
from content_factory.editorial_contract import AudienceOption, ArticleEditorialBrief


class CustomerProfileFixtures:
    def setUp(self):
        self.org = Organization.objects.create(name="Fixture", domain="example.test")
        OrganizationContentConfig.objects.create(organization=self.org, pillar_strategy={"editorial_catalog": {}})
        self.analytics = uuid.uuid4()
        self.brief = ArticleEditorialBrief(audience_id="owners", audience_version=1, conversion_intent="none",
            no_offer_reason="Teach an implementation decision", country="AU", reader_task="Choose a workflow",
            distinct_contribution="A labelled comparison", acceptance_criteria=["Explain limitations"]).model_dump(mode="json")
        self.audience = AudienceOption(id="owners", catalog_schema_version=2, name="Firm owners", description="Own a small practice",
            reader_task="Choose a workflow", allow_no_offer=True, status="approved", approved_by="user:fixture",
            approved_at="2026-09-16T00:00:00Z").model_dump(mode="json")

    def run_record(self, name, parent=None, analytics=None):
        selected = {"brief": self.brief, "audience": self.audience, "offer": None}
        admission = {"schema_version": "2026-09-16.1", "checked_at": "2026-09-16T00:00:00Z", "domain": self.org.domain,
            "github_repo": None, **selected, "selection_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()}
        request = {"editorial_brief": deepcopy(self.brief), "editorial_admission": admission, "analytics_article_id": str(analytics or self.analytics)}
        if parent: request["revision_source_run_id"] = parent
        return ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain, run_id=name,
            workflow="article_revision" if parent else "article_generation", run_request=request)

    def write(self, run, slug="workflow"):
        return upsert_written_article(organization=self.org, slug=slug, source_run_id=run.run_id,
            defaults={"title": run.run_id, "category": "guides", "primary_keyword": "workflow"})[0]


class CustomerProfilePersistenceTests(CustomerProfileFixtures, TestCase):
    def test_roundtrip_retry_revision_and_late_callback(self):
        first = self.run_record("first")
        article = self.write(first)
        original = deepcopy(article.original_editorial_snapshot)
        self.write(first)
        self.assertEqual(WrittenArticle.objects.count(), 1)
        revision = self.run_record("revision", parent=first.run_id)
        self.write(revision, slug="new-angle")
        self.write(first)
        article.refresh_from_db()
        self.assertEqual(article.slug, "new-angle")
        self.assertEqual(article.source_run_id, revision.run_id)
        self.assertEqual(article.original_editorial_snapshot, original)
        self.assertEqual(article.editorial_snapshot["writing_run_id"], revision.run_id)
        self.assertEqual(article.audience_id, "owners")
        self.assertEqual(article.editorial_provenance_status, "recorded")
        self.assertEqual(article.analytics_id, self.analytics)

    def test_unrelated_collision_rolls_back(self):
        article = self.write(self.run_record("first"))
        with self.assertRaises(ArticleEditorialConflict): self.write(self.run_record("unrelated"))
        article.refresh_from_db()
        self.assertEqual(article.title, "first")
        self.assertEqual(WrittenArticle.objects.count(), 1)

    def test_distinct_articles_with_the_same_keyword_remain_queryable(self):
        self.write(self.run_record("first"))
        self.write(self.run_record("other", analytics=uuid.uuid4()), slug="another-reader-task")
        self.assertEqual(WrittenArticle.objects.filter(organization=self.org, audience_id="owners").count(), 2)

    def test_foreign_writing_run_and_missing_evidence_are_rejected(self):
        foreign = Organization.objects.create(name="Other", domain="other.test")
        run = self.run_record("first")
        with self.assertRaises(ArticleEditorialConflict):
            upsert_written_article(organization=foreign, slug="workflow", source_run_id=run.run_id, defaults={})
        with self.assertRaises(ArticleEditorialConflict):
            upsert_written_article(organization=self.org, slug="workflow", defaults={})
        self.assertFalse(WrittenArticle.objects.exists())

    def test_another_tenants_article_identity_is_a_conflict(self):
        foreign = Organization.objects.create(name="Other", domain="other.test")
        existing = WrittenArticle.objects.create(organization=foreign, slug="private", title="Private article",
            category="guides", primary_keyword="private", analytics_id=self.analytics)
        with self.assertRaises(ArticleEditorialConflict):
            self.write(self.run_record("first"))
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Private article")
        self.assertEqual(WrittenArticle.objects.count(), 1)

    @override_settings(ROO_API_KEY="synthetic-article-sync-key")
    def test_direct_sync_and_callback_share_one_historical_decision(self):
        from rest_framework.test import APIRequestFactory
        from .service_views import SEOWrittenArticleCreateView
        from .vibe_marketing_views import _persist_article_memory_from_run
        run = self.run_record("first")
        payload = {"domain": self.org.domain, "slug": "workflow", "title": "First article",
                   "category": "guides", "primary_keyword": "workflow", "source_run_id": run.run_id,
                   "analytics_id": str(self.analytics), "editorial_admission": run.run_request["editorial_admission"]}
        request = APIRequestFactory().post("/api/seo/articles/", payload, format="json", HTTP_X_API_KEY="synthetic-article-sync-key")
        response = SEOWrittenArticleCreateView.as_view()(request)
        self.assertEqual(response.status_code, 201, response.data)
        first = WrittenArticle.objects.get()
        run.status = "completed"
        run.result = {"delivery_package": {"slug": "workflow", "title": "First article", "target_keyword": "workflow"}}
        run.save(update_fields=["status", "result"])
        callback = _persist_article_memory_from_run(organization=self.org, run=run)
        self.assertEqual(callback.pk, first.pk)
        self.assertEqual(callback.editorial_snapshot, first.editorial_snapshot)
        self.assertEqual(callback.original_editorial_snapshot, first.original_editorial_snapshot)
        self.assertEqual(WrittenArticle.objects.count(), 1)

    def test_publish_child_preserves_parent_snapshot_and_late_publish_cannot_replace_revision(self):
        from .vibe_marketing_views import _apply_publish_child_evidence_to_article
        first = self.run_record("first")
        article = self.write(first)
        original = deepcopy(article.editorial_snapshot)
        child = ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain, run_id="publish",
            workflow="article_generation", run_request={"source_run_id": first.run_id, "delivery_mode": "publish_code"},
            result={"pr_url": "https://github.com/example/repo/pull/1", "pr_number": 1})
        article, _ = upsert_written_article(organization=self.org, slug=article.slug, source_run_id=child.run_id,
            defaults={"pr_url": child.result["pr_url"], "pr_number": 1, "publish_status": "pr_open"})
        self.assertEqual(article.editorial_snapshot, original)
        self.assertEqual(article.source_run_id, first.run_id)
        revision = self.run_record("revision", parent=first.run_id)
        self.write(revision)
        _apply_publish_child_evidence_to_article(self.org, first, child)
        self.write(first)
        article.refresh_from_db()
        self.assertEqual(article.source_run_id, revision.run_id)
        self.assertEqual(article.editorial_snapshot["writing_run_id"], revision.run_id)
        self.assertEqual(article.original_editorial_snapshot, original)


class CustomerProfileRegistryTests(CustomerProfileFixtures, TestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from rest_framework.test import APIRequestFactory
        self.factory = APIRequestFactory()
        self.user = get_user_model().objects.create_user(email="owner@example.test", password=None, role="participant")
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org, name="Fixture", domain=self.org.domain)
        self.other_org = Organization.objects.create(name="Other", domain="other.test")
        self.other_company = VibeRaisingCompany.objects.create(profile=profile, organization=self.other_org, name="Other", domain=self.other_org.domain)

    def request(self, view, method="get", payload=None, user=None):
        from rest_framework.test import force_authenticate
        request = getattr(self.factory, method)("/editorial-catalog/", payload or {"company_id": str(self.company.pk)}, format="json")
        force_authenticate(request, user=user or self.user)
        return view.as_view()(request)

    def test_registry_filters_snapshots_pagination_and_company_isolation(self):
        from .editorial_views import EditorialArticlesView
        article = self.write(self.run_record("first"))
        WrittenArticle.objects.bulk_create([
            WrittenArticle(organization=self.org, slug=f"legacy-{i}", title=f"Legacy {i}", category="guides", primary_keyword="old")
            for i in range(26)
        ])
        WrittenArticle.objects.create(organization=self.other_org, slug="private", title="Other company", category="guides", primary_keyword="workflow", audience_id="secret-profile")
        response = self.request(EditorialArticlesView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], 27)
        self.assertEqual(len(response.data["articles"]), 25)
        self.assertEqual(response.data["audienceIds"], ["owners"])
        page = self.request(EditorialArticlesView, payload={"company_id": str(self.company.pk), "offset": 25})
        self.assertEqual(len(page.data["articles"]), 2)
        filtered = self.request(EditorialArticlesView, payload={"company_id": str(self.company.pk), "audience_id": "owners", "offer_id": "__none__", "q": "work"})
        self.assertEqual(filtered.data["total"], 1)
        self.assertEqual(filtered.data["articles"][0]["editorialSnapshot"], article.editorial_snapshot)
        self.assertEqual(filtered.data["articles"][0]["originalEditorialSnapshot"], article.original_editorial_snapshot)
        unknown = self.request(EditorialArticlesView, payload={"company_id": str(self.company.pk), "audience_id": "__unknown__"})
        self.assertEqual(unknown.data["total"], 26)
        other = self.request(EditorialArticlesView, payload={"company_id": str(self.other_company.pk)})
        self.assertEqual(other.data["total"], 1)
        self.assertEqual(other.data["audienceIds"], ["secret-profile"])

    def test_foreign_owner_cannot_read_registry_or_approve_profile(self):
        from django.contrib.auth import get_user_model
        from .editorial_views import EditorialArticlesView, EditorialCatalogApprovalView
        from founder_tools.models import VibeRaisingProfile
        stranger = get_user_model().objects.create_user(email="stranger@example.test", password=None, role="participant")
        VibeRaisingProfile.objects.create(user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.write(self.run_record("first"))
        self.assertEqual(self.request(EditorialArticlesView, user=stranger).status_code, 404)
        self.assertEqual(self.request(EditorialCatalogApprovalView, "post", user=stranger).status_code, 404)

    def test_owner_draft_approval_and_stale_update_are_persisted_atomically(self):
        from .editorial_views import EditorialCatalogView, EditorialCatalogApprovalView
        from .editorial_catalog import catalog_payload
        draft = {key: value for key, value in self.audience.items() if key not in {"status", "approved_at", "approved_by"}}
        payload = {"company_id": str(self.company.pk), "expected_editorial_catalog_version": 0, "audience_options": [draft]}
        saved = self.request(EditorialCatalogView, "put", payload)
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["audience_options"][0]["status"], "draft")
        approved = self.request(EditorialCatalogApprovalView, "post", {
            "company_id": str(self.company.pk), "expected_editorial_catalog_version": saved.data["editorial_catalog_version"],
            "entries": saved.data["review_entries"],
        })
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["audience_options"][0]["approved_by"], f"user:{self.user.pk}")
        config = OrganizationContentConfig.objects.get(organization=self.org)
        before = deepcopy(config.pillar_strategy)
        stale = self.request(EditorialCatalogView, "put", payload)
        self.assertEqual(stale.status_code, 409)
        config.refresh_from_db()
        self.assertEqual(config.pillar_strategy, before)
        self.assertEqual(catalog_payload(before)["audience_options"][0]["status"], "approved")


@skipUnlessDBFeature("has_select_for_update")
class CustomerProfileConcurrencyTests(CustomerProfileFixtures, TransactionTestCase):
    def race(self, *runs):
        barrier = Barrier(len(runs))

        def write(run):
            try:
                barrier.wait(timeout=10)
                return self.write(run)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(runs)) as executor:
            futures = [executor.submit(write, run) for run in runs]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_callbacks_create_one_article(self):
        run = self.run_record("first")
        rows = self.race(run, run)
        self.assertEqual(rows[0].pk, rows[1].pk)
        self.assertEqual(WrittenArticle.objects.count(), 1)
        self.assertEqual(rows[0].editorial_snapshot, rows[1].editorial_snapshot)

    def test_revision_wins_when_racing_a_late_callback(self):
        first = self.run_record("first")
        original = self.write(first).original_editorial_snapshot
        revision = self.run_record("revision", parent=first.run_id)
        self.race(first, revision)
        article = WrittenArticle.objects.get()
        self.assertEqual(article.source_run_id, revision.run_id)
        self.assertEqual(article.original_editorial_snapshot, original)
        self.assertEqual(article.editorial_snapshot["writing_run_id"], revision.run_id)

    def test_writer_waits_for_organization_lock_before_creating(self):
        run = self.run_record("first")
        attempting = Event()
        writer_pid = []

        def write():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    writer_pid.append(cursor.fetchone()[0])
                attempting.set()
                return self.write(run)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    holder_pid = cursor.fetchone()[0]
                future = executor.submit(write)
                self.assertTrue(attempting.wait(timeout=10))
                deadline = time.monotonic() + 10
                blockers = []
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [writer_pid[0]])
                        blockers = cursor.fetchone()[0]
                    if holder_pid in blockers:
                        break
                    time.sleep(0.01)
                self.assertIn(holder_pid, blockers)
                self.assertFalse(future.done())
                self.assertFalse(WrittenArticle.objects.exists())
            article = future.result(timeout=10)
        self.assertEqual(article.editorial_provenance_status, "recorded")
