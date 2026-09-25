"""No-database tests for hosted article quality issue projection."""

from copy import deepcopy
import unittest

from content_factory.hosted_quality_issues import public_hosted_quality_issues


PREVIEW_URL = "https://preview.example/articles/test?cfInspector=1"


def sixth_shaped_report():
    quality = {
        "status": "blocking_findings",
        "preview_url": PREVIEW_URL,
        "resume_generation": 0,
        "reviewed_at": "2026-09-25T03:09:23Z",
        "visible_content_acceptance": {
            "required": True, "passed": False,
            "errors": [
                "claim-150: generated resource cannot substantiate an external claim",
                "claim-158: insufficient: The pixels cannot establish concentration.",
                "claim-174: empirical claim needs evidence",
            ],
            "review": {"attempts": [{"review": {"claims": [
                {"claim_id": "claim-150", "source_id": "artifact:verified-resource"},
                {"claim_id": "claim-158", "source_id": "captured-image:3"},
                {"claim_id": "claim-174", "source_id": ""},
            ]}}]},
        },
        "browser": {"anatomy": {
            "article_image_count": 5,
            "component_ids": ["hero-image", "image:choose-task", "image:configure-example",
                              "image:test-assistant", "resource-cta", "image:next-step", "references"],
        }},
    }
    preview = {
        "available": True, "exactRender": True, "previewUrl": PREVIEW_URL,
        "startedAt": "2026-09-24T11:12:21Z",
    }
    manifest = {"components": [
        {"id": "hero-image", "type": "image", "editable": True},
        {"id": "image:choose-task", "type": "image", "sourceSectionId": "choose-task", "editable": True},
        {"id": "image:configure-example", "type": "image", "sourceSectionId": "configure-example", "editable": True},
        {"id": "image:test-assistant", "type": "image", "sourceSectionId": "test-assistant", "editable": True},
        {"id": "image:next-step", "type": "image", "sourceSectionId": "next-step", "editable": True},
        {"id": "resource-cta", "type": "resource-cta", "editable": True},
        {"id": "references", "type": "references"},
        {"id": "section:test-assistant", "type": "section", "sourceSectionId": "test-assistant"},
    ]}
    return quality, preview, manifest


class HostedQualityIssueTests(unittest.TestCase):
    def test_sixth_shaped_findings_anchor_image_without_guessing_resource_or_disclosure(self):
        quality, preview, manifest = sixth_shaped_report()
        issues = public_hosted_quality_issues(quality, preview, manifest, resume_generation=0)
        self.assertEqual([item["claimId"] for item in issues], ["claim-150", "claim-158", "claim-174"])
        self.assertIsNone(issues[0]["componentId"])
        self.assertEqual(issues[1]["componentId"], "image:test-assistant")
        self.assertEqual(issues[1]["sectionId"], "section:test-assistant")
        self.assertIsNone(issues[2]["componentId"])
        self.assertTrue(all(item["canRemoveSection"] is False for item in issues))

    def test_changed_preview_url_generation_or_deployment_hides_old_issues(self):
        quality, preview, manifest = sixth_shaped_report()
        for edit in (
            {"previewUrl": "https://preview.example/articles/changed"},
            {"resumeGeneration": 1},
            {"startedAt": "2026-09-25T04:00:00Z"},
            {"exactRender": False},
            {"available": False},
        ):
            changed = {**preview, **edit}
            self.assertEqual(public_hosted_quality_issues(quality, changed, manifest, resume_generation=0), [])
        self.assertEqual(public_hosted_quality_issues(quality, preview, manifest, resume_generation=1), [])

    def test_unverified_image_inventory_never_guesses_a_component(self):
        quality, preview, manifest = sixth_shaped_report()
        quality["browser"]["anatomy"]["article_image_count"] = 6
        issues = public_hosted_quality_issues(quality, preview, manifest, resume_generation=0)
        self.assertIsNone(issues[1]["componentId"])

    def test_resource_source_never_proves_a_component_location(self):
        quality, preview, manifest = sixth_shaped_report()
        issues = public_hosted_quality_issues(quality, preview, manifest, resume_generation=0)
        self.assertIsNone(issues[0]["componentId"])

    def test_multiple_review_attempts_fail_closed_until_claim_identity_is_attested(self):
        quality, preview, manifest = sixth_shaped_report()
        review = quality["visible_content_acceptance"]["review"]
        review["attempts"].append(deepcopy(review["attempts"][0]))
        self.assertEqual(public_hosted_quality_issues(quality, preview, manifest, resume_generation=0), [])

    def test_errors_are_bounded_and_redacted(self):
        quality, preview, manifest = sixth_shaped_report()
        changed = deepcopy(quality)
        changed["visible_content_acceptance"]["errors"][1] = "claim-158: Bearer secret-token token=secret-value"
        issues = public_hosted_quality_issues(changed, preview, manifest, resume_generation=0)
        self.assertIn("[redacted]", issues[1]["reason"])
        self.assertNotIn("secret-token", str(issues))
        self.assertNotIn("secret-value", str(issues))


if __name__ == "__main__":
    unittest.main()
