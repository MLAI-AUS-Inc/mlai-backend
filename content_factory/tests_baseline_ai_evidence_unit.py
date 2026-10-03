"""Database-free regressions for the founder's AI answer evidence contract."""

import copy
import unittest

from content_factory.baseline_ai_evidence import (
    AI_ANSWER_TEXT_LIMIT,
    AI_PROMPT_LIMIT,
    compact_ai_prompt_evidence,
    compact_ai_providers,
)
from content_factory.tests_baseline_metrics_unit import ai_metric, snapshot
from content_factory.vibe_marketing_views import _compact_baseline_metric, _serialize_baseline_snapshot


def answer(**extra):
    return {
        "query": "Australian founder communities",
        "prompt": "Which communities help Australian founders?",
        "status": "measured",
        "mentioned": True,
        "cited": True,
        "citedUrls": ["https://example.org/about"],
        "mentionContexts": [{"text": "Example helps founders share their progress.", "kind": "mentioned"}],
        "responseExcerpt": "Example helps founders share their progress.",
        **extra,
    }


class BaselineAiEvidenceTests(unittest.TestCase):
    def test_compact_keeps_new_answers_and_provenance_without_changing_scores(self):
        metadata = {
            "modelName": "provider-returned-model",
            "capturedAt": "2026-10-03T02:00:00Z",
            "measurementType": "model_probe",
            "evidenceVersion": "llm-final-answer-v1",
            "webSearchUsed": True,
            "countryCode": None,
            "requestedCountryCode": "AU",
            "locationTargeting": "unsupported",
        }
        prompt = answer(
            answerText="Example helps founders.\n\nAnother resource is listed.",
            answerTextTruncated=False,
            sourceUrls=["https://example.org/about", "https://other.example/guide"],
            **metadata,
        )
        metric = ai_metric(providers=[ai_metric(key="gemini", label="Gemini", prompts=[prompt], **metadata)], **metadata)
        original = copy.deepcopy(metric)
        result = _serialize_baseline_snapshot(snapshot({"aiVisibility": metric}), compact=True)
        compact = result["metrics"]["aiVisibility"]
        self.assertEqual(compact["score"], 30)
        self.assertEqual(result["scoreCoverage"], 10)
        provider = compact["providers"][0]
        self.assertEqual(provider["score"], 30)
        self.assertEqual(provider["requestedCount"], 12)
        for key, value in metadata.items():
            self.assertEqual(provider[key], value)
            self.assertEqual(provider["prompts"][0][key], value)
        self.assertEqual(provider["prompts"][0]["answerText"], prompt["answerText"])
        self.assertFalse(provider["prompts"][0]["answerTextTruncated"])
        self.assertEqual(provider["prompts"][0]["sourceUrls"], prompt["sourceUrls"])
        self.assertEqual(compact["evidenceVersion"], metadata["evidenceVersion"])
        self.assertIsNone(compact["countryCode"])
        self.assertEqual(metric, original)

    def test_saved_excerpt_is_available_without_inventing_a_full_answer(self):
        compact = _compact_baseline_metric(ai_metric(providers=[ai_metric(key="chatgpt", prompts=[answer()])]))
        row = compact["providers"][0]["prompts"][0]
        self.assertEqual(row["responseExcerpt"], answer()["responseExcerpt"])
        self.assertNotIn("answerText", row)
        self.assertNotIn("answerTextTruncated", row)
        self.assertNotIn("webSearchUsed", row)
        self.assertNotIn("measurementType", row)
        self.assertNotIn("countryCode", row)

    def test_missing_legacy_transcripts_stay_missing(self):
        for prompts in (None, [], {}, "not an array", [{"text": "unidentified transcript"}]):
            provider = compact_ai_providers([ai_metric(key="chatgpt", prompts=prompts)])[0]
            self.assertNotIn("prompts", provider)

    def test_failures_and_unavailable_answers_are_not_negative_observations(self):
        rows = compact_ai_prompt_evidence([
            answer(status="error", error="The provider failed.", mentioned=False, cited=False),
            answer(status="unavailable", mentioned=False, cited=False),
            answer(status="measured", mentioned=False, cited=False),
        ])
        self.assertEqual([row["status"] for row in rows], ["error", "unavailable", "measured"])
        self.assertEqual(rows[0]["error"], "The provider failed.")
        for row in rows[:2]:
            self.assertEqual(row["prompt"], answer()["prompt"])
            self.assertNotIn("mentioned", row)
            self.assertNotIn("cited", row)
            self.assertNotIn("answerText", row)
        self.assertFalse(rows[2]["mentioned"])
        self.assertFalse(rows[2]["cited"])

    def test_malformed_flags_never_become_absent_or_measured(self):
        for fields in ({"mentioned": 0}, {"cited": "false"}, {"mentioned": None}, {"cited": []}):
            row = compact_ai_prompt_evidence([answer(**fields)])[0]
            self.assertEqual(row["status"], "unavailable")
            self.assertEqual(row["reasonCode"], "invalid_answer_evidence")
            self.assertNotIn("mentioned", row)
            self.assertNotIn("cited", row)

    def test_malformed_nested_values_are_rejected_without_crashing(self):
        for value in (None, "text", {}, 1):
            self.assertEqual(compact_ai_prompt_evidence(value), [])
            self.assertEqual(compact_ai_providers(value), [])
        rows = compact_ai_prompt_evidence([
            None, "text", [], {"query": []},
            answer(status=[], sourceUrls={}, citedUrls="https://example.org", mentionContexts={}),
            answer(mentionContexts=[None, {"text": [], "kind": []}, {"text": "A valid quote", "kind": []}]),
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "unavailable")
        self.assertEqual(rows[1]["mentionContexts"], [{"text": "A valid quote", "kind": "mentioned"}])
        providers = compact_ai_providers([None, {"key": []}, {"key": "unsupported"}, ai_metric(key="chatgpt", prompts=[answer()])])
        self.assertEqual([row["key"] for row in providers], ["chatgpt"])

    def test_clickable_urls_are_http_or_https_without_credentials_or_controls(self):
        links = [
            "javascript:alert(1)", "data:text/html,hello", "file:///tmp/answer", "//example.org/path",
            "https://user:password@example.org/", "https://example.org:invalid/", "https://example.org/a\tb",
            "https://", None, {}, "https://example.org/about", "https://example.org/about", "http://other.example/guide",
        ]
        row = compact_ai_prompt_evidence([answer(sourceUrls=links, citedUrls=links)])[0]
        safe = ["https://example.org/about", "http://other.example/guide"]
        self.assertEqual(row["sourceUrls"], safe)
        self.assertEqual(row["citedUrls"], safe)

    def test_payload_limits_and_truncation_are_explicit(self):
        prompt = answer(
            query="q" * 1000, prompt="p" * 3000, responseExcerpt="e" * 1000,
            answerText="a" * (AI_ANSWER_TEXT_LIMIT + 10),
            sourceUrls=[f"https://source.example/{index}" for index in range(30)],
            citedUrls=[f"https://example.org/{index}" for index in range(30)],
            mentionContexts=[{"text": "c" * 500, "kind": "mentioned"}] * 10,
        )
        rows = compact_ai_prompt_evidence([prompt] * (AI_PROMPT_LIMIT + 10))
        self.assertEqual(len(rows), AI_PROMPT_LIMIT)
        row = rows[0]
        self.assertEqual(len(row["query"]), 500)
        self.assertEqual(len(row["prompt"]), 2000)
        self.assertEqual(len(row["responseExcerpt"]), 320)
        self.assertEqual(len(row["answerText"]), AI_ANSWER_TEXT_LIMIT)
        self.assertTrue(row["answerTextTruncated"])
        self.assertEqual(len(row["sourceUrls"]), 20)
        self.assertEqual(len(row["citedUrls"]), 10)
        self.assertEqual(len(row["mentionContexts"]), 2)
        self.assertEqual(len(row["mentionContexts"][0]["text"]), 280)
        self.assertTrue(compact_ai_prompt_evidence([answer(answerText="short", answerTextTruncated=True)])[0]["answerTextTruncated"])

    def test_unknown_provenance_does_not_become_an_australian_web_search(self):
        row = compact_ai_prompt_evidence([answer(
            webSearchUsed="true", countryCode=["AU"], requestedCountryCode="Australia",
            modelName={}, capturedAt="not a timestamp", locationTargeting=[],
        )])[0]
        for field in ("webSearchUsed", "countryCode", "requestedCountryCode", "modelName", "capturedAt", "locationTargeting"):
            self.assertNotIn(field, row)
        row = compact_ai_prompt_evidence([answer(countryCode=None, webSearchUsed=None)])[0]
        self.assertIsNone(row["countryCode"])
        self.assertIsNone(row["webSearchUsed"])

    def test_full_response_and_saved_metrics_are_not_rewritten(self):
        metric = ai_metric(providers=[ai_metric(key="chatgpt", prompts=[answer(answerText="x" * 20000)])])
        original = copy.deepcopy(metric)
        result = _serialize_baseline_snapshot(snapshot({"aiVisibility": metric}), compact=False)
        self.assertEqual(len(result["metrics"]["aiVisibility"]["providers"][0]["prompts"][0]["answerText"]), 20000)
        self.assertEqual(metric, original)
