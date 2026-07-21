from __future__ import annotations

import json
from datetime import date

from django.test import TestCase, override_settings

from content_analytics.models import (
    AnalyticsSite,
    AnalyticsSyncSource,
    AnalyticsSyncState,
    ArticleBehaviorDaily,
    ArticlePerformanceReport,
    ArticlePerformanceReportCategory,
    ArticleSearchDaily,
    ArticleTrafficSourceDaily,
    SearchConsoleProperty,
    SearchConsolePropertyStatus,
)
from content_analytics.services.reports import (
    build_article_performance_payload,
    generate_article_performance_report,
    report_windows,
)
from content_factory.models import ArticlePublishStatus, WrittenArticle
from organizations.models import Organization

REPORT_SETTINGS = {
    "CONTENT_ANALYTICS_REPORT_WINDOW_DAYS": 7,
    "CONTENT_ANALYTICS_REPORT_MIN_VISITS": 20,
    "CONTENT_ANALYTICS_REPORT_TOP_CONVERSION_RATE": 0.03,
    "CONTENT_ANALYTICS_REPORT_HIGH_ENGAGED_RATE": 0.40,
    "CONTENT_ANALYTICS_REPORT_LOW_CTA_REACH_RATE": 0.50,
}

REPORT_DATE = date(2026, 7, 21)


