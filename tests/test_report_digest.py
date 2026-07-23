"""Digest of the latest article-performance report (feeds the daily reminder)."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from content_analytics.models import ArticlePerformanceReport
from content_analytics.services.report_digest import latest_report_digest
from organizations.models import Organization


def _article_row(**overrides):
    row = {
        "title": "Guide to AI Evals",
        "slug": "guide-to-ai-evals",
        "canonicalUrl": "https://digest.example.com/articles/guide-to-ai-evals",
        "metrics": {
            "visits": 5,
            "engaged30Visits": 3,
            "engagedReaderRate": 0.6,
            "ctaClickVisits": 2,
            "ctaConversionRate": 0.4,
        },
        "priorVisits": 2,
        "visitsDelta": 3,
        "category": "top_performer",
        "categoryLabel": "Top performer",
        "reasons": ["Strong landing-to-CTA conversion (40.0% of visits click a CTA)."],
    }
    metrics = overrides.pop("metrics", None)
    if metrics is not None:
        row["metrics"] = {**row["metrics"], **metrics}
    row.update(overrides)
    return row


def _payload(articles, *, human_visits=11, visits_delta=4):
    return {
        "schemaVersion": 1,
        "window": {"start": "2026-07-15", "end": "2026-07-21", "days": 7},
        "headline": {
            "humanVisits": human_visits,
            "engagedReaderRate": 0.4545,
            "ctaClickers": 2,
            "ctaConversionRate": 0.1818,
            "visitsDelta": visits_delta,
        },
        "articles": articles,
    }


class ReportDigestTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Digest Co", domain="digest.example.com")

    def _create_report(self, payload, *, report_date=None):
        report_date = report_date or timezone.localdate()
        return ArticlePerformanceReport.objects.create(
            organization=self.org,
            report_date=report_date,
            window_start=report_date - timedelta(days=7),
            window_end=report_date - timedelta(days=1),
            prior_window_start=report_date - timedelta(days=14),
            prior_window_end=report_date - timedelta(days=8),
            payload=payload,
        )

    def test_no_report_returns_none(self):
        self.assertIsNone(latest_report_digest(self.org))

    def test_stale_report_returns_none(self):
        self._create_report(
            _payload([_article_row()]),
            report_date=timezone.localdate() - timedelta(days=4),
        )
        self.assertIsNone(latest_report_digest(self.org))

    @override_settings(FOUNDER_TOOLS_URL="https://mlai.au")
    def test_digest_summarizes_top_and_attention_pages(self):
        # Payload order is visits-desc, matching the report builder; the most
        # visited page can be the one needing attention — it should appear in
        # both lists.
        articles = [
            _article_row(
                title="Leaky Landing",
                canonicalUrl="https://digest.example.com/articles/leaky-landing",
                metrics={
                    "visits": 25,
                    "engaged30Visits": 1,
                    "engagedReaderRate": 0.04,
                    "ctaClickVisits": 0,
                },
                priorVisits=30,
                visitsDelta=-5,
                category="needs_attention",
                categoryLabel="Needs attention",
                reasons=[
                    "Visitors arrive but leave without engaging "
                    "(4% reach 30 active seconds, 0.0% CTA conversion)."
                ],
            ),
            _article_row(),
            _article_row(
                title="Quiet Post",
                canonicalUrl="http://digest.example.com/articles/quiet-post",
                metrics={
                    "visits": 3,
                    "engaged30Visits": 2,
                    "engagedReaderRate": 2 / 3,
                    "ctaClickVisits": 0,
                },
                priorVisits=0,
                visitsDelta=3,
                category="high_interest",
                categoryLabel="High-interest opportunity",
                reasons=["Readers engage but few click a CTA."],
            ),
            _article_row(
                title="Fresh Draft",
                metrics={"visits": 0, "engaged30Visits": 0, "ctaClickVisits": 0},
                priorVisits=0,
                visitsDelta=0,
                category="gathering_data",
                categoryLabel="Gathering data",
                reasons=["Only 0 visits in the last 7 days."],
            ),
        ]
        self._create_report(_payload(articles))

        digest = latest_report_digest(self.org)

        self.assertEqual(digest["domain"], "digest.example.com")
        self.assertEqual(digest["window_days"], 7)
        self.assertEqual(
            digest["summary_line"],
            "11 visits (+4 vs prior) · 45% engaged · 2 CTA clickers",
        )

        titles = [page["title"] for page in digest["top_pages"]]
        self.assertEqual(titles, ["Leaky Landing", "Guide to AI Evals", "Quiet Post"])
        self.assertEqual([page["rank"] for page in digest["top_pages"]], [1, 2, 3])
        self.assertEqual(
            digest["top_pages"][0]["summary"], "25 visits (-5) · 4% engaged"
        )
        self.assertEqual(
            digest["top_pages"][1]["summary"],
            "5 visits (+3) · 60% engaged · 2 CTA clicks · Top performer",
        )
        # No prior traffic → no delta; category label carried for high interest.
        self.assertEqual(
            digest["top_pages"][2]["summary"],
            "3 visits · 67% engaged · High-interest opportunity",
        )
        # Only absolute https URLs survive into messages.
        self.assertEqual(
            digest["top_pages"][0]["url"],
            "https://digest.example.com/articles/leaky-landing",
        )
        self.assertEqual(digest["top_pages"][2]["url"], "")

        self.assertEqual(len(digest["attention_pages"]), 1)
        self.assertEqual(digest["attention_pages"][0]["title"], "Leaky Landing")
        self.assertIn("leave without engaging", digest["attention_pages"][0]["summary"])
        self.assertEqual(digest["extra_attention_count"], 0)
        self.assertEqual(digest["brief_url"], "https://mlai.au/founder-tools/marketing#analytics")

    def test_zero_visit_report_reads_as_gathering(self):
        articles = [
            _article_row(
                metrics={"visits": 0, "engaged30Visits": 0, "ctaClickVisits": 0},
                priorVisits=0,
                visitsDelta=0,
                category="gathering_data",
                categoryLabel="Gathering data",
            )
        ]
        self._create_report(_payload(articles, human_visits=0, visits_delta=0))

        digest = latest_report_digest(self.org)

        self.assertEqual(digest["summary_line"], "No measured visits in the last 7 days yet.")
        self.assertEqual(digest["top_pages"], [])
        self.assertEqual(digest["attention_pages"], [])

    def test_attention_overflow_is_counted_not_listed(self):
        articles = [
            _article_row(
                title=f"Slipping Page {index}",
                metrics={"visits": 30 - index, "engaged30Visits": 1, "ctaClickVisits": 0},
                category="needs_attention",
                categoryLabel="Needs attention",
                reasons=["Visitors arrive but leave without engaging."],
            )
            for index in range(5)
        ]
        self._create_report(_payload(articles))

        digest = latest_report_digest(self.org)

        self.assertEqual(len(digest["attention_pages"]), 3)
        self.assertEqual(digest["extra_attention_count"], 2)

    def test_latest_report_wins(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self._create_report(_payload([], human_visits=1), report_date=yesterday)
        self._create_report(_payload([], human_visits=9))

        digest = latest_report_digest(self.org)

        self.assertEqual(digest["report_date"], timezone.localdate().isoformat())
        self.assertIn("9 visits", digest["summary_line"])
