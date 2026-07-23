from __future__ import annotations

import importlib
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    AnalyticsSyncSource,
    AnalyticsSyncState,
    ArticleAnalyticsLocation,
    ArticleBehaviorDaily,
    ArticleSearchDaily,
    ArticleSearchQueryDaily,
    ArticleTrafficSourceDaily,
    SearchConsoleProperty,
    SearchConsolePropertyStatus,
)
from content_analytics.services.config import analytics_article_manifest, public_analytics_config
from content_analytics.services.reporting import build_analytics_summary
from content_analytics.services.locations import (
    location_for_day,
    record_article_location,
)
from content_analytics.services.search_console import (
    SearchConsoleVerificationError,
    verify_search_console_property,
)
from content_analytics.services.sync import (
    sync_due_analytics,
    sync_organization_analytics,
    sync_search_console_property,
    sync_umami_site,
)
from content_analytics.services.umami import UmamiArticleDay, UmamiClient, classify_referrer, normalize_path
from content_analytics.views import analytics_status_payload
from content_factory.models import (
    ArticlePublishStatus,
    OrganizationContentConfig,
    WrittenArticle,
)
from content_factory.vibe_marketing_views import _persist_article_memory_from_run
from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


ANALYTICS_SETTINGS = {
    "UMAMI_BASE_URL": "https://umami.internal.test",
    "UMAMI_API_TOKEN": "secret-token",
    "CONTENT_ANALYTICS_TRACKER_SCRIPT_URL": "https://analytics.example.test/script.js",
    "CONTENT_ANALYTICS_HOST_URL": "https://analytics.example.test",
    "CONTENT_ANALYTICS_FIRST_PARTY_PROXY_ENABLED": False,
}


