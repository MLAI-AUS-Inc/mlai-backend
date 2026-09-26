"""Metric regressions, using scripts/test_without_database.py (no migrations)."""

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .topic_metrics import (
    dated_history,
    difficulty_metrics,
    keyword_measurement_defaults,
    topic_metric_payload,
    velocity_snapshot_defaults,
)


def months(values, start=3):
    return [{"year": 2026, "month": index + start, "search_volume": value} for index, value in enumerate(values)]


class TopicMetricsUnitTests(unittest.TestCase):
    def test_verified_zero_and_true_fifty_are_available(self):
        for source in ("dataforseo_labs", "dataforseo_bulk"):
            for value in (0, 50, 100, "31"):
                with self.subTest(source=source, value=value):
                    actual = difficulty_metrics({"difficulty": value, "difficulty_source": source})
                    self.assertEqual(actual["difficultyStatus"], "available")
                    self.assertEqual(actual["difficulty"], int(value))

    def test_unverified_default_and_invalid_scores_are_not_measurements(self):
        for source, value in [("legacy_default", 50), ("missing", 0), ("dataforseo_labs", None),
                              ("dataforseo_labs", -1), ("dataforseo_labs", 101),
                              ("dataforseo_labs", True), ("dataforseo_labs", 3.5),
                              ("dataforseo_labs", "NaN"), ("dataforseo_labs", float("inf"))]:
            with self.subTest(source=source, value=value):
                actual = difficulty_metrics({"difficulty": value, "difficulty_source": source})
                self.assertIsNone(actual["difficulty"])
                self.assertEqual(actual["difficultyStatus"], "unavailable")

    def test_provider_error_is_distinct_from_unavailable(self):
        actual = difficulty_metrics({"difficulty_status": "error", "difficulty_reason": "Provider lookup failed."})
        self.assertEqual(actual["difficultyStatus"], "error")
        self.assertEqual(actual["difficultyReason"], "Provider lookup failed.")

    def test_months_are_sorted_and_trimmed_without_filling_gaps(self):
        rows = months([10, 20, 30, 40, 50, 60, 70], start=1)
        rows.pop(3)
        rows += [{"year": 2026, "month": 13, "search_volume": 99}, {"year": 2026, "month": 3, "search_volume": None}]
        result = dated_history(list(reversed(rows)))
        self.assertEqual([row["month"] for row in result], [2, 3, 5, 6, 7])

    def test_unanchored_arrays_and_missing_values_never_become_chart_points(self):
        self.assertEqual(dated_history([10, 20, 30]), [])
        self.assertEqual(dated_history([{"date": "2026-01-01", "volume": None}, {"date": "2026-01-02", "volume": False}]), [])
        self.assertEqual(dated_history([{"date": "2026-01-01", "volume": 0}]), [{"date": "2026-01-01", "volume": 0.0}])

    def test_monthly_trend_thresholds_and_zero_baselines(self):
        for values, expected, percent in [
            ([100, 100, 100, 200, 200, 200], "breakout", 100),
            ([100, 100, 100, 116, 116, 116], "rising", 16),
            ([100, 100, 100, 115, 115, 115], "stable", 15),
            ([100, 100, 100, 84, 84, 84], "declining", -16),
            ([10, 10, 10, 10, 10, 10], "stable", 0),
            ([0, 0, 0, 10, 10, 10], "breakout", None),
            ([0, 0, 0, 0, 0, 0], "unknown", None),
        ]:
            with self.subTest(values=values):
                result = topic_metric_payload({"monthly_searches": months(values)})
                self.assertEqual(result["trendStatus"], expected)
                self.assertEqual(result["trendPercent"], percent)
                self.assertEqual(result["trendSource"], "dataforseo_labs")
                self.assertEqual(result["trendBasis"], "search_volume")
                self.assertFalse(result["trendIsEstimated"])

    def test_incomplete_signal_is_unknown_even_if_old_payload_claimed_stable(self):
        for rows in (months([20]), months([20, 30, 40])[::2], []):
            with self.subTest(rows=rows):
                result = topic_metric_payload({"monthly_searches": rows, "trend_status": "stable", "trend_percent": 0})
                self.assertEqual(result["trendStatus"], "unknown")
                self.assertIsNone(result["trendPercent"])

    def test_google_monthly_history_does_not_inherit_conflicting_ai_trend(self):
        result = topic_metric_payload({
            "monthly_searches": months([100, 100, 100, 10, 10, 10]),
            "trend_source": "dataforseo_ai", "trend_basis": "ai_search_volume",
            "velocity_data": {"daily_volumes": [{"date": "2026-07-01", "volume": 1}, {"date": "2026-08-01", "volume": 10}],
                              "trend_status": "breakout", "velocity_score": 9,
                              "source": "dataforseo_ai", "basis": "ai_search_volume"},
        })
        self.assertEqual(result["trendStatus"], "declining")
        self.assertEqual(result["trendPercent"], -90)
        self.assertEqual(result["monthlySearchesSource"], "dataforseo_labs")
        self.assertEqual(result["monthlySearchesBasis"], "search_volume")

    def test_nested_velocity_keeps_its_own_history_and_provenance(self):
        history = [{"date": "2026-07-01", "volume": 4}, {"date": "2026-08-01", "volume": 4}]
        result = topic_metric_payload({"velocity_data": {"daily_volumes": history, "trend_status": "stable",
                                    "velocity_score": 0, "source": "google_trends", "basis": "relative_interest", "is_estimated": False}})
        self.assertEqual(result["monthlySearches"], history)
        self.assertEqual(result["trendPercent"], 0)
        self.assertFalse(result["trendIsEstimated"])
        self.assertEqual(result["trendSource"], "google_trends")

    def test_numeric_velocity_requires_real_matching_dates(self):
        velocity = {"daily_volumes": [20, 20, 10, 10],
                    "dates": ["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"],
                    "source": "google_trends", "basis": "relative_interest"}
        result = topic_metric_payload({"velocity_data": velocity})
        self.assertEqual(result["trendStatus"], "declining")
        self.assertEqual(result["trendPercent"], -50)
        self.assertEqual(len(result["monthlySearches"]), 4)
        velocity["dates"] = velocity["dates"][:1]
        self.assertEqual(topic_metric_payload({"velocity_data": velocity})["monthlySearches"], [])

    def test_worker_nested_monthly_payload_round_trips_to_snapshot(self):
        velocity = {"monthly_searches": months([10, 10, 10, 30, 30, 30]),
                    "daily_volumes": [10, 10, 10, 30, 30, 30], "source": "dataforseo_labs", "basis": "search_volume"}
        snapshot = velocity_snapshot_defaults({"velocity_data": velocity})
        self.assertEqual(snapshot["trend_status"], "breakout")
        self.assertEqual(snapshot["velocity_score"], 2)
        self.assertEqual(snapshot["daily_volumes"], velocity["monthly_searches"])

    def test_specific_failure_and_provider_dates_are_preserved(self):
        result = topic_metric_payload({"trend_reason": "History provider unavailable.", "trend_country": "Australia",
                                       "trend_language": "English", "metrics_checked_at": "2026-09-26T12:00:00Z",
                                       "trend_last_updated_at": "2026-09-02T10:00:00Z"})
        self.assertEqual(result["trendReason"], "History provider unavailable.")
        self.assertEqual(result["trendCountry"], "Australia")
        self.assertEqual(result["metricsCheckedAt"], "2026-09-26T12:00:00Z")
        self.assertEqual(result["trendLastUpdatedAt"], "2026-09-02T10:00:00Z")

    def test_sparse_or_failed_sync_does_not_overwrite_stored_measurements(self):
        for data in ({}, {"difficulty": None, "difficulty_source": "missing", "monthly_searches": []},
                     {"difficulty": 50, "difficulty_source": "legacy_default", "monthly_searches": [1, 2]}):
            self.assertEqual(keyword_measurement_defaults(data), {})
        self.assertEqual(keyword_measurement_defaults({"difficulty": 0, "difficulty_source": "dataforseo_bulk"}),
                         {"difficulty": 0, "difficulty_source": "dataforseo_bulk"})

    def test_empty_and_unknown_velocity_cannot_create_fake_stable_snapshot(self):
        for velocity in ({}, {"daily_volumes": []}, {"daily_volumes": [1, 2]}, {"daily_volumes": months([0, 0])}):
            self.assertIsNone(velocity_snapshot_defaults({"velocity_data": velocity}))
        self.assertEqual(velocity_snapshot_defaults({"velocity_data": {
            "daily_volumes": months([100, 100, 100, 10, 10, 10]), "source": "dataforseo_labs", "basis": "search_volume",
        }})["trend_status"], "declining")