@override_settings(**REPORT_SETTINGS)
class ArticlePerformanceReportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Brief Co", domain="brief.example")
        AnalyticsSite.objects.create(
            organization=self.organization,
            domain="brief.example",
            enabled=True,
        )

    def _article(self, slug: str, *, title: str | None = None, publish_status=ArticlePublishStatus.LIVE):
        return WrittenArticle.objects.create(
            organization=self.organization,
            title=title or slug.replace("-", " ").title(),
            slug=slug,
            category="featured",
            primary_keyword=slug,
            publish_status=publish_status,
            live_url=f"https://brief.example/articles/{slug}",
            canonical_url=f"https://brief.example/articles/{slug}",
            canonical_path=f"/articles/{slug}",
        )

    def _behavior(self, article, day, **counts):
        defaults = {
            "pageviews": counts.get("visits", 0),
            "visitors": counts.get("visits", 0),
        }
        defaults.update(counts)
        return ArticleBehaviorDaily.objects.create(
            organization=self.organization,
            article=article,
            date=day,
            **defaults,
        )

    def test_report_windows_bounds(self):
        windows = report_windows(REPORT_DATE)
        self.assertEqual(windows.window_start, date(2026, 7, 14))
        self.assertEqual(windows.window_end, date(2026, 7, 20))
        self.assertEqual(windows.prior_window_start, date(2026, 7, 7))
        self.assertEqual(windows.prior_window_end, date(2026, 7, 13))

    def test_headline_totals_and_deltas(self):
        article = self._article("headline")
        self._behavior(
            article,
            date(2026, 7, 14),
            visits=40,
            engaged_30_count=10,
            cta_impression_count=30,
            cta_click_count=2,
        )
        self._behavior(
            article,
            date(2026, 7, 20),
            visits=60,
            engaged_30_count=20,
            cta_impression_count=40,
            cta_click_count=4,
        )
        # Prior window and out-of-window rows must not leak into totals.
        self._behavior(article, date(2026, 7, 13), visits=30, cta_click_count=3)
        self._behavior(article, date(2026, 7, 21), visits=999)

        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        headline = payload["headline"]
        self.assertEqual(headline["humanVisits"], 100)
        self.assertEqual(headline["ctaClickers"], 6)
        self.assertEqual(headline["engagedReaderRate"], 0.3)
        self.assertEqual(headline["ctaConversionRate"], 0.06)
        self.assertEqual(headline["visitsDelta"], 70)
        self.assertEqual(headline["ctaClickersDelta"], 3)
        self.assertEqual(payload["deltas"]["ctaConversionRate"], round(0.06 - 0.1, 6))
        self.assertEqual(payload["window"], {"start": "2026-07-14", "end": "2026-07-20", "days": 7})
        self.assertEqual(
            payload["priorWindow"], {"start": "2026-07-07", "end": "2026-07-13", "days": 7}
        )

    def test_categories_cover_all_branches(self):
        top = self._article("top-performer")
        self._behavior(
            top,
            date(2026, 7, 15),
            visits=100,
            engaged_30_count=30,
            cta_impression_count=80,
            cta_click_count=5,
        )
        high = self._article("high-interest")
        self._behavior(
            high,
            date(2026, 7, 15),
            visits=100,
            engaged_30_count=50,
            cta_impression_count=20,
            cta_click_count=1,
        )
        needs = self._article("needs-attention")
        self._behavior(
            needs,
            date(2026, 7, 15),
            visits=100,
            engaged_30_count=10,
            cta_impression_count=60,
            cta_click_count=0,
        )
        gathering = self._article("gathering-data")
        self._behavior(gathering, date(2026, 7, 15), visits=5)

        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        by_slug = {row["slug"]: row for row in payload["articles"]}

        self.assertEqual(by_slug["top-performer"]["category"], ArticlePerformanceReportCategory.TOP_PERFORMER)
        self.assertIn("Strong landing-to-CTA conversion", by_slug["top-performer"]["reasons"][0])

        self.assertEqual(by_slug["high-interest"]["category"], ArticlePerformanceReportCategory.HIGH_INTEREST)
        self.assertIn("Readers engage", by_slug["high-interest"]["reasons"][0])
        # 20% CTA reach is below the 50% threshold, so the placement reason appears too.
        self.assertTrue(
            any("CTA becomes visible" in reason for reason in by_slug["high-interest"]["reasons"])
        )

        self.assertEqual(by_slug["needs-attention"]["category"], ArticlePerformanceReportCategory.NEEDS_ATTENTION)
        self.assertEqual(by_slug["gathering-data"]["category"], ArticlePerformanceReportCategory.GATHERING_DATA)
        self.assertIn("not enough data", by_slug["gathering-data"]["reasons"][0])

        self.assertEqual(
            payload["categoriesSummary"],
            {
                "top_performer": 1,
                "high_interest": 1,
                "needs_attention": 1,
                "gathering_data": 1,
            },
        )
        # Ordered by window visits descending; the zero-visit article sorts last.
        self.assertEqual(payload["articles"][-1]["slug"], "gathering-data")

    def test_live_zero_traffic_and_nonlive_with_traffic_included(self):
        self._article("silent-live")
        retired = self._article("retired", publish_status=ArticlePublishStatus.MERGED)
        self._behavior(retired, date(2026, 7, 16), visits=25, engaged_30_count=5)

        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        by_slug = {row["slug"]: row for row in payload["articles"]}
        self.assertIn("silent-live", by_slug)
        self.assertEqual(by_slug["silent-live"]["category"], ArticlePerformanceReportCategory.GATHERING_DATA)
        self.assertIn("retired", by_slug)
        self.assertEqual(by_slug["retired"]["publishStatus"], ArticlePublishStatus.MERGED)

    def test_source_mix_and_per_article_source_visits(self):
        article = self._article("sourced")
        self._behavior(article, date(2026, 7, 15), visits=50)
        ArticleTrafficSourceDaily.objects.create(
            organization=self.organization,
            article=article,
            date=date(2026, 7, 15),
            source_category="search",
            source_name="google.com",
            visits=30,
            pageviews=32,
        )
        ArticleTrafficSourceDaily.objects.create(
            organization=self.organization,
            article=article,
            date=date(2026, 7, 16),
            source_category="ai",
            source_name="chatgpt.com",
            visits=8,
            pageviews=8,
        )
        # Out-of-window rows are ignored.
        ArticleTrafficSourceDaily.objects.create(
            organization=self.organization,
            article=article,
            date=date(2026, 7, 13),
            source_category="search",
            source_name="google.com",
            visits=99,
        )

        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        self.assertEqual(
            payload["sources"][0],
            {"category": "search", "visits": 30, "pageviews": 32, "ctaClickVisits": 0, "isAi": False},
        )
        self.assertEqual(payload["sources"][1]["category"], "ai")
        self.assertTrue(payload["sources"][1]["isAi"])
        row = {item["slug"]: item for item in payload["articles"]}["sourced"]
        self.assertEqual(row["searchVisits"], 30)
        self.assertEqual(row["aiVisits"], 8)

    def test_search_block_connected_vs_not(self):
        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        self.assertEqual(payload["search"], {"connected": False, "syncEnabled": False})

        article = self._article("searched")
        SearchConsoleProperty.objects.create(
            organization=self.organization,
            site_url="sc-domain:brief.example",
            status=SearchConsolePropertyStatus.VERIFIED,
        )
        ArticleSearchDaily.objects.create(
            organization=self.organization,
            article=article,
            date=date(2026, 7, 15),
            clicks=10,
            impressions=200,
            position=4,
        )
        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        self.assertTrue(payload["search"]["connected"])
        self.assertEqual(payload["search"]["searchClicks"], 10.0)
        self.assertEqual(payload["search"]["searchImpressions"], 200.0)
        self.assertEqual(payload["search"]["searchCtr"], 0.05)
        self.assertEqual(payload["search"]["dataThrough"], "2026-07-15")

    def test_data_through_from_sync_state_adds_note(self):
        article = self._article("lagging")
        self._behavior(article, date(2026, 7, 15), visits=30)
        AnalyticsSyncState.objects.create(
            organization=self.organization,
            source=AnalyticsSyncSource.UMAMI,
            synced_through=date(2026, 7, 18),
        )
        report, created = generate_article_performance_report(self.organization, REPORT_DATE)
        self.assertTrue(created)
        self.assertEqual(report.data_through_date, date(2026, 7, 18))
        self.assertTrue(
            any("had not synced yet" in note for note in report.payload["notes"])
        )
        # A synced_through beyond the window is clamped to the window end.
        AnalyticsSyncState.objects.filter(organization=self.organization).update(
            synced_through=date(2026, 7, 25)
        )
        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        self.assertEqual(payload["dataThroughDate"], "2026-07-20")

    def test_immutable_unless_forced(self):
        article = self._article("frozen")
        self._behavior(article, date(2026, 7, 15), visits=40, cta_click_count=2)

        report, created = generate_article_performance_report(self.organization, REPORT_DATE)
        self.assertTrue(created)
        original_visits = report.payload["headline"]["humanVisits"]
        self.assertEqual(original_visits, 40)

        # Underlying data changes; the stored snapshot must not.
        self._behavior(article, date(2026, 7, 16), visits=60)
        again, created_again = generate_article_performance_report(self.organization, REPORT_DATE)
        self.assertFalse(created_again)
        self.assertEqual(again.pk, report.pk)
        self.assertEqual(again.payload["headline"]["humanVisits"], 40)
        self.assertEqual(ArticlePerformanceReport.objects.count(), 1)

        forced, forced_created = generate_article_performance_report(
            self.organization, REPORT_DATE, force=True
        )
        self.assertFalse(forced_created)
        self.assertEqual(forced.pk, report.pk)
        self.assertEqual(forced.payload["headline"]["humanVisits"], 100)
        self.assertEqual(ArticlePerformanceReport.objects.count(), 1)

    def test_payload_is_json_serializable_and_has_bot_note(self):
        article = self._article("serializable")
        self._behavior(article, date(2026, 7, 15), visits=10)
        payload = build_article_performance_payload(self.organization, REPORT_DATE)
        encoded = json.dumps(payload)
        self.assertIn("Known bots are excluded at collection", encoded)
        self.assertEqual(payload["schemaVersion"], 1)
