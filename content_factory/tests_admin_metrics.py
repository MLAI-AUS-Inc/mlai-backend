from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from roo.models import Ledger, PointsPurchase
from startup_updates.models import UserStartupBinding
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

from .models import ArticlePublishStatus, WrittenArticle


User = get_user_model()
USAGE_URL = "/api/v1/vibe-marketing/admin/usage/"


class VibeMarketingAdminUsageTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(email="marketing-admin@example.com")
        self.founder = User.objects.create_user(
            email="founder-one@example.com", slack_id="UFOUNDER1"
        )
        self.second_founder = User.objects.create_user(
            email="founder-two@example.com", slack_id="UFOUNDER2"
        )
        self.inactive_buyer = User.objects.create_user(
            email="not-using-marketing@example.com", slack_id="UINACTIVE"
        )
        self.first_org = Organization.objects.create(
            name="First Startup", domain="first-startup.example"
        )
        self.second_org = Organization.objects.create(
            name="Second Startup", domain="second-startup.example"
        )
        UserStartupBinding.objects.create(
            user=self.founder, organization=self.first_org
        )
        UserStartupBinding.objects.create(
            user=self.second_founder, organization=self.second_org
        )

    def _run(self, run_id, workflow, user, organization, status):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow=workflow,
            domain=organization.domain,
            organization=organization,
            slack_user_id=(
                f"mlai_user:{user.id}" if user == self.founder else user.slack_id
            ),
            status=status,
        )

    def _article(self, *, slug, organization, source_run, status):
        return WrittenArticle.objects.create(
            organization=organization,
            title=f"Article {slug}",
            slug=slug,
            category="guides",
            primary_keyword=slug,
            source_run_id=source_run.run_id,
            publish_status=status,
        )

    def _purchase(self, user, *, status="paid", points=20, cents=1000):
        return PointsPurchase.objects.create(
            user=user,
            slack_user_id=user.slack_id,
            pack_id="test-pack",
            points_amount=points,
            amount_cents=cents,
            currency="aud",
            status=status,
            paid_at=timezone.now() if status == "paid" else None,
        )

    def test_rejects_anonymous_and_non_admin_users(self):
        self.assertIn(self.client.get(USAGE_URL).status_code, (401, 403))
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(USAGE_URL).status_code, 403)

    def test_aggregates_adoption_output_points_purchases_and_operations(self):
        discovery = self._run(
            "discovery-1",
            "content_factory_discovery",
            self.founder,
            self.first_org,
            ContentFactoryRunStatus.COMPLETED,
        )
        article_run = self._run(
            "article-1",
            "article_generation",
            self.founder,
            self.first_org,
            ContentFactoryRunStatus.COMPLETED,
        )
        # A retry by the same founder must not increase distinct adoption.
        self._run(
            "article-revision-1",
            "article_revision",
            self.founder,
            self.first_org,
            ContentFactoryRunStatus.BLOCKED,
        )
        failed_run = self._run(
            "baseline-2",
            "website_baseline",
            self.second_founder,
            self.second_org,
            ContentFactoryRunStatus.FAILED,
        )

        self._article(
            slug="live-article",
            organization=self.first_org,
            source_run=article_run,
            status=ArticlePublishStatus.LIVE,
        )
        self._article(
            slug="written-article",
            organization=self.second_org,
            source_run=failed_run,
            status=ArticlePublishStatus.WRITTEN,
        )

        Ledger.objects.create(
            user=self.founder,
            delta=-6,
            kind="SPEND",
            source="CONTENT_FACTORY",
        )
        Ledger.objects.create(
            user=self.founder,
            delta=1,
            kind="REFUND",
            source="CONTENT_FACTORY",
        )
        Ledger.objects.create(
            user=self.founder,
            delta=-100,
            kind="SPEND",
            source="TASK",
        )
        self._purchase(self.founder, points=20, cents=1000)
        self._purchase(self.second_founder, status="pending", points=50, cents=2000)
        # Paid in the period, but not attributed to the Vibe Marketing cohort.
        self._purchase(self.inactive_buyer, points=100, cents=4000)

        self.client.force_authenticate(self.admin)
        response = self.client.get(USAGE_URL + "?range=30d")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["range"]["key"], "30d")
        self.assertEqual(
            body["summary"],
            {
                "activeUsers": 2,
                "activeStartups": 2,
                "articlesCreated": 2,
                "articlesLive": 1,
                "grossPointsSpent": 6,
                "refundedPoints": 1,
                "netPointsSpent": 5,
                "purchasers": 1,
                "pointsPurchased": 20,
                "purchaseRevenueCents": 1000,
                "currency": "AUD",
                "failedRuns": 1,
                "blockedRuns": 1,
            },
        )
        self.assertEqual(
            {item["key"]: item["users"] for item in body["funnel"]},
            {
                "started": 2,
                "researched": 1,
                "article_created": 2,
                "article_live": 1,
            },
        )
        self.assertEqual(len(body["timeline"]), 6)
        self.assertEqual(body["timeline"][-1]["articlesCreated"], 2)
        self.assertEqual(body["previous"]["activeUsers"], 0)
        self.assertIsNotNone(discovery)

    def test_range_excludes_older_records_and_all_includes_them(self):
        old_run = self._run(
            "old-article-run",
            "article_generation",
            self.founder,
            self.first_org,
            ContentFactoryRunStatus.COMPLETED,
        )
        old_article = self._article(
            slug="old-article",
            organization=self.first_org,
            source_run=old_run,
            status=ArticlePublishStatus.LIVE,
        )
        old_ledger = Ledger.objects.create(
            user=self.founder,
            delta=-6,
            kind="SPEND",
            source="CONTENT_FACTORY",
        )
        old_purchase = self._purchase(self.founder, points=20, cents=1000)
        old_time = timezone.now() - timedelta(days=100)
        ContentFactoryRun.objects.filter(pk=old_run.pk).update(created_at=old_time)
        WrittenArticle.objects.filter(pk=old_article.pk).update(created_at=old_time)
        Ledger.objects.filter(pk=old_ledger.pk).update(created_at=old_time)
        PointsPurchase.objects.filter(pk=old_purchase.pk).update(
            created_at=old_time, paid_at=old_time
        )

        self.client.force_authenticate(self.admin)
        recent = self.client.get(USAGE_URL + "?range=30d").json()
        self.assertEqual(recent["summary"]["activeUsers"], 0)
        self.assertEqual(recent["summary"]["articlesCreated"], 0)
        self.assertEqual(recent["summary"]["netPointsSpent"], 0)
        self.assertEqual(recent["summary"]["pointsPurchased"], 0)

        lifetime = self.client.get(USAGE_URL + "?range=all").json()
        self.assertEqual(lifetime["summary"]["activeUsers"], 1)
        self.assertEqual(lifetime["summary"]["articlesCreated"], 1)
        self.assertEqual(lifetime["summary"]["netPointsSpent"], 6)
        self.assertEqual(lifetime["summary"]["pointsPurchased"], 20)
        self.assertIsNone(lifetime["previous"])

    def test_invalid_range_falls_back_to_thirty_days(self):
        self.client.force_authenticate(self.admin)
        body = self.client.get(USAGE_URL + "?range=surprise").json()
        self.assertEqual(body["range"]["key"], "30d")

    def test_preserved_legacy_actor_resolves_without_organization_binding(self):
        ContentFactoryRun.objects.create(
            run_id="legacy-collision-run",
            workflow="content_factory_discovery",
            domain="",
            organization=None,
            slack_user_id=f"web_{self.founder.pk}",
            status=ContentFactoryRunStatus.COMPLETED,
        )

        self.client.force_authenticate(self.admin)
        body = self.client.get(USAGE_URL + "?range=30d").json()

        self.assertEqual(body["summary"]["activeUsers"], 1)
        self.assertEqual(
            {item["key"]: item["users"] for item in body["funnel"]}["researched"],
            1,
        )