class TopicMetricsAPIUnitTests(unittest.TestCase):
    """Exercise actual API code against controlled ORM seams; no database."""

    def setUp(self):
        from . import service_views, vibe_marketing_views
        self.service = service_views
        self.views = vibe_marketing_views

    def test_discovery_option_preserves_zero_and_reads_nested_velocity(self):
        option = {"keyword": "example", "difficulty": 0, "difficulty_source": "dataforseo_bulk", "ai_search_volume": 0,
                  "velocity_data": {"daily_volumes": months([10, 10, 10, 10, 10, 10]), "velocity_score": 0,
                                    "source": "dataforseo_labs", "basis": "search_volume", "is_estimated": False}}
        result = self.views._extract_topic_candidates_from_result({"options": [option]})[0]
        self.assertEqual(result["difficulty"], 0)
        self.assertEqual(result["aiSearches"], 0)
        self.assertEqual(result["trendPercent"], 0)
        self.assertEqual(len(result["monthlySearches"]), 6)

    def test_stored_measurement_enriches_old_run_without_replacing_title(self):
        old = {"keyword": "example", "title": "An editorial headline", "sourceRunId": "old-run", "difficulty": 50, "difficultySource": "legacy_default"}
        measured = {"keyword": "example", "title": "example", "difficulty": 0, "difficultySource": "dataforseo_bulk", "monthlySearches": months([100, 100, 100, 10, 10, 10])}
        result = self.views._merge_topic_candidate(old, measured)
        self.assertEqual(result["title"], "An editorial headline")
        self.assertEqual(result["difficulty"], 0)
        self.assertEqual(result["difficultyStatus"], "available")
        self.assertEqual(result["trendStatus"], "declining")

    def test_new_stored_metrics_win_but_old_run_provenance_is_not_relabelled(self):
        old = {"keyword": "example", "sourceRunId": "old-run", "monthlySearches": months([10, 10, 10, 20, 20, 20]),
               "trendCountry": "Australia", "trendLastUpdatedAt": "2026-09-02T10:00:00Z",
               "difficulty": 10, "difficultySource": "dataforseo_labs"}
        stored = {"keyword": "example", "source": "researched_keyword", "monthlySearches": months([100, 100, 100, 10, 10, 10], start=4),
                  "difficulty": 40, "difficultySource": "dataforseo_bulk"}
        merged = self.views._merge_topic_candidate(old, stored)
        self.assertEqual(merged["difficulty"], 10)
        self.assertEqual(merged["trendStatus"], "declining")
        self.assertIsNone(merged["trendCountry"])
        self.assertIsNone(merged["trendLastUpdatedAt"])
        same = self.views._merge_topic_candidate(old, {**stored, "monthlySearches": old["monthlySearches"]})
        self.assertEqual(same["trendCountry"], "Australia")
        self.assertEqual(same["trendLastUpdatedAt"], old["trendLastUpdatedAt"])

    def test_older_stored_history_cannot_replace_a_newer_unsynchronized_run(self):
        fresh = {"keyword": "example", "sourceRunId": "new-run", "monthlySearches": months([10, 10, 10, 20, 20, 20]),
                 "trendLastUpdatedAt": "2026-09-02T10:00:00Z"}
        stale = {"keyword": "example", "source": "researched_keyword", "monthlySearches": months([100, 100, 100, 10, 10, 10], start=2)}
        result = self.views._merge_topic_candidate(fresh, stale)
        self.assertEqual(result["monthlySearches"][-1]["month"], 8)
        self.assertEqual(result["trendStatus"], "breakout")
        self.assertEqual(result["trendLastUpdatedAt"], fresh["trendLastUpdatedAt"])

    def test_difficulty_uses_known_lookup_dates_and_preserves_verified_result_if_unknown(self):
        fresh = {"difficulty": 20, "difficultySource": "dataforseo_labs", "metricsCheckedAt": "2026-09-26T10:00:00Z"}
        stored = {"difficulty": 40, "difficultySource": "dataforseo_bulk", "source": "researched_keyword",
                  "monthlySearches": months([100, 100], start=10)}
        for checked_at in (None, "invalid", "2026-09-25T10:00:00Z", "2026-09-26T10:00:00"):
            with self.subTest(checked_at=checked_at):
                score, source = self.views._prefer_topic_difficulty(fresh, {**stored, "metricsCheckedAt": checked_at})
                self.assertEqual((score, source), (20, "dataforseo_labs"))
        self.assertEqual(self.views._prefer_topic_difficulty(fresh, {**stored, "metricsCheckedAt": "2026-09-27T10:00:00Z"}),
                         (40, "dataforseo_bulk"))

    def test_new_declining_bundle_uses_its_volume_including_zero_instead_of_historical_maximum(self):
        old = {"keyword": "example", "volume": 500, "monthlySearches": months([500] * 6, start=2)}
        for volume in (0, 10):
            with self.subTest(volume=volume):
                fresh = {"keyword": "example", "source": "researched_keyword", "volume": volume,
                         "monthlySearches": months([100, 100, 100, 10, 10, 10])}
                merged = self.views._merge_topic_candidate(old, fresh)
                self.assertEqual(merged["volume"], volume)
                self.assertEqual(merged["trendStatus"], "declining")

    def test_legacy_stored_default_is_unavailable_and_undated_history_is_not_plotted(self):
        keyword = SimpleNamespace(id="legacy", keyword="legacy", intent="informational", difficulty=50,
                                  difficulty_source="legacy_default", opportunity_index=0, volume=100,
                                  written_article=None, status="pending", tier="tier_3_long_tail", monthly_searches=[10, 20, 30])
        with patch.object(self.views, "_latest_keyword_velocity", return_value=None), \
             patch.object(self.views, "_latest_keyword_saturation", return_value=None), \
             patch.object(self.views, "_keyword_pillar_metadata", return_value={}), \
             patch.object(self.views, "_keyword_related_keywords", return_value=[]), \
             patch.object(self.views, "_keyword_paa_questions", return_value=[]):
            result = self.views._topic_candidate_from_keyword(keyword)
        self.assertEqual(result["difficultyStatus"], "unavailable")
        self.assertIsNone(result["difficulty"])
        self.assertIn("difficulty unavailable", result["reason"])
        self.assertEqual(result["monthlySearches"], [])

    def test_bulk_upsert_accepts_null_and_only_updates_verified_measurements(self):
        from rest_framework.test import APIRequestFactory
        payload = {"domain": "example.test", "keywords": [{"keyword": "Example", "difficulty": None, "difficulty_source": "missing", "monthly_searches": [], "velocity_data": {"daily_volumes": []}}]}
        organization = SimpleNamespace(domain="example.test")
        manager = Mock()
        manager.update_or_create.return_value = (SimpleNamespace(), False)
        with patch.object(self.service.Organization.objects, "get", return_value=organization), \
             patch.object(self.service.ResearchedKeyword, "objects", manager), \
             patch.object(self.service.KeywordVelocity.objects, "create") as create_snapshot, \
             patch.object(self.service.transaction, "atomic", return_value=nullcontext()):
            request = self.service.SEOKeywordBulkUpsertView().initialize_request(APIRequestFactory().post("/", payload, format="json"))
            response = self.service.SEOKeywordBulkUpsertView().post(request)
        self.assertEqual(response.status_code, 200)
        kwargs = manager.update_or_create.call_args.kwargs
        self.assertIs(kwargs["organization"], organization)
        self.assertEqual(kwargs["keyword_normalized"], "example")
        self.assertNotIn("difficulty", kwargs["defaults"])
        self.assertNotIn("difficulty_source", kwargs["defaults"])
        self.assertNotIn("monthly_searches", kwargs["defaults"])
        self.assertEqual(kwargs["create_defaults"]["difficulty_source"], "missing")
        create_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
