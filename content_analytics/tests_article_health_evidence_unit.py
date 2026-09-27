"""Health-evidence regressions with no database, network or provider credentials."""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from content_analytics.services.article_health_evidence import (
    build_article_health_evidence,
    research_evidence_bundles,
)


AS_OF = date(2026, 9, 27)


def article(**changes):
    return SimpleNamespace(**{
        "id": "article-1", "primary_keyword": "urban trees", "source_run_id": "writing-1",
        "canonical_url": "https://own.example/articles/urban-trees", "live_url": "",
        "published_at": datetime(2025, 1, 4, tzinfo=timezone.utc), **changes,
    })


def observation(**changes):
    return {
        "query": "Urban   Trees", "observedAt": "2026-09-25T12:30:00Z",
        "locationCode": 2036, "languageCode": "en", "device": "desktop",
        "ownRankings": [{"url": article().canonical_url, "position": 8}],
        "competitors": [{
            "url": "https://other.example/tree-guide", "position": 2,
            "publishedAt": "2026-03-01", "updatedAt": "2026-09-10",
        }], **changes,
    }


class ArticleHealthEvidenceUnitTests(unittest.TestCase):
    def test_url_references_do_not_invent_rank_age_or_intent(self):
        result = build_article_health_evidence(article(), keywords=[{
            "keyword": "urban trees", "competitor_urls": ["https://other.example/guide"],
        }], as_of=AS_OF)
        self.assertIsNone(result["publishedAt"])
        self.assertIsNone(result["firstKnownLiveAt"])
        self.assertIsNone(result["updatedAt"])
        row = result["competitors"][0]
        for key in ("position", "ownPosition", "publishedAt", "updatedAt", "observedAt"):
            self.assertIsNone(row[key])
        self.assertFalse(row["intentMatch"])

    def test_stored_provider_bundle_matches_exact_article_not_just_domain(self):
        result = build_article_health_evidence(article(), observations=[observation()], as_of=AS_OF)
        row = result["competitors"][0]
        self.assertEqual((row["position"], row["ownPosition"]), (2, 8))
        self.assertEqual(row["observedAt"], "2026-09-25")
        self.assertEqual(row["updatedAt"], "2026-09-10")
        self.assertEqual(row["locationCode"], result["searchLocationCode"])
        self.assertEqual(result["searchLanguageCode"], "en")
        result = build_article_health_evidence(article(), observations=[observation(
            ownRankings=[{"url": "https://own.example/another-article", "position": 8}],
        )], as_of=AS_OF)
        self.assertIsNone(result["competitors"][0]["ownPosition"])

    def test_future_or_mismatched_observations_cannot_borrow_own_rank(self):
        for overrides in (
            {"observedAt": "2026-10-01"},
            {"query": "unrelated search"},
        ):
            raw = {"url": "https://other.example/guide", "position": 1, **overrides}
            result = build_article_health_evidence(
                article(), observations=[observation(competitors=[raw])], as_of=AS_OF,
            )
            self.assertIsNone(result["competitors"][0]["ownPosition"])
        self.assertIsNone(build_article_health_evidence(
            article(), observations=[observation(observedAt="2026-10-01")], as_of=AS_OF,
        )["competitors"][0]["position"])

    def test_wrong_article_or_query_is_excluded(self):
        for fields in ({"articleId": "another"}, {"articleUrl": "https://own.example/another"}, {"query": "other"}):
            result = build_article_health_evidence(article(), observations=[observation(**fields)], as_of=AS_OF)
            self.assertEqual(result["competitors"], [])

    def test_locale_mismatch_never_borrows_own_rank(self):
        for key, wrong in (("locationCode", 2840), ("languageCode", "fr"), ("device", "mobile")):
            for collection in ("ownRankings", "competitors"):
                bundle = observation()
                bundle[collection][0][key] = wrong
                result = build_article_health_evidence(article(), observations=[bundle], as_of=AS_OF)
                self.assertIsNone(result["competitors"][0]["ownPosition"])

    def test_content_dates_cannot_postdate_rank_or_the_report_snapshot(self):
        for changed in ({"updatedAt": "2026-09-26"}, {"dateObservedAt": "2026-10-01"}):
            bundle = observation()
            bundle["competitors"][0].update(changed)
            result = build_article_health_evidence(article(), observations=[bundle], as_of=AS_OF)
            self.assertIsNone(result["competitors"][0]["updatedAt"])
            self.assertEqual(result["competitors"][0]["position"], 2)

    def test_exact_own_page_dates_and_latest_complete_locale_are_preserved(self):
        bundle = observation(ownRankings=[{
            "url": article().canonical_url, "position": 8,
            "publishedAt": "2025-03-01", "updatedAt": "2026-09-10", "dateObservedAt": "2026-09-26",
        }])
        result = build_article_health_evidence(article(), observations=[
            observation(observedAt="2026-09-01", locationCode=2840), bundle,
        ], as_of=AS_OF)
        self.assertEqual(result["updatedAt"], "2026-09-10")
        self.assertEqual(result["publishedAt"], "2025-03-01")
        self.assertEqual(result["searchLocationCode"], 2036)

    def test_live_verification_is_a_separate_lower_bound_not_publication_date(self):
        result = build_article_health_evidence(article(
            published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            live_verified_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        ), as_of=AS_OF)
        self.assertIsNone(result["publishedAt"])
        self.assertEqual(result["firstKnownLiveAt"], "2026-01-10")
        result = build_article_health_evidence(article(
            live_verified_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        ), as_of=AS_OF)
        self.assertIsNone(result["firstKnownLiveAt"])

    def test_invalid_urls_internal_pages_and_invalid_ranks_stay_out(self):
        rows = [
            {"url": url, "position": 2}
            for url in ("javascript:alert(1)", "https://name:secret@other.example/", article().canonical_url)
        ] + [{"url": "https://other.example/guide", "position": True}]
        result = build_article_health_evidence(article(), observations=[observation(competitors=rows)], as_of=AS_OF)
        self.assertEqual(len(result["competitors"]), 1)
        self.assertIsNone(result["competitors"][0]["position"])

    def test_newer_observation_wins_and_references_do_not_overwrite_it(self):
        result = build_article_health_evidence(
            article(), observations=[observation(), observation(observedAt="2026-01-01")],
            keywords=[{"keyword": "Urban   Trees", "competitor_urls": ["https://other.example/tree-guide"]}],
            as_of=AS_OF,
        )
        self.assertEqual(len(result["competitors"]), 1)
        self.assertEqual(result["competitors"][0]["observedAt"], "2026-09-25")

    def test_keyed_run_result_contract_and_bounded_output(self):
        bundle = observation(competitors=[{"url": f"https://other-{i}.example/guide"} for i in range(100)])
        bundles = research_evidence_bundles({"article_health_evidence": {"urban trees": bundle}})
        self.assertEqual(bundles, [bundle])
        self.assertEqual(len(build_article_health_evidence(article(), observations=bundles, as_of=AS_OF)["competitors"]), 20)