@override_settings(**ANALYTICS_SETTINGS)
class ContentAnalyticsContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="analytics-founder@example.com", password="password")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        self.organization = Organization.objects.create(name="Analytics Co", domain="example.com")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="Analytics Co",
            domain="example.com",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            baseline_skipped_at=timezone.now(),
        )
        self.site = AnalyticsSite.objects.create(
            organization=self.organization,
            domain="example.com",
            external_website_id="876b328e-1306-4b4e-b0c6-7e7d03597fb9",
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
            enabled=True,
            tracker_script_url=ANALYTICS_SETTINGS["CONTENT_ANALYTICS_TRACKER_SCRIPT_URL"],
            collector_url=ANALYTICS_SETTINGS["CONTENT_ANALYTICS_HOST_URL"],
            last_synced_at=timezone.now(),
        )
        self.article = WrittenArticle.objects.create(
            organization=self.organization,
            title="Tracked article",
            slug="tracked-article",
            category="featured",
            primary_keyword="tracked keyword",
            publish_status=ArticlePublishStatus.LIVE,
            live_url="https://www.example.com/articles/tracked-article",
            canonical_url="https://www.example.com/articles/tracked-article",
            canonical_path="/articles/tracked-article",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_public_config_contains_exact_apex_and_www_domains(self):
        payload = public_analytics_config(self.organization, analytics_article_id=self.article.analytics_id)

        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["website_id"], self.site.external_website_id)
        self.assertEqual(payload["data_domains"], ["example.com", "www.example.com"])
        self.assertEqual(payload["analytics_article_id"], str(self.article.analytics_id))
        self.assertFalse(payload["identify_visitors"])
        self.assertFalse(payload["session_replay"])

    def test_google_country_domains_are_classified_as_search(self):
        for referrer in ("https://www.google.co.uk/search?q=x", "google.ca", "google.co.nz"):
            self.assertEqual(classify_referrer(referrer), ("search", "Google"))

    def test_path_normalization_matches_umami_decode_uri_and_trailing_slash_rules(self):
        self.assertEqual(normalize_path("/articles/caf%C3%A9/"), "/articles/café")
        self.assertEqual(normalize_path("/articles/a%2Fb%3Fc%23d"), "/articles/a%2Fb%3Fc%23d")
        self.assertEqual(normalize_path("/"), "/")

    @override_settings(CONTENT_ANALYTICS_UMAMI_SOURCE_ATTRIBUTION_LIMIT=3)
    def test_umami_daily_sync_uses_one_event_aggregate_and_bounded_utm_enrichment(self):
        client = object.__new__(UmamiClient)
        client._stats = MagicMock(
            return_value={"pageviews": 8, "visitors": 6, "visits": 7, "bounces": 2, "totaltime": 30}
        )
        client._event_visits_by_name = MagicMock(
            return_value={
                "cf_engaged_30s": 5,
                "cf_scroll_50": 4,
                "cf_scroll_90": 2,
                "cf_cta_impression": 3,
                "cf_cta_click": 1,
            }
        )
        client._event_unique_visits = MagicMock()
        client._referrers = MagicMock(
            return_value=[{"name": "google.com", "pageviews": 4, "visitors": 3, "visits": 3}]
        )
        client._utm_sources = MagicMock(
            return_value=[{"utm": f"source-{index}", "views": 1} for index in range(8)]
        )

        result = client.fetch_article_day(
            self.site.external_website_id,
            path="/articles/tracked-article/",
            day=timezone.now().date() - timedelta(days=1),
        )

        self.assertEqual(client._stats.call_count, 4)  # One headline query + top three UTM sources.
        client._event_visits_by_name.assert_called_once()
        client._event_unique_visits.assert_not_called()
        client._referrers.assert_called_once()
        client._utm_sources.assert_called_once()
        self.assertEqual(result.milestones["cta_click_count"], 1)
        self.assertEqual(result.milestones["outbound_click_count"], 0)
        self.assertFalse(result.source_attribution_complete)

    def test_existing_article_manifest_uses_exact_ids_and_is_bounded(self):
        written = WrittenArticle.objects.create(
            organization=self.organization,
            title="Known draft",
            slug="known-draft",
            category="featured",
            primary_keyword="known draft",
        )
        qualified = WrittenArticle.objects.create(
            organization=self.organization,
            title="Qualified slug",
            slug="guides/qualified-slug",
            category="should-not-prefix",
            primary_keyword="qualified slug",
            publish_status=ArticlePublishStatus.MERGED,
        )

        manifest = analytics_article_manifest(self.organization, limit=3)

        self.assertEqual(
            manifest,
            [
                {
                    "slug": f"featured/{self.article.slug}",
                    "analytics_article_id": str(self.article.analytics_id),
                },
                {
                    "slug": qualified.slug,
                    "analytics_article_id": str(qualified.analytics_id),
                },
            ],
        )
        self.assertEqual(len(analytics_article_manifest(self.organization, limit=1)), 1)

    def test_status_exposes_frontend_contract_and_disable_semantics(self):
        payload = analytics_status_payload(self.organization)

        for key in ("status", "available", "enabled", "collecting", "provider", "lastSyncedAt", "message", "gsc"):
            self.assertIn(key, payload)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["enabled"])
        self.assertIn("collection", payload["behavior"]["disableSemantics"].lower())
        self.assertIn("mlai-backend stores only daily", payload["behavior"]["rawStoreSemantics"])

    def test_collecting_requires_applied_scaffold_proof_not_only_a_completed_request(self):
        self.config.articles_scaffolded = True
        self.config.save(update_fields=["articles_scaffolded", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="analytics-proof-status",
            workflow="article_system_setup",
            domain=self.organization.domain,
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={
                "analytics_config": {
                    "enabled": True,
                    "website_id": self.site.external_website_id,
                }
            },
            result={
                "article_seed_proof": {
                    "analytics": {
                        "status": "unsupported_target",
                        "website_id": self.site.external_website_id,
                    }
                }
            },
        )

        self.assertFalse(analytics_status_payload(self.organization)["collecting"])

        run.result = {
            "article_seed_proof": {
                "analytics": {
                    "status": "applied",
                    "website_id": self.site.external_website_id,
                    "article_manifest": {
                        "requested_count": 2,
                        "applied_count": 1,
                    },
                }
            }
        }
        run.save(update_fields=["result", "updated_at"])
        self.assertFalse(analytics_status_payload(self.organization)["collecting"])

        run.result["article_seed_proof"]["analytics"]["article_manifest"]["applied_count"] = 2
        run.save(update_fields=["result", "updated_at"])
        self.assertTrue(analytics_status_payload(self.organization)["collecting"])

    @patch("content_analytics.services.search_console._search_console_service")
    def test_search_console_rejects_cross_tenant_requested_property_before_api_access(self, service):
        with self.assertRaises(SearchConsoleVerificationError):
            verify_search_console_property(
                organization=self.organization,
                requested_site_url="sc-domain:another-customer.example",
                access_method="service_account",
            )

        service.assert_not_called()

    def test_summary_uses_aggregate_gsc_grain_and_frontend_metric_aliases(self):
        day = timezone.now().date() - timedelta(days=1)
        ArticleBehaviorDaily.objects.create(
            organization=self.organization,
            article=self.article,
            date=day,
            pageviews=20,
            visitors=12,
            visits=15,
            engaged_30_count=10,
            scroll_50_count=8,
            scroll_90_count=5,
            cta_impression_count=6,
            cta_click_count=3,
        )
        ArticleSearchDaily.objects.create(
            organization=self.organization,
            article=self.article,
            date=day,
            country="",
            device="",
            clicks=Decimal("4"),
            impressions=Decimal("100"),
            ctr=Decimal("0.04"),
            position=Decimal("7"),
        )
        # Dimension grain must not be added to headline totals.
        ArticleSearchDaily.objects.create(
            organization=self.organization,
            article=self.article,
            date=day,
            country="AUS",
            device="MOBILE",
            clicks=Decimal("99"),
            impressions=Decimal("999"),
            ctr=Decimal("0.1"),
            position=Decimal("1"),
        )
        ArticleTrafficSourceDaily.objects.create(
            organization=self.organization,
            article=self.article,
            date=day,
            source_category="ai",
            source_name="ChatGPT",
            visits=5,
            cta_click_count=2,
            conversion_attribution_available=False,
        )

        payload = build_analytics_summary(
            self.organization,
            start_date=day,
            end_date=day,
        )

        self.assertEqual(payload["totals"]["searchClicks"], 4.0)
        self.assertEqual(payload["totals"]["searchImpressions"], 100.0)
        self.assertEqual(payload["totals"]["engaged30"], 10)
        self.assertEqual(payload["totals"]["scroll50"], 8)
        self.assertEqual(payload["totals"]["ctaImpressions"], 6)
        self.assertEqual(payload["totals"]["ctaClicks"], 3)
        self.assertEqual(payload["totals"]["ctaConversionRate"], 0.2)
        self.assertEqual(payload["sources"][0]["key"], "ai:ChatGPT")
        self.assertTrue(payload["sources"][0]["isAi"])
        self.assertIsNone(payload["sources"][0]["visitorToCtaRate"])

    def test_summary_and_article_endpoints_match_dashboard_shape(self):
        summary = self.client.get("/api/v1/vibe-marketing/analytics/summary?range=16m")
        detail = self.client.get(f"/api/v1/vibe-marketing/analytics/articles/{self.article.id}?range=28d")

        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["range"], "16m")
        for key in ("status", "totals", "articles", "sources", "freshness"):
            self.assertIn(key, summary.data)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["article"]["id"], str(self.article.id))
        self.assertEqual(detail.data["range"], "28d")

    def test_unsynced_search_and_denominator_free_rates_are_unavailable(self):
        day = timezone.now().date() - timedelta(days=1)

        payload = build_analytics_summary(self.organization, start_date=day, end_date=day)

        self.assertIsNone(payload["totals"]["searchClicks"])
        self.assertIsNone(payload["totals"]["searchImpressions"])
        self.assertIsNone(payload["totals"]["searchCtr"])
        self.assertIsNone(payload["totals"]["averagePosition"])
        self.assertIsNone(payload["totals"]["ctaConversionRate"])
        self.assertIsNone(payload["totals"]["ctaClickThroughRate"])

    @override_settings(CONTENT_ANALYTICS_GSC_QUERY_LIMIT=2)
    @patch("content_analytics.services.sync.service_for_search_console_property")
    def test_search_console_sync_batches_a_thirty_day_window_per_article(self, service_for_property):
        prop = SearchConsoleProperty.objects.create(
            organization=self.organization,
            site_url="sc-domain:example.com",
            status=SearchConsolePropertyStatus.VERIFIED,
            sync_enabled=True,
        )
        second_article = WrittenArticle.objects.create(
            organization=self.organization,
            title="Second tracked article",
            slug="second-tracked-article",
            category="featured",
            primary_keyword="second tracked keyword",
            publish_status=ArticlePublishStatus.LIVE,
            canonical_url="https://www.example.com/articles/second-tracked-article",
            canonical_path="/articles/second-tracked-article",
        )
        start_date = timezone.now().date() - timedelta(days=32)
        end_date = start_date + timedelta(days=29)
        ArticleAnalyticsLocation.objects.filter(
            article__in=[self.article, second_article]
        ).update(valid_from=start_date)
        search_analytics = MagicMock()
        service = MagicMock()
        service.searchanalytics.return_value = search_analytics
        search_analytics.query.return_value.execute.side_effect = [
            {
                "rows": [
                    {
                        "keys": [start_date.isoformat()],
                        "clicks": 9,
                        "impressions": 100,
                        "ctr": 0.09,
                        "position": 4.5,
                    }
                ]
            },
            {
                "rows": [
                    {
                        "keys": [start_date.isoformat(), "lower query"],
                        "clicks": 1,
                        "impressions": 20,
                        "ctr": 0.05,
                        "position": 8,
                    },
                    {
                        "keys": [start_date.isoformat(), "best query"],
                        "clicks": 5,
                        "impressions": 40,
                        "ctr": 0.125,
                        "position": 3,
                    },
                    {
                        "keys": [start_date.isoformat(), "middle query"],
                        "clicks": 3,
                        "impressions": 30,
                        "ctr": 0.1,
                        "position": 5,
                    },
                ]
            },
            {"rows": []},
            {"rows": []},
        ]
        service_for_property.return_value = service

        result = sync_search_console_property(
            prop,
            start_date=start_date,
            end_date=end_date,
        )

        self.assertEqual(result["daily_rows"], 60)
        self.assertEqual(search_analytics.query.call_count, 4)
        service_for_property.assert_called_once_with(prop)
        request_bodies = [call.kwargs["body"] for call in search_analytics.query.call_args_list]
        self.assertEqual(
            [body["dimensions"] for body in request_bodies],
            [["date"], ["date", "query"], ["date"], ["date", "query"]],
        )
        self.assertTrue(
            all(
                body["startDate"] == start_date.isoformat()
                and body["endDate"] == end_date.isoformat()
                for body in request_bodies
            )
        )
        requested_pages = [
            body["dimensionFilterGroups"][0]["filters"][0]["expression"]
            for body in request_bodies
        ]
        self.assertEqual(
            requested_pages,
            [self.article.canonical_url, self.article.canonical_url, second_article.canonical_url, second_article.canonical_url],
        )
        self.assertEqual(
            ArticleSearchDaily.objects.filter(
                organization=self.organization,
                date__range=(start_date, end_date),
            ).count(),
            60,
        )
        zero_day = ArticleSearchDaily.objects.get(
            article=self.article,
            date=start_date + timedelta(days=1),
            country="",
            device="",
        )
        self.assertEqual(zero_day.clicks, Decimal("0"))
        self.assertEqual(
            list(
                ArticleSearchQueryDaily.objects.filter(article=self.article, date=start_date)
                .order_by("-clicks")
                .values_list("query", flat=True)
            ),
            ["best query", "middle query"],
        )

    def test_location_history_splits_gsc_windows_and_preserves_stable_aggregate_owner(self):
        prop = SearchConsoleProperty.objects.create(
            organization=self.organization,
            site_url="sc-domain:example.com",
            status=SearchConsolePropertyStatus.VERIFIED,
            sync_enabled=True,
        )
        first_day = timezone.localdate() - timedelta(days=4)
        change_day = first_day + timedelta(days=2)
        end_day = change_day + timedelta(days=1)
        ArticleAnalyticsLocation.objects.filter(article=self.article).delete()
        ArticleAnalyticsLocation.objects.create(
            organization=self.organization,
            article=self.article,
            canonical_url="https://www.example.com/articles/old-slug",
            canonical_path="/articles/old-slug",
            valid_from=first_day,
            valid_to=change_day - timedelta(days=1),
        )
        ArticleAnalyticsLocation.objects.create(
            organization=self.organization,
            article=self.article,
            canonical_url="https://www.example.com/articles/tracked-article",
            canonical_path="/articles/tracked-article",
            valid_from=change_day,
        )
        search_analytics = MagicMock()
        service = MagicMock()
        service.searchanalytics.return_value = search_analytics
        search_analytics.query.return_value.execute.side_effect = [
            {"rows": []},
            {"rows": []},
            {"rows": []},
            {"rows": []},
        ]

        with patch(
            "content_analytics.services.sync.service_for_search_console_property",
            return_value=service,
        ):
            result = sync_search_console_property(prop, start_date=first_day, end_date=end_day)

        self.assertEqual(result["daily_rows"], 4)
        self.assertEqual(search_analytics.query.call_count, 4)
        bodies = [call.kwargs["body"] for call in search_analytics.query.call_args_list]
        self.assertEqual(
            [(body["startDate"], body["endDate"]) for body in bodies],
            [
                (first_day.isoformat(), (change_day - timedelta(days=1)).isoformat()),
                (first_day.isoformat(), (change_day - timedelta(days=1)).isoformat()),
                (change_day.isoformat(), end_day.isoformat()),
                (change_day.isoformat(), end_day.isoformat()),
            ],
        )
        self.assertEqual(
            [
                body["dimensionFilterGroups"][0]["filters"][0]["expression"]
                for body in bodies
            ],
            [
                "https://www.example.com/articles/old-slug",
                "https://www.example.com/articles/old-slug",
                self.article.canonical_url,
                self.article.canonical_url,
            ],
        )
        self.assertEqual(
            ArticleSearchDaily.objects.filter(article=self.article).count(),
            4,
        )

    def test_gsc_sync_does_not_zero_a_day_before_known_location_history(self):
        prop = SearchConsoleProperty.objects.create(
            organization=self.organization,
            site_url="sc-domain:example.com",
            status=SearchConsolePropertyStatus.VERIFIED,
            sync_enabled=True,
        )
        unknown_day = timezone.localdate() - timedelta(days=3)
        known_day = unknown_day + timedelta(days=1)
        location = ArticleAnalyticsLocation.objects.get(article=self.article, valid_to__isnull=True)
        location.valid_from = known_day
        location.save(update_fields=["valid_from", "updated_at"])
        ArticleSearchDaily.objects.create(
            organization=self.organization,
            article=self.article,
            date=unknown_day,
            clicks=9,
            impressions=10,
            ctr=Decimal("0.9"),
            position=1,
        )
        service = MagicMock()
        service.searchanalytics.return_value.query.return_value.execute.return_value = {"rows": []}

        with patch(
            "content_analytics.services.sync.service_for_search_console_property",
            return_value=service,
        ):
            sync_search_console_property(prop, start_date=unknown_day, end_date=known_day)

        historical = ArticleSearchDaily.objects.get(article=self.article, date=unknown_day)
        self.assertEqual(historical.clicks, Decimal("9"))

    def test_umami_sync_resolves_the_historical_path_for_each_day(self):
        first_day = timezone.localdate() - timedelta(days=2)
        second_day = first_day + timedelta(days=1)
        ArticleAnalyticsLocation.objects.filter(article=self.article).delete()
        ArticleAnalyticsLocation.objects.create(
            organization=self.organization,
            article=self.article,
            canonical_url="https://www.example.com/articles/old-slug",
            canonical_path="/articles/old-slug",
            valid_from=first_day,
            valid_to=first_day,
        )
        ArticleAnalyticsLocation.objects.create(
            organization=self.organization,
            article=self.article,
            canonical_url=self.article.canonical_url,
            canonical_path=self.article.canonical_path,
            valid_from=second_day,
        )
        client = MagicMock()
        client.fetch_article_day.return_value = UmamiArticleDay(
            stats={"pageviews": 1, "visitors": 1, "visits": 1, "bounces": 0, "umami_total_time": 0},
            milestones={
                "engaged_30_count": 0,
                "scroll_50_count": 0,
                "scroll_90_count": 0,
                "cta_impression_count": 0,
                "cta_click_count": 0,
                "outbound_click_count": 0,
            },
            referrers=[],
            source_attribution_complete=True,
        )

        sync_umami_site(self.site, start_date=first_day, end_date=second_day, client=client)

        self.assertEqual(
            [call.kwargs["path"] for call in client.fetch_article_day.call_args_list],
            ["/articles/old-slug", self.article.canonical_path],
        )
        self.assertEqual(ArticleBehaviorDaily.objects.filter(article=self.article).count(), 2)

    def test_same_day_location_change_updates_one_active_interval(self):
        today = timezone.localdate()
        location = ArticleAnalyticsLocation.objects.get(article=self.article, valid_to__isnull=True)
        self.article.canonical_url = "https://www.example.com/articles/renamed-today"
        self.article.canonical_path = "/articles/renamed-today"
        self.article.live_url = self.article.canonical_url
        self.article.save(update_fields=["canonical_url", "canonical_path", "live_url"])

        active = ArticleAnalyticsLocation.objects.get(article=self.article, valid_to__isnull=True)
        self.assertEqual(active.pk, location.pk)
        self.assertEqual(active.valid_from, today)
        self.assertEqual(active.canonical_path, "/articles/renamed-today")
        self.assertEqual(location_for_day(self.article, today).pk, active.pk)
        self.assertEqual(ArticleAnalyticsLocation.objects.filter(article=self.article).count(), 1)

    def test_effective_dated_reconciliation_closes_the_previous_location(self):
        first_day = timezone.localdate() - timedelta(days=5)
        change_day = first_day + timedelta(days=3)
        ArticleAnalyticsLocation.objects.filter(article=self.article).delete()
        old_location = record_article_location(self.article, effective_on=first_day)
        WrittenArticle.objects.filter(pk=self.article.pk).update(
            canonical_url="https://www.example.com/articles/later-slug",
            canonical_path="/articles/later-slug",
            live_url="https://www.example.com/articles/later-slug",
        )
        self.article.refresh_from_db()

        new_location = record_article_location(self.article, effective_on=change_day)

        old_location.refresh_from_db()
        self.assertEqual(old_location.valid_to, change_day - timedelta(days=1))
        self.assertEqual(new_location.valid_from, change_day)
        self.assertIsNone(new_location.valid_to)
        self.assertEqual(
            ArticleAnalyticsLocation.objects.filter(article=self.article, valid_to__isnull=True).count(),
            1,
        )

    @patch("content_factory.vibe_marketing_views._queue_content_factory_run")
    @patch("content_factory.vibe_marketing_views._require_roo_points_for_ai_agent")
    def test_article_setup_payload_contains_existing_article_manifest(self, require_points, queue):
        captured = {}
        require_points.return_value = (None, 100)

        def fake_queue(**kwargs):
            captured.update(kwargs["payload"])
            return ContentFactoryRun.objects.create(
                run_id="analytics-setup-manifest",
                workflow="article_system_setup",
                domain=self.organization.domain,
                status=ContentFactoryRunStatus.QUEUED,
                run_request=kwargs["payload"],
                result={},
            )

        queue.side_effect = fake_queue
        response = self.client.post(
            "/api/v1/vibe-marketing/article-system-setup/",
            {
                "articleSurfaceMode": "existing",
                "articleSurfaceUrl": "https://www.example.com/articles",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(
            captured["analytics_article_manifest"],
            [
                {
                    "slug": f"featured/{self.article.slug}",
                    "analytics_article_id": str(self.article.analytics_id),
                }
            ],
        )

    @patch("content_factory.vibe_marketing_views._queue_content_factory_run")
    @patch("content_factory.vibe_marketing_views._charge_roo_points_for_article")
    def test_article_request_allocates_uuid_before_dispatch(self, charge, queue):
        captured = {}

        def fake_charge(request, *, context, payload):
            return self.user, None, dict(payload), None

        def fake_queue(**kwargs):
            captured.update(kwargs["payload"])
            return SimpleNamespace(run_id="analytics-article-run", status=ContentFactoryRunStatus.QUEUED)

        charge.side_effect = fake_charge
        queue.side_effect = fake_queue
        response = self.client.post(
            "/api/v1/vibe-marketing/article/",
            {"customTitle": "A new tracked article", "targetKeyword": "new tracked keyword"},
            format="json",
        )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(str(uuid.UUID(captured["analytics_article_id"])), captured["analytics_article_id"])
        self.assertEqual(captured["analytics_config"]["analytics_article_id"], captured["analytics_article_id"])
        self.assertEqual(captured["analytics_config"]["website_id"], self.site.external_website_id)


@override_settings(**ANALYTICS_SETTINGS)
class ArticleAnalyticsPersistenceTests(TestCase):
    def test_completed_run_persists_exact_generated_identity_and_canonical_location(self):
        organization = Organization.objects.create(name="Persist Co", domain="persist.example")
        analytics_id = uuid.uuid4()
        run = ContentFactoryRun.objects.create(
            run_id="persist-analytics-run",
            workflow="article_generation",
            domain=organization.domain,
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"analytics_article_id": str(analytics_id)},
            result={},
        )
        with patch(
            "content_factory.vibe_marketing_views._content_package_from_run",
            return_value={
                "contentPackaged": True,
                "title": "Persisted analytics",
                "slug": "persisted-analytics",
                "targetKeyword": "persisted analytics",
                "canonicalUrl": "https://persist.example/articles/persisted-analytics",
            },
        ), patch(
            "content_factory.vibe_marketing_views._publish_evidence_from_run",
            return_value={},
        ):
            article = _persist_article_memory_from_run(organization=organization, run=run)

        self.assertEqual(article.analytics_id, analytics_id)
        self.assertEqual(article.canonical_url, "https://persist.example/articles/persisted-analytics")
        self.assertEqual(article.canonical_path, "/articles/persisted-analytics")

    def test_later_run_cannot_replace_an_existing_article_analytics_identity(self):
        organization = Organization.objects.create(name="Stable Co", domain="stable.example")
        original_id = uuid.uuid4()
        article = WrittenArticle.objects.create(
            organization=organization,
            analytics_id=original_id,
            title="Stable analytics",
            slug="stable-analytics",
            category="featured",
            primary_keyword="stable analytics",
        )
        run = ContentFactoryRun.objects.create(
            run_id="persist-conflicting-analytics-run",
            workflow="article_generation",
            domain=organization.domain,
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"analytics_article_id": str(uuid.uuid4())},
            result={},
        )
        with patch(
            "content_factory.vibe_marketing_views._content_package_from_run",
            return_value={
                "contentPackaged": True,
                "title": "Stable analytics revised",
                "slug": "stable-analytics",
                "targetKeyword": "stable analytics",
                "canonicalUrl": "https://stable.example/articles/stable-analytics",
            },
        ), patch(
            "content_factory.vibe_marketing_views._publish_evidence_from_run",
            return_value={},
        ):
            persisted = _persist_article_memory_from_run(organization=organization, run=run)

        article.refresh_from_db()
        self.assertEqual(persisted.pk, article.pk)
        self.assertEqual(article.analytics_id, original_id)

    def test_location_migration_backfills_the_existing_canonical_lifetime(self):
        organization = Organization.objects.create(name="Legacy Co", domain="legacy.example")
        article = WrittenArticle.objects.create(
            organization=organization,
            title="Legacy article",
            slug="legacy-article",
            category="featured",
            primary_keyword="legacy keyword",
            publish_status=ArticlePublishStatus.LIVE,
            canonical_url="https://www.legacy.example/articles/legacy-article?ignored=1#fragment",
            canonical_path="/articles/legacy-article",
        )
        ArticleAnalyticsLocation.objects.filter(article=article).delete()
        migration = importlib.import_module(
            "content_analytics.migrations.0002_article_analytics_location_history"
        )

        migration.backfill_article_locations(django_apps, None)

        location = ArticleAnalyticsLocation.objects.get(article=article)
        self.assertEqual(location.canonical_url, "https://www.legacy.example/articles/legacy-article")
        self.assertEqual(location.canonical_path, "/articles/legacy-article")
        self.assertEqual(location.valid_from, timezone.localtime(article.created_at).date())
        self.assertEqual(location.source, "migration")


