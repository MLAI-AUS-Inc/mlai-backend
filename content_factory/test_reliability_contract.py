"""No database: compatibility, ordering and serializer tests."""
import unittest
from django.conf import settings

if not settings.configured:
    settings.configure(USE_I18N=False, SECRET_KEY="test-only")

from content_factory.run_state import (execution_version, stale_execution_event,
    merge_reliability_fields, reliability_presentation, active_retry_signal)
from workflow_runs.serializers import ContentFactoryRunSyncSerializer


class ReliabilityContractTests(unittest.TestCase):
    def test_live_body_requires_exact_content_and_canonical_identity(self):
        from content_factory.article_live_evidence import compare_live_body
        body = "<article><h1>Requirements</h1><p>" + "The cost is AUD 50 and it is not guaranteed. " * 5 + "</p></article>"
        url = "https://example.org/articles/requirements"
        html = '<link rel="canonical" href="' + url + '">' + body
        self.assertEqual(compare_live_body(body, html.encode(), canonical_url=url)["state"], "verified")
        for changed in [html.replace("AUD 50", "USD 50"), html.replace("not guaranteed", "guaranteed")]:
            self.assertEqual(compare_live_body(body, changed.encode(), canonical_url=url)["state"], "content_mismatch")
        with self.assertRaises(ValueError):
            compare_live_body(body, html.replace(url, "https://example.org/other").encode(), canonical_url=url)
        with self.assertRaises(ValueError):
            compare_live_body(body, b"<div id='root'></div>", canonical_url=url)
    def test_old_failure_cannot_overwrite_recovery_or_cancel(self):
        current = {"generation": 2, "state_version": 50}
        for old in [{}, {"generation": 1, "state_version": 999}, {"generation": 2, "state_version": 49}]:
            self.assertTrue(stale_execution_event(current, old))
        self.assertFalse(stale_execution_event(current, {"generation": 3, "state_version": 1}))
        self.assertTrue(stale_execution_event(current, {**current, "status": "running"}, saved_status="cancelled"))

    def test_typed_failure_round_trips_real_serializer(self):
        payload = {"run_id": "run", "workflow": "direct_generate", "status": "running",
            "generation": 2, "state_version": 50,
            "failure": {"code": "CORPUS_UNAVAILABLE", "dependency": "corpus_transport", "next_action": "automatic_retry"},
            "recovery": {"state": "pending", "due_at": "2026-09-14T01:00:00+00:00"}}
        serializer = ContentFactoryRunSyncSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        result = merge_reliability_fields({}, serializer.validated_data)
        self.assertEqual(result["failure"], payload["failure"])
        self.assertTrue(active_retry_signal(result))
        rendered = reliability_presentation(result)
        self.assertFalse(rendered["retryAvailable"])
        self.assertEqual(rendered["nextAction"], "automatic_retry")

    def test_unknown_failure_keeps_original_code_and_action(self):
        result = merge_reliability_fields({}, {"failure": {"code": "FUTURE_DEPENDENCY", "next_action": "ask_operator"}})
        self.assertEqual(reliability_presentation(result)["errorCode"], "FUTURE_DEPENDENCY")

    def test_half_version_is_invalid(self):
        serializer = ContentFactoryRunSyncSerializer(data={"run_id": "run", "workflow": "direct_generate", "status": "failed", "generation": 2})
        self.assertFalse(serializer.is_valid())
        for payload in [{"generation": True, "state_version": 1}, {"generation": -1, "state_version": 1}]:
            with self.assertRaises(ValueError):
                execution_version(payload)
