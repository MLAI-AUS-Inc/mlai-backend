"""Contract checks for article section issue presentation and review actions."""

import json
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

    def test_article_wide_evidence_issue_is_visible_but_other_claim_ids_are_rejected(self):
        issues = public_section_issues([
            {
                "section_id": "section:article",
                "claim_id": "evidence-support",
                "state": "needs_review",
                "reason": "Editorial evidence support failed.",
            },
            {
                "section_id": "section:article",
                "claim_id": "anything-else",
                "state": "needs_review",
            },
        ])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["id"], "section:article:evidence-support")
        self.assertEqual(issues[0]["reason"], "Editorial evidence support failed.")

    def test_remote_review_html_is_bounded_before_storage(self):
        oversized = "x" * (views.MAX_REVIEW_DRAFT_HTML_CHARS + 1)
        merged = views._run_result_from_remote({
            "result": {
                "review_draft_html": oversized,
                "review_draft_actions_available": True,
            },
        })
        self.assertNotIn("review_draft_html", merged)
        self.assertIs(merged["review_draft_actions_available"], False)

    def test_remote_status_and_run_expose_reviewable_draft_without_polling_html(self):
        raw = [{
            "sectionId": "section:intro", "claimId": "claim-001",
            "claimExcerpt": "An unsupported sentence.", "state": "needs_review",
            "internal": "never expose",
        }]
        draft_html = '<article><h1>Review me</h1><p data-source-section-id="intro">Draft</p></article>'
        merged = views._run_result_from_remote({
            "status": "failed", "section_issues": raw,
            "review_draft_html": draft_html,
            "review_draft_actions_available": True,
        })
        self.assertEqual(merged["section_issues"], raw)
        self.assertEqual(merged["review_draft_html"], draft_html)
        self.assertIs(merged["review_draft_actions_available"], True)
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1", workflow="direct_generate", domain="mlai.au", github_repo="example/repo",
            status="failed", current_step="ground_section:intro", approval_state="pending",
            resume_available=False, created_at=now, updated_at=now, step_order=[],
            steps=SimpleNamespace(order_by=lambda *args: []), result=merged, run_request={},
            acceptance_summary={}, verification_summary={}, error="Grounding failed",
        )
        with (
            patch.object(views, "_article_setup_state", return_value={}),
            patch.object(views, "_workflow_progress", return_value={}),
            patch.object(views, "_live_preview_from_run", return_value={"available": False}),
            patch.object(views, "_run_content_island_payload", return_value=None),
            patch.object(views, "_article_restart_available", return_value=False),
            patch.object(views, "_run_source_run_id", return_value=""),
            patch.object(views, "_content_package_from_run", return_value=None),
            patch.object(views, "_component_manifest_from_run", return_value=None),
            patch.object(views, "_component_feedback_from_run", return_value={}),
        ):
            compact = views._serialize_run(run, mode="status")
            full = views._serialize_run(run, mode="full")
            run.result = {
                "review_draft_html": {"unexpected": "payload"},
                "review_draft_actions_available": "false",
            }
            malformed = views._serialize_run(run, mode="full")
        self.assertEqual(compact["sectionIssues"][0]["id"], "section:intro:claim-001")
        self.assertNotIn("internal", str(compact["sectionIssues"]))
        self.assertEqual(compact["diagnostics"], {})
        self.assertIs(compact["reviewDraftActionsAvailable"], True)
        self.assertNotIn("reviewDraftHtml", compact)
        self.assertNotIn(draft_html, str(compact))
        self.assertEqual(full["reviewDraftHtml"], draft_html)
        self.assertIs(full["reviewDraftActionsAvailable"], True)
        self.assertNotIn("review_draft_html", full["result"])
        self.assertEqual(merged["review_draft_html"], draft_html)
        self.assertEqual(malformed["reviewDraftHtml"], "")
        self.assertIs(malformed["reviewDraftActionsAvailable"], False)
        self.assertNotIn("review_draft_html", views.COMPACT_RUN_RESULT_KEYS)

    def test_full_article_run_projects_large_review_artifacts_once(self):
        now = datetime.now(timezone.utc)
        draft_html = "<p>Review paragraph</p>" * 8_000
        manifest = {"components": [{"id": "section:intro", "body": "component body " * 5_000}]}
        package = {"title": "Example article", "slug": "example-article", "article_markdown": "draft prose " * 8_000}
        raw_result = {
            "review_draft_html": draft_html,
            "component_manifest": manifest,
            "delivery_package": package,
            "artifacts": [{"detail": "artifact detail " * 1_000}],
            "diagnostics": {"log": "diagnostic line " * 1_000},
            "section_issues": [{"section_id": "section:intro", "claim_id": "claim-001", "state": "needs_review", "reason": "Evidence needs review."}],
            "latest_control_response": {
                "review_draft_html": draft_html,
                "delivery_package": package,
                "publish_child_status": "queued",
            },
            "publish_handoff_pending": True,
            "publish_child_run_id": "publish-1",
            "approval_receipt": {"run_id": "review-1", "approved": False},
        }
        run = SimpleNamespace(
            run_id="review-1", workflow="article_revision", domain="example.test", github_repo="example/repo",
            status="awaiting_approval", current_step="await_review", approval_state="pending",
            resume_available=False, created_at=now, updated_at=now, step_order=[],
            steps=SimpleNamespace(order_by=lambda *args: []), result=raw_result, run_request={},
            acceptance_summary={}, verification_summary={}, error="",
        )
        with (
            patch.object(views, "_article_setup_state", return_value={}),
            patch.object(views, "_workflow_progress", return_value={}),
            patch.object(views, "_live_preview_from_run", return_value={"available": True}),
            patch.object(views, "_run_content_island_payload", return_value=None),
            patch.object(views, "_article_restart_available", return_value=False),
            patch.object(views, "_run_source_run_id", return_value="article-1"),
            patch.object(views, "_content_package_from_run", return_value={"title": "Example article", "contentPackaged": True}),
            patch.object(views, "_component_manifest_from_run", return_value=manifest),
            patch.object(views, "_component_feedback_from_run", return_value={}),
        ):
            serialized = views._serialize_run(run, mode="full")

        self.assertEqual(serialized["reviewDraftHtml"], draft_html)
        self.assertEqual(serialized["componentManifest"], manifest)
        self.assertEqual(serialized["contentPackage"]["title"], "Example article")
        self.assertEqual(serialized["artifacts"], raw_result["artifacts"])
        self.assertEqual(serialized["diagnostics"], raw_result["diagnostics"])
        self.assertEqual(serialized["sectionIssues"][0]["sectionId"], "section:intro")
        self.assertEqual(serialized["approvalState"], "pending")
        self.assertEqual(serialized["sourceRunId"], "article-1")
        self.assertEqual(serialized["result"]["publish_child_run_id"], "publish-1")
        self.assertTrue(serialized["result"]["publish_handoff_pending"])
        self.assertEqual(serialized["result"]["approval_receipt"], raw_result["approval_receipt"])
        self.assertEqual(serialized["result"]["latest_control_response"], {"publish_child_status": "queued"})
        for key in ("review_draft_html", "component_manifest", "delivery_package", "artifacts", "diagnostics", "section_issues"):
            self.assertNotIn(key, serialized["result"])
            self.assertIn(key, raw_result)  # No mutation of the persisted run.
        old_wire_bytes = len(json.dumps({**serialized, "result": raw_result}))
        new_wire_bytes = len(json.dumps(serialized))
        self.assertLess(new_wire_bytes, old_wire_bytes * 0.55)

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