class ReportEvidenceLoaderUnitTests(unittest.TestCase):
    def test_loader_scopes_both_research_sources_to_organization(self):
        from content_analytics.services import reports
        keywords = Mock()
        keywords.objects.filter.return_value.values.return_value = []
        runs = Mock()
        runs.objects.filter.return_value.order_by.return_value.values_list.return_value = [
            {"article_health_evidence": {"urban trees": observation()}},
        ]
        organization = object()
        with patch.object(reports, "ResearchedKeyword", keywords), patch.object(reports, "ContentFactoryRun", runs):
            result = reports._article_health_evidence(organization, [article()], AS_OF)
        self.assertEqual(keywords.objects.filter.call_args.kwargs["organization"], organization)
        self.assertEqual(runs.objects.filter.call_args.kwargs["organization"], organization)
        self.assertEqual(result["article-1"]["competitors"][0]["ownPosition"], 8)

    def test_search_metrics_use_observed_aggregate_rows_and_sync_watermark(self):
        from content_analytics.services import reports
        search = Mock()
        search.objects.filter.return_value.values.return_value.annotate.return_value.order_by.return_value = [{
            "article_id": "article-1", "clicks": Decimal("12"), "impressions": Decimal("400"),
            "position_weight": Decimal("2400"), "data_through": date(2026, 9, 25),
        }]
        state = Mock()
        state.objects.filter.return_value.first.return_value = SimpleNamespace(synced_through=date(2026, 9, 25))
        organization = object()
        windows = SimpleNamespace(window_start=date(2026, 9, 19), window_end=date(2026, 9, 25))
        with patch.object(reports, "ArticleSearchDaily", search), patch.object(reports, "AnalyticsSyncState", state):
            result, through = reports._per_article_search(organization, windows, connected=True)
            missing, missing_through = reports._per_article_search(organization, windows, connected=False)
        self.assertEqual(result["article-1"]["averagePosition"], 6)
        self.assertEqual(result["article-1"]["searchCtr"], 0.03)
        self.assertEqual(through, date(2026, 9, 25))
        self.assertEqual((missing, missing_through), ({}, None))
        self.assertEqual(search.objects.filter.call_args.kwargs, {
            "organization": organization, "date__range": (windows.window_start, windows.window_end),
            "country": "", "device": "", "engine": "google", "surface": "web",
        })


if __name__ == "__main__":
    unittest.main()
