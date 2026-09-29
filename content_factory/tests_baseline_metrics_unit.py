"""Baseline contract tests; run with scripts/test_without_database.py."""

import copy
from types import SimpleNamespace
import unittest

from django.utils import timezone

from content_factory.baseline_metrics import baseline_display_metrics
from content_factory.vibe_marketing_views import (
    _calculate_baseline_overall,
    _serialize_baseline_history_point,
    _serialize_baseline_snapshot,
)


def snapshot(metrics):
    return SimpleNamespace(
        id=1, run_id="baseline-fixture", domain="example.org", status="completed",
        collected_at=timezone.now(), overall_score=99,
        metrics=metrics, source_status={key: metric["status"] for key, metric in metrics.items()},
        raw_payload={"scoreCoverage": 95}, summary={"text": "Old summary"}, recommendations=[],
    )


def ai_metric(**extra):
    return {
        "status": "measured", "score": 99, "methodVersion": "ai-mentions-v2",
        "source": "DataForSEO LLM Responses", "responseCount": 10,
        "mentionCount": 3, "citationCount": 2, "requestedCount": 12,
        "queryCount": 3, "providerCount": 4, "requestedProviderCount": 4,
        "promptSetId": "fixture", "countryCode": "AU", **extra,
    }


class BaselineMetricContractTests(unittest.TestCase):
    def test_legacy_scores_are_unavailable_without_rewriting_saved_data(self):
        metrics = {
            "technicalHealth": {"status": "measured", "score": 80},
            "authority": {"status": "measured", "score": 91, "source": "DataForSEO Backlinks"},
            "aiVisibility": {"status": "measured", "score": 28, "providers": [{"key": "chatgpt", "status": "measured", "score": 52}]},
        }
        original = copy.deepcopy(metrics)
        result = _serialize_baseline_snapshot(snapshot(metrics), compact=True)
        self.assertEqual(metrics, original)
        for key in ("authority", "aiVisibility"):
            self.assertIsNone(result["metrics"][key]["score"])
            self.assertEqual(result["metrics"][key]["reasonCode"], "legacy_method")
            self.assertEqual(result["sourceStatus"][key], "unavailable")
        self.assertIsNone(result["metrics"]["aiVisibility"]["providers"][0]["score"])
        self.assertEqual(result["overallScore"], 80)
        self.assertEqual(result["scoreCoverage"], 40)

    def test_genuine_older_ahrefs_result_retains_its_provenance(self):
        result = baseline_display_metrics({"authority": {"status": "measured", "source": "Ahrefs", "domainRating": 9.1, "score": 9}})["authority"]
        self.assertEqual(result["score"], 9.1)
        self.assertEqual(result["methodVersion"], "ahrefs-dr-v1")

    def test_ahrefs_zero_and_precision_survive_compact_and_history(self):
        for rating in (0, 9.1, 91.25):
            metrics = {"authority": {"status": "measured", "score": rating, "domainRating": rating, "source": "Ahrefs", "methodVersion": "ahrefs-dr-v1"}}
            result = _serialize_baseline_snapshot(snapshot(metrics), compact=True)
            self.assertEqual(result["metrics"]["authority"]["domainRating"], rating)
            self.assertEqual(result["metrics"]["authority"]["source"], "Ahrefs")
            point = _serialize_baseline_history_point(snapshot(metrics))
            self.assertEqual(point["metricScores"]["authority"], rating)
            self.assertEqual(point["metricMethods"]["authority"], "ahrefs-dr-v1")

    def test_current_ai_percent_uses_counts_and_compact_retains_evidence(self):
        provider = ai_metric(key="chatgpt", label="ChatGPT", prompts=[{"text": "large transcript"}])
        metrics = {"aiVisibility": ai_metric(providers=[provider])}
        result = _serialize_baseline_snapshot(snapshot(metrics), compact=True)
        ai = result["metrics"]["aiVisibility"]
        self.assertEqual(ai["score"], 30)
        for field in ("responseCount", "mentionCount", "citationCount", "requestedCount", "queryCount", "providerCount", "requestedProviderCount", "promptSetId", "countryCode", "source", "methodVersion"):
            self.assertEqual(ai[field], metrics["aiVisibility"][field])
        self.assertEqual(ai["providers"][0]["responseCount"], 10)
        self.assertNotIn("prompts", ai["providers"][0])
        point = _serialize_baseline_history_point(snapshot(metrics))
        self.assertEqual(point["metricContexts"]["aiVisibility"]["promptSetId"], "fixture")

    def test_invalid_evidence_never_becomes_measured_zero(self):
        for fields in ({"responseCount": 0}, {"mentionCount": 11}, {"citationCount": 11}, {"mentionCount": True}, {"responseCount": None}, {"requestedCount": 9}, {"requestedCount": None}):
            metric = baseline_display_metrics({"aiVisibility": ai_metric(**fields)})["aiVisibility"]
            self.assertEqual(metric["status"], "unavailable")
            self.assertIsNone(metric["score"])

    def test_unavailable_prior_sources_do_not_claim_to_have_a_legacy_measurement(self):
        for key in ("authority", "aiVisibility"):
            metric = {"status": "error", "score": None, "message": "Provider failed", "reasonCode": "provider_error"}
            result = baseline_display_metrics({key: metric})[key]
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["reasonCode"], "provider_error")
    def test_invalid_authority_never_becomes_measured_zero(self):
        for rating in (True, float("nan"), float("inf"), -1, 101):
            metric = baseline_display_metrics({"authority": {"status": "measured", "source": "Ahrefs", "methodVersion": "ahrefs-dr-v1", "domainRating": rating}})["authority"]
            self.assertIsNone(metric["score"])

    def test_history_does_not_connect_legacy_metrics_to_new_measurements(self):
        point = _serialize_baseline_history_point(snapshot({"aiVisibility": {"status": "measured", "score": 28}, "authority": {"status": "measured", "score": 91}}))
        self.assertEqual(point["metricScores"], {"aiVisibility": None, "authority": None})
        self.assertEqual(point["metricMethods"], {"aiVisibility": "legacy", "authority": "legacy"})
        self.assertIsNone(point["overallScore"])
        self.assertEqual(point["scoreCoverage"], 0)

    def test_mixed_coverage_excludes_unavailable_scores(self):
        metrics = {"technicalHealth": {"status": "measured", "score": 80}, "aiVisibility": ai_metric(responseCount=10, mentionCount=0), "authority": {"status": "measured", "score": 91}}
        self.assertEqual(_calculate_baseline_overall(metrics), {"score": 64, "coverage": 50})

    def test_percentages_and_overall_round_half_up_like_clients(self):
        result = baseline_display_metrics({"aiVisibility": ai_metric(responseCount=8, mentionCount=1, citationCount=0, requestedCount=8)})
        self.assertEqual(result["aiVisibility"]["score"], 13)
        self.assertEqual(_calculate_baseline_overall({"technicalHealth": {"status": "measured", "score": 12.5}})["score"], 13)
