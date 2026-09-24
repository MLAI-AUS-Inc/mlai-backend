"""Contract checks for article section issue presentation and review actions."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from content_factory.section_issues import public_section_issues
from content_factory import vibe_marketing_views as views


class SectionIssueContractTests(SimpleTestCase):
    def test_projection_is_bounded_and_never_returns_raw_diagnostics(self):
        issues = public_section_issues(
            [
                {
                    "id": "untrusted-id",
                    "section_id": "section:approve-tool",
                    "claim_id": "claim-003",
                    "claim_excerpt": "A claim with token=secret-value that needs a source.",
                    "reason": "Unsupported. Bearer very-secret-token",
                    "state": "needs_review",
                    "source_hint": "Look at the source bundle.",
                    "artifact_root": "/private/artifacts",
                },
                {"sectionId": "section:../../private", "claimId": "claim-004", "state": "needs_review"},
            ]
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["id"], "section:approve-tool:claim-003")
        self.assertEqual(issues[0]["sectionId"], "section:approve-tool")
        self.assertNotIn("secret-value", str(issues))
        self.assertNotIn("very-secret-token", str(issues))
        self.assertNotIn("artifact_root", issues[0])

    def test_remote_status_and_compact_run_keep_only_typed_section_issues(self):
        raw = [{
            "sectionId": "section:intro", "claimId": "claim-001",
            "claimExcerpt": "An unsupported sentence.", "state": "needs_review",
            "internal": "never expose",
        }]
        merged = views._run_result_from_remote({"status": "failed", "section_issues": raw})
        self.assertEqual(merged["section_issues"], raw)
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1", workflow="direct_generate", domain="mlai.au", github_repo="example/repo",
            status="failed", current_step="ground_section:intro", approval_state="pending",
            resume_available=False, created_at=now, updated_at=now, step_order=[],
            steps=SimpleNamespace(order_by=lambda *args: []), result=merged, run_request={},
            acceptance_summary={}, error="Grounding failed",
        )
        with (
            patch.object(views, "_article_setup_state", return_value={}),
            patch.object(views, "_workflow_progress", return_value={}),
            patch.object(views, "_live_preview_from_run", return_value={"available": False}),
            patch.object(views, "_run_content_island_payload", return_value=None),
            patch.object(views, "_article_restart_available", return_value=False),
            patch.object(views, "_run_source_run_id", return_value=""),
        ):
            serialized = views._serialize_run(run, mode="status")
        self.assertEqual(serialized["sectionIssues"][0]["id"], "section:intro:claim-001")
        self.assertNotIn("internal", str(serialized["sectionIssues"]))
        self.assertEqual(serialized["diagnostics"], {})

    def test_delete_action_is_explicit_and_bound_to_exact_section(self):
        input_payload = {
            "componentId": "section:approve-tool",
            "sourceSectionId": "approve-tool",
            "body": "Delete this unsupported section.",
            "requestedAction": "delete_section",
        }
        comment_payload = views._comment_payload_from_request(input_payload)
        self.assertIsNone(views._component_comment_action_error(input_payload, comment_payload))
        self.assertEqual(comment_payload["context"]["requestedAction"], "delete_section")
        comment = SimpleNamespace(
            id="comment-1", component_id="section:approve-tool", component_type="section",
            component_label="Approve a tool", source_section_id="approve-tool", selector="",
            anchor={}, context=comment_payload["context"], body=comment_payload["body"],
        )
        self.assertEqual(views._remote_comment_payload(comment)["requested_action"], "delete_section")
        self.assertEqual(
            views._component_comment_action_error(
                {**input_payload, "sourceSectionId": "different"},
                views._comment_payload_from_request({**input_payload, "sourceSectionId": "different"}),
            ),
            "The selected section and source section do not match.",
        )
        self.assertEqual(
            views._component_comment_action_error(
                {**input_payload, "componentId": "section:../evil"},
                views._comment_payload_from_request({**input_payload, "componentId": "section:../evil"}),
            ),
            "Choose an exact article section before deleting it.",
        )
        ordinary = {**input_payload, "requestedAction": ""}
        self.assertNotIn("requestedAction", views._comment_payload_from_request(ordinary)["context"])