@override_settings(**ANALYTICS_SETTINGS)
class UmamiClientContractTests(TestCase):
    def test_provisioning_forces_replay_and_heatmaps_off(self):
        website_id = "876b328e-1306-4b4e-b0c6-7e7d03597fb9"
        client = UmamiClient()
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": []},
                {"id": website_id, "domain": "example.com"},
                {"id": website_id, "domain": "example.com"},
            ],
        ) as request:
            result = client.ensure_website(name="Example", domain="example.com")

        self.assertEqual(result["id"], website_id)
        update_body = request.call_args_list[2].kwargs["json"]
        self.assertFalse(update_body["replayEnabled"])
        self.assertFalse(update_body["replayConfig"]["replayEnabled"])
        self.assertFalse(update_body["replayConfig"]["heatmapEnabled"])
        self.assertEqual(update_body["replayConfig"]["sampleRate"], 0)
        self.assertEqual(update_body["replayConfig"]["heatmapSampleRate"], 0)


@override_settings(**ANALYTICS_SETTINGS, CONTENT_ANALYTICS_SYNC_INTERVAL_SECONDS=86400)
class AnalyticsSchedulerFairnessTests(TestCase):
    @patch("content_analytics.services.sync.sync_organization_analytics")
    def test_due_sites_are_oldest_first_and_recent_site_is_not_reselected(self, sync):
        now = timezone.now()
        organizations = [
            Organization.objects.create(name=f"Org {index}", domain=f"org-{index}.example")
            for index in range(3)
        ]
        AnalyticsSite.objects.create(
            organization=organizations[0],
            domain=organizations[0].domain,
            external_website_id=str(uuid.uuid4()),
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
            last_synced_at=None,
        )
        AnalyticsSite.objects.create(
            organization=organizations[1],
            domain=organizations[1].domain,
            external_website_id=str(uuid.uuid4()),
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
            last_synced_at=now - timedelta(days=3),
        )
        AnalyticsSite.objects.create(
            organization=organizations[2],
            domain=organizations[2].domain,
            external_website_id=str(uuid.uuid4()),
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
            last_synced_at=now,
        )
        sync.return_value = {"status": "succeeded"}

        sync_due_analytics(source="umami", limit=2)

        selected = [call.args[0].id for call in sync.call_args_list]
        self.assertEqual(selected, [organizations[0].id, organizations[1].id])

    @override_settings(
        CONTENT_ANALYTICS_SYNC_LOOKBACK_DAYS=3,
        CONTENT_ANALYTICS_SYNC_MAX_BACKFILL_DAYS_PER_RUN=2,
        CONTENT_ANALYTICS_UMAMI_RETENTION_DAYS=120,
    )
    @patch("content_analytics.services.sync.sync_umami_site")
    def test_first_umami_sync_seeds_bounded_catchup_from_site_creation(self, sync_site):
        organization = Organization.objects.create(name="First catchup", domain="first-catchup.example")
        site = AnalyticsSite.objects.create(
            organization=organization,
            domain=organization.domain,
            external_website_id=str(uuid.uuid4()),
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
        )
        end_date = timezone.localdate() - timedelta(days=1)
        first_available = end_date - timedelta(days=10)
        AnalyticsSite.objects.filter(pk=site.pk).update(created_at=timezone.now() - timedelta(days=11))
        sync_site.return_value = {"source": "umami", "status": "succeeded"}

        result = sync_organization_analytics(
            organization,
            source=AnalyticsSyncSource.UMAMI,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(sync_site.call_count, 2)
        rolling_call, catchup_call = sync_site.call_args_list
        self.assertEqual(rolling_call.kwargs["start_date"], end_date - timedelta(days=2))
        self.assertEqual(rolling_call.kwargs["end_date"], end_date)
        # auto_now_add is updated directly above to exactly eleven days before
        # today, which is ten days before the latest complete Umami day.
        self.assertEqual(catchup_call.kwargs["start_date"], first_available)
        self.assertEqual(catchup_call.kwargs["end_date"], first_available + timedelta(days=1))

    @override_settings(
        CONTENT_ANALYTICS_SYNC_LOOKBACK_DAYS=3,
        CONTENT_ANALYTICS_SYNC_MAX_BACKFILL_DAYS_PER_RUN=2,
        CONTENT_ANALYTICS_UMAMI_RETENTION_DAYS=120,
    )
    @patch("content_analytics.services.sync.sync_umami_site")
    def test_umami_outage_gap_is_recovered_in_bounded_chunks(self, sync_site):
        organization = Organization.objects.create(name="Catchup", domain="catchup.example")
        site = AnalyticsSite.objects.create(
            organization=organization,
            domain=organization.domain,
            external_website_id=str(uuid.uuid4()),
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
        )
        end_date = timezone.now().date() - timedelta(days=1)
        AnalyticsSyncState.objects.create(
            organization=organization,
            source=AnalyticsSyncSource.UMAMI,
            synced_through=end_date - timedelta(days=10),
        )
        sync_site.return_value = {"source": "umami", "status": "succeeded"}

        result = sync_organization_analytics(
            organization,
            source=AnalyticsSyncSource.UMAMI,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(sync_site.call_count, 2)
        rolling_call, catchup_call = sync_site.call_args_list
        self.assertEqual(rolling_call.kwargs["start_date"], end_date - timedelta(days=2))
        self.assertEqual(rolling_call.kwargs["end_date"], end_date)
        self.assertEqual(catchup_call.kwargs["start_date"], end_date - timedelta(days=9))
        self.assertEqual(catchup_call.kwargs["end_date"], end_date - timedelta(days=8))
        state = AnalyticsSyncState.objects.get(
            organization=organization,
            source=AnalyticsSyncSource.UMAMI,
        )
        self.assertEqual(state.cursor["umami_catchup_next"], (end_date - timedelta(days=7)).isoformat())
        self.assertFalse(state.cursor["umami_catchup_complete"])
        site.refresh_from_db()
        self.assertLess(site.last_synced_at, timezone.now() - timedelta(hours=23))
