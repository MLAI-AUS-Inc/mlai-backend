from unittest import TestCase

from .hosted_quality_state import founder_result


class HostedSafetyPresentationTests(TestCase):
    def test_current_hold_shows_sentence_and_delivery_state_without_audit(self):
        url = "https://preview.example/draft"
        quality = {
            "status": "blocking_findings", "escalation": "safety_review",
            "preview_url": url, "resume_generation": 2, "preview_inputs_invalid": True,
            "reviewed_at": "2026-11-18T00:30:00+00:00",
            "safety_check": {"status": "needs_verification", "items": [
                {"sentence": "Draft safety sentence.", "staff_note": "Internal evidence notes"},
            ]},
        }
        alert = {
            "state": "sent", "queued_at": "2026-11-18T00:31:00+00:00",
            "sent_at": "2026-11-18T00:32:00+00:00",
        }
        result = {"live_preview": {"previewUrl": url, "resumeGeneration": 2}, "article_preview_quality": quality,
                  "ops_alerts": {"article_safety_review_required": alert}}
        view = founder_result(result)
        self.assertNotIn("ops_alerts", view)
        projected = view["article_preview_quality"]
        self.assertEqual(projected["safety_review_status"], "notified")
        self.assertTrue(projected["preview_inputs_invalid"])
        self.assertEqual(projected["safety_check"]["items"], [{"sentence": "Draft safety sentence."}])
        alert["acknowledged_at"] = "2026-11-18T00:34:00+00:00"
        self.assertEqual(founder_result(result)["article_preview_quality"]["safety_review_status"], "acknowledged")
        quality["reviewed_at"] = "2026-11-18T00:35:00+00:00"
        self.assertNotIn("safety_review_status", founder_result(result)["article_preview_quality"])
        quality["resume_generation"] = 1
        self.assertNotIn("safety_check", founder_result(result)["article_preview_quality"])
