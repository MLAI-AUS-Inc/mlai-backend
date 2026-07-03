from unittest import mock

from django.test import TestCase
from django.utils import timezone

from content_factory.models import WebsiteBaselineSnapshot
from content_factory.vibe_marketing_views import (
    _calculate_baseline_overall,
    _merge_google_metrics_into_baseline,
    _serialize_baseline_snapshot,
)
from organizations.models import Organization


def _snapshot_metrics():
    return {
        "technicalHealth": {"status": "measured", "score": 89, "checks": {"https": True}, "pages": [{"url": "https://x"}]},
        "aiVisibility": {
            "status": "measured",
            "score": 41,
            "displayRows": [{"label": "Mentioned queries", "value": 4, "unit": "of 4"}],
            "providers": [
                {
                    "key": "chatgpt",
                    "label": "ChatGPT",
                    "score": 65,
                    "status": "measured",
                    "source": "DataForSEO LLM Responses",
                    "prompts": [{"prompt": "long transcript", "responseExcerpt": "x" * 500}],
                }
            ],
            "queries": [{"query": "brand"}],
        },
        "organicSearch": {"status": "measured", "score": 17, "topKeywords": [{"keyword": "the product"}]},
        "authority": {"status": "measured", "score": 0, "authorityScore": 0, "backlinks": 0, "referringDomains": 0, "raw": {}},
        "lighthouse": {"status": "unavailable", "score": None, "message": "rate limited", "reasonCode": "rate_limited"},
        "coreWebVitals": {"status": "unavailable", "score": None, "message": "rate limited"},
        "traffic": {"status": "needs_connection", "score": None, "verified": False, "message": "Connect GSC"},
    }


class BaselineCompactSerializationTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="theproductbus.com", name="The Product Bus")
        self.snapshot = WebsiteBaselineSnapshot.objects.create(
            organization=self.organization,
            domain="theproductbus.com",
            run_id="run-1",
            status="completed",
            collected_at=timezone.now(),
            overall_score=59,
            summary={"text": "Website baseline needs attention."},
            metrics=_snapshot_metrics(),
            source_status={key: value.get("status") for key, value in _snapshot_metrics().items()},
            raw_payload={"scoreCoverage": 60, "metrics": _snapshot_metrics()},
        )

    def test_compact_keeps_every_metric_key(self):
        payload = _serialize_baseline_snapshot(self.snapshot, compact=True)
        self.assertEqual(set(payload["metrics"].keys()), set(_snapshot_metrics().keys()))

    def test_compact_metric_keeps_card_fields(self):
        payload = _serialize_baseline_snapshot(self.snapshot, compact=True)
        technical = payload["metrics"]["technicalHealth"]
        self.assertEqual(technical["status"], "measured")
        self.assertEqual(technical["score"], 89)
        self.assertNotIn("pages", technical)
        authority = payload["metrics"]["authority"]
        self.assertEqual(authority["backlinks"], 0)
        self.assertEqual(authority["referringDomains"], 0)
        lighthouse = payload["metrics"]["lighthouse"]
        self.assertEqual(lighthouse["reasonCode"], "rate_limited")

    def test_compact_slims_ai_providers_but_keeps_card_rows(self):
        payload = _serialize_baseline_snapshot(self.snapshot, compact=True)
        ai = payload["metrics"]["aiVisibility"]
        self.assertEqual(ai["displayRows"][0]["label"], "Mentioned queries")
        self.assertEqual(ai["providers"][0]["key"], "chatgpt")
        self.assertEqual(ai["providers"][0]["score"], 65)
        self.assertNotIn("prompts", ai["providers"][0])
        self.assertNotIn("queries", ai)

    def test_score_coverage_prefers_raw_payload_then_falls_back_to_weights(self):
        payload = _serialize_baseline_snapshot(self.snapshot, compact=True)
        self.assertEqual(payload["scoreCoverage"], 60)
        self.snapshot.raw_payload = {}
        self.snapshot.save(update_fields=["raw_payload"])
        payload = _serialize_baseline_snapshot(self.snapshot, compact=True)
        # measured weights: technicalHealth 40 + authority 10 + organicSearch 10 + aiVisibility 10 = 70
        self.assertEqual(payload["scoreCoverage"], 70)

    def test_full_serialization_untouched(self):
        payload = _serialize_baseline_snapshot(self.snapshot, compact=False)
        self.assertIn("prompts", payload["metrics"]["aiVisibility"]["providers"][0])


class BaselineOverallRecomputeTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="theproductbus.com", name="The Product Bus")
        metrics = {
            "technicalHealth": {"status": "measured", "score": 74},
            "authority": {"status": "measured", "score": 0},
            "traffic": {"status": "needs_connection", "score": None},
        }
        self.snapshot = WebsiteBaselineSnapshot.objects.create(
            organization=self.organization,
            domain="theproductbus.com",
            run_id="run-1",
            status="completed",
            collected_at=timezone.now(),
            overall_score=59,
            summary={"text": "Website baseline needs attention."},
            metrics=metrics,
            source_status={key: value["status"] for key, value in metrics.items()},
            raw_payload={"metrics": metrics, "overallScore": 59, "scoreCoverage": 50},
        )

    def test_calculate_baseline_overall_matches_producer(self):
        overall = _calculate_baseline_overall(self.snapshot.metrics)
        # (74*40 + 0*10) / 50 = 59.2 -> 59; coverage 50/100 -> 50
        self.assertEqual(overall["score"], 59)
        self.assertEqual(overall["coverage"], 50)

    def test_merge_recomputes_score_coverage_and_summary(self):
        google_metrics = {
            "traffic": {
                "status": "measured",
                "verified": True,
                "score": 80,
                "googleSearchConsole": {"status": "measured", "last28Days": {"clicks": 100}},
            },
            "sourceStatus": {"googleSearchConsole": "measured", "googleAnalytics": "needs_connection"},
        }
        snapshot = _merge_google_metrics_into_baseline(self.snapshot, google_metrics)
        # (74*40 + 0*10 + 80*5) / 55 = 61.09 -> 61; coverage 55/100 -> 55
        self.assertEqual(snapshot.overall_score, 61)
        self.assertEqual(snapshot.raw_payload["overallScore"], 61)
        self.assertEqual(snapshot.raw_payload["scoreCoverage"], 55)
        self.assertIn("fair", snapshot.summary["text"].lower())
        self.assertEqual(snapshot.source_status["traffic"], "measured")

    def test_merge_with_error_traffic_keeps_existing_score_but_updates_summary(self):
        google_metrics = {
            "traffic": {"status": "error", "verified": False, "score": None, "message": "Search Console lookup failed: 403"},
            "sourceStatus": {"googleSearchConsole": "error", "googleAnalytics": "needs_connection"},
        }
        snapshot = _merge_google_metrics_into_baseline(self.snapshot, google_metrics)
        self.assertEqual(snapshot.overall_score, 59)
        self.assertEqual(snapshot.metrics["traffic"]["status"], "error")
        self.assertEqual(snapshot.metrics["traffic"]["message"], "Search Console lookup failed: 403")


class GoogleTrafficRollupTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def _user(self):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(email="founder@mlai.au", password="x")

    def test_gsc_error_is_surfaced_not_masked_as_needs_connection(self):
        from content_factory import google_baseline

        user = self._user()
        connection = mock.Mock()
        connection.scope = google_baseline.GSC_SCOPE
        with mock.patch.object(google_baseline, "google_connection_for_user", return_value=connection), mock.patch.object(
            google_baseline,
            "_collect_search_console",
            return_value={"status": "error", "message": "Search Console lookup failed: accessNotConfigured"},
        ):
            result = google_baseline.collect_verified_google_metrics(user=user, domain="mlai.au")

        traffic = result["traffic"]
        self.assertEqual(traffic["status"], "error")
        self.assertEqual(traffic["message"], "Search Console lookup failed: accessNotConfigured")
        self.assertFalse(traffic["verified"])

    def test_gsc_property_missing_still_reports_needs_connection(self):
        from content_factory import google_baseline

        user = self._user()
        connection = mock.Mock()
        connection.scope = google_baseline.GSC_SCOPE
        with mock.patch.object(google_baseline, "google_connection_for_user", return_value=connection), mock.patch.object(
            google_baseline,
            "_collect_search_console",
            return_value={"status": "needs_connection", "message": "No verified Search Console property matched this domain."},
        ):
            result = google_baseline.collect_verified_google_metrics(user=user, domain="mlai.au")

        traffic = result["traffic"]
        self.assertEqual(traffic["status"], "needs_connection")
        self.assertEqual(traffic["message"], "No verified Search Console property matched this domain.")

    def test_gsc_measured_keeps_measured_status(self):
        from content_factory import google_baseline

        user = self._user()
        connection = mock.Mock()
        connection.scope = google_baseline.GSC_SCOPE
        with mock.patch.object(google_baseline, "google_connection_for_user", return_value=connection), mock.patch.object(
            google_baseline,
            "_collect_search_console",
            return_value={"status": "measured", "last28Days": {"clicks": 250, "impressions": 4000}},
        ):
            result = google_baseline.collect_verified_google_metrics(user=user, domain="mlai.au")

        traffic = result["traffic"]
        self.assertEqual(traffic["status"], "measured")
        self.assertTrue(traffic["verified"])
        self.assertIsNotNone(traffic["score"])
