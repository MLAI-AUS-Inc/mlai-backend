from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from content_factory.article_publish_approval import (
    RECEIPT_KEY,
    RECEIPT_REQUIRED_KEY,
    article_publish_approval_receipt_matches,
    make_article_publish_approval_receipt,
)
from content_factory.vibe_marketing_views import VibeMarketingRunControlView, _article_publish_retry_authorized


def _review_run():
    return SimpleNamespace(
        run_id="revision-1",
        status="awaiting_approval",
        approval_state="approval_required",
        run_request={},
        result={
            "generation": 0,
            "preview_url": "https://preview.example/articles/featured/one",
            "livePreview": {
                "previewUrl": "https://preview.example/articles/featured/one",
                "exactRender": True,
                "resumeGeneration": 0,
                "proof": {"commitSha": "a" * 40},
            },
            "article_preview_quality": {
                "status": "passed", "preview_url": "https://preview.example/articles/featured/one",
                "resume_generation": 0, "inputs_sha256": "b" * 64,
            },
        },
    )


class ArticlePublishApprovalReceiptTests(SimpleTestCase):
    def test_late_worker_approval_records_receipt_and_rejection_keeps_review_gate(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                run = _review_run()
                run.pk = 1
                run.workflow = "article_generation"
                run.save = MagicMock()
                run.refresh_from_db = MagicMock()
                child = SimpleNamespace(run_id="publish-child-1")
                request = SimpleNamespace(data={}, headers={}, user=SimpleNamespace(pk=1))

                def remote_approve(**kwargs):
                    # Model the observed 31-second acknowledgement without a
                    # wall-clock sleep or network/database access.
                    if kwargs["timeout"][1] < 31:
                        return {"error": "Worker acknowledgement timed out."}
                    if rejected:
                        return {"content_factory_status_code": 409, "error": "Review expired."}
                    return {"run_id": child.run_id, "status": "queued"}

                def sync_child(**kwargs):
                    run.approval_state = "approved"
                    return child

                with (
                    patch("content_factory.article_review_views.check_approval_comments", return_value=None),
                    patch("content_factory.article_review_views.record_review_approval") as accept_feedback,
                    patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(SimpleNamespace(organization=object()), None)),
                    patch("content_factory.vibe_marketing_views._get_config", return_value=object()),
                    patch("content_factory.vibe_marketing_views._setup_blocked_response_for_generation", return_value=None),
                    patch("content_factory.incident_guards.publish_child_binding", return_value={}),
                    patch("content_factory.vibe_marketing_views.get_object_or_404", return_value=run),
                    patch("content_factory.vibe_marketing_views._run_belongs_to_context", return_value=True),
                    patch("content_factory.vibe_marketing_views._latest_review_ready_component_revision", return_value=None),
                    patch("content_factory.vibe_marketing_views.founder_actor_id_for_user", return_value="founder-1"),
                    patch("content_factory.vibe_marketing_views.transaction.atomic", return_value=nullcontext()),
                    patch("content_factory.vibe_marketing_views.ContentFactoryRun.objects.select_for_update") as lock,
                    patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=remote_approve),
                    patch("content_factory.vibe_marketing_views._sync_publish_child_from_control_response", side_effect=sync_child) as sync,
                    patch("content_factory.vibe_marketing_views._serialize_run", return_value={"runId": child.run_id}),
                ):
                    lock.return_value.get.return_value = run
                    response = VibeMarketingRunControlView.post.__wrapped__(
                        VibeMarketingRunControlView(), request, run.run_id, "approve",
                    )

                self.assertTrue(run.run_request[RECEIPT_REQUIRED_KEY])
                if rejected:
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(run.approval_state, "approval_required")
                    self.assertNotIn(RECEIPT_KEY, run.run_request)
                    sync.assert_not_called()
                    accept_feedback.assert_not_called()
                else:
                    self.assertEqual(response.status_code, 202)
                    self.assertTrue(article_publish_approval_receipt_matches(run))
                    self.assertTrue(_article_publish_retry_authorized(run))
                    accept_feedback.assert_called_once()

    def test_a_quality_rejection_replies_with_founder_copy_and_keeps_staff_diagnostics(self):
        run = _review_run()
        run.pk = 1
        run.workflow = "article_generation"
        run.save = MagicMock()
        run.refresh_from_db = MagicMock()
        blocker = {"code": "article_preview_editorial_review_blocked", "message": "content:visible_editorial:claim_7 unsupported",
                   "founder_message": "Some statements or links still need an evidence check.", "findings": ["claim_7"]}
        request = SimpleNamespace(data={}, headers={}, user=SimpleNamespace(pk=1))
        with (
            patch("content_factory.article_review_views.check_approval_comments", return_value=None),
            patch("content_factory.article_review_views.record_review_approval"),
            patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(SimpleNamespace(organization=object()), None)),
            patch("content_factory.vibe_marketing_views._get_config", return_value=object()),
            patch("content_factory.vibe_marketing_views._setup_blocked_response_for_generation", return_value=None),
            patch("content_factory.incident_guards.publish_child_binding", return_value={}),
            patch("content_factory.vibe_marketing_views.get_object_or_404", return_value=run),
            patch("content_factory.vibe_marketing_views._run_belongs_to_context", return_value=True),
            patch("content_factory.vibe_marketing_views._latest_review_ready_component_revision", return_value=None),
            patch("content_factory.vibe_marketing_views.founder_actor_id_for_user", return_value="founder-1"),
            patch("content_factory.vibe_marketing_views.transaction.atomic", return_value=nullcontext()),
            patch("content_factory.vibe_marketing_views.ContentFactoryRun.objects.select_for_update") as lock,
            patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value={
                "content_factory_status_code": 409, "error": blocker["message"], "approval_blocker": blocker}),
        ):
            lock.return_value.get.return_value = run
            response = VibeMarketingRunControlView.post.__wrapped__(
                VibeMarketingRunControlView(), request, run.run_id, "approve",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], blocker["founder_message"])
        self.assertEqual(response.data["error"], blocker["founder_message"])
        self.assertNotIn("claim_7", str(response.data))
        self.assertEqual(run.result["approval_blocker"]["message"], blocker["message"], "staff diagnostics are stored")
        self.assertEqual(run.result["latest_control_response"]["error"], blocker["message"])

    def test_passing_preview_does_not_authorize_direct_promotion(self):
        run = _review_run()
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_explicit_approve_receipt_allows_same_review_retry(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertEqual(receipt["run_id"], run.run_id)
        self.assertEqual(receipt["commit_sha"], "a" * 40)
        run.approval_state = "approved"
        run.run_request[RECEIPT_REQUIRED_KEY] = True
        run.run_request[RECEIPT_KEY] = receipt
        self.assertTrue(article_publish_approval_receipt_matches(run))
        self.assertTrue(_article_publish_retry_authorized(run))

    def test_new_approval_without_receipt_cannot_use_legacy_child_retry(self):
        run = _review_run()
        run.approval_state = "approved"
        run.result["publish_child_run_id"] = "publish-child-1"
        run.run_request[RECEIPT_REQUIRED_KEY] = True
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_postapprove_identity_drift_returns_conflict_with_or_without_child(self):
        for has_child in (False, True):
            with self.subTest(has_child=has_child):
                run = _review_run()
                run.pk = 1
                run.workflow = "article_generation"
                run.save = MagicMock()
                run.refresh_from_db = MagicMock()
                current = _review_run()
                current.pk = 1
                current.workflow = "article_generation"
                current.save = MagicMock()
                current.refresh_from_db = MagicMock()
                request = SimpleNamespace(data={}, headers={}, user=SimpleNamespace(pk=1))

                def remote_approve(**_kwargs):
                    self.assertTrue(current.run_request[RECEIPT_REQUIRED_KEY])
                    return {
                        "run_id": "publish-child-1" if has_child else run.run_id,
                        "status": "completed",
                    }

                lock_reads = 0

                def locked_current(**_kwargs):
                    nonlocal lock_reads
                    lock_reads += 1
                    if lock_reads == 2:
                        # A status poll advanced the saved generation after CF
                        # approved and the local action synced its response.
                        current.approval_state = "approved"
                        current.result["generation"] = 1
                        current.result["publish_child_run_id"] = "publish-child-1"
                    return current

                with (
                    patch("content_factory.article_review_views.check_approval_comments", return_value=None),
                    patch("content_factory.article_review_views.record_review_approval") as accept_feedback,
                    patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(SimpleNamespace(organization=object()), None)),
                    patch("content_factory.vibe_marketing_views._get_config", return_value=object()),
                    patch("content_factory.vibe_marketing_views._setup_blocked_response_for_generation", return_value=None),
                    patch("content_factory.incident_guards.publish_child_binding", return_value={}),
                    patch("content_factory.vibe_marketing_views.get_object_or_404", return_value=run),
                    patch("content_factory.vibe_marketing_views._run_belongs_to_context", return_value=True),
                    patch("content_factory.vibe_marketing_views._latest_review_ready_component_revision", return_value=None),
                    patch("content_factory.vibe_marketing_views.founder_actor_id_for_user", return_value="founder-1"),
                    patch("content_factory.vibe_marketing_views.transaction.atomic", return_value=nullcontext()),
                    patch("content_factory.vibe_marketing_views.ContentFactoryRun.objects.select_for_update") as lock,
                    patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=remote_approve),
                    patch(
                        "content_factory.vibe_marketing_views._sync_publish_child_from_control_response",
                        return_value=SimpleNamespace(run_id="publish-child-1") if has_child else None,
                    ),
                ):
                    lock.return_value.get.side_effect = locked_current
                    # This database-free unit exercises approval identity drift
                    # after authorization. The lifecycle/database suites cover
                    # the outer website-consent decorator with real requests.
                    response = VibeMarketingRunControlView.post.__wrapped__(
                        VibeMarketingRunControlView(), request, run.run_id, "approve",
                    )

                self.assertEqual(response.status_code, 409)
                accept_feedback.assert_not_called()
                self.assertEqual(lock_reads, 2)
                self.assertIn("reviewed preview changed", response.data["detail"])
                self.assertEqual(current.result["livePreview"]["resumeGeneration"], 0)
                self.assertEqual(current.result["article_preview_quality"]["resume_generation"], 0)
                self.assertNotIn(RECEIPT_KEY, current.run_request)
                self.assertFalse(_article_publish_retry_authorized(current))

    def test_receipt_rejects_changed_run_preview_commit_and_quality(self):
        for changed in ("run_id", "preview_url", "commit_sha", "resume_generation", "quality_inputs_sha256"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(run, actor_id="founder-1")
                run.approval_state = "approved"
                if changed == "run_id":
                    run.run_id = "revision-2"
                elif changed == "preview_url":
                    run.result["livePreview"]["previewUrl"] = "https://preview.example/articles/featured/two"
                elif changed == "commit_sha":
                    run.result["livePreview"]["proof"]["commitSha"] = "c" * 40
                elif changed == "resume_generation":
                    run.result["livePreview"]["resumeGeneration"] = 1
                else:
                    run.result["article_preview_quality"]["inputs_sha256"] = "d" * 64
                run.result["publish_child_run_id"] = "publish-child-1"
                self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_changed_run_generation_with_unchanged_hosted_review(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertEqual(receipt["run_generation"], 0)
        run.approval_state = "approved"
        run.run_request[RECEIPT_KEY] = receipt
        run.result["generation"] = 1
        self.assertEqual(run.result["livePreview"]["resumeGeneration"], 0)
        self.assertEqual(run.result["article_preview_quality"]["resume_generation"], 0)
        self.assertFalse(article_publish_approval_receipt_matches(run))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_regressed_quality_status_with_unchanged_hash(self):
        for status in ("blocking_findings", "queued", "self_healing"):
            with self.subTest(status=status):
                run = _review_run()
                receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
                run.approval_state = "approved"
                run.run_request[RECEIPT_KEY] = receipt
                run.result["article_preview_quality"]["status"] = status
                self.assertEqual(run.result["article_preview_quality"]["inputs_sha256"], receipt["quality_inputs_sha256"])
                self.assertFalse(article_publish_approval_receipt_matches(run))
                self.assertFalse(_article_publish_retry_authorized(run))

        run = _review_run()
        run.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(run, actor_id="founder-1")
        run.approval_state = "approved"
        run.result["article_preview_quality"]["status"] = "advisory_findings"
        self.assertTrue(_article_publish_retry_authorized(run))

    def test_no_baseline_quality_requires_hash_and_allows_exact_retry(self):
        run = _review_run()
        run.result["article_preview_quality"]["status"] = "passed_no_baseline"
        # The run lease generation and hosted preview attempt are separate.
        run.result["generation"] = 1
        run.result["livePreview"]["resumeGeneration"] = "0"
        run.result["article_preview_quality"]["resume_generation"] = "0"
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["run_generation"], 1)
        self.assertEqual(receipt["resume_generation"], 0)
        self.assertEqual(receipt["quality_inputs_sha256"], "b" * 64)
        run.approval_state = "approved"
        run.run_request[RECEIPT_KEY] = receipt
        self.assertTrue(article_publish_approval_receipt_matches(run))
        self.assertTrue(_article_publish_retry_authorized(run))

        run.result["article_preview_quality"]["inputs_sha256"] = "c" * 64
        self.assertFalse(_article_publish_retry_authorized(run))

        run.result["article_preview_quality"].pop("inputs_sha256")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_rejects_render_or_quality_target_drift(self):
        for changed in ("exact_render", "quality_preview_url", "quality_generation"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.result["article_preview_quality"]["status"] = "passed_no_baseline"
                receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
                self.assertIsNotNone(receipt)
                run.approval_state = "approved"
                run.run_request[RECEIPT_KEY] = receipt
                if changed == "exact_render":
                    run.result["livePreview"]["exactRender"] = False
                elif changed == "quality_preview_url":
                    run.result["article_preview_quality"]["preview_url"] = "https://preview.example/articles/other"
                else:
                    run.result["article_preview_quality"]["resume_generation"] = 1
                self.assertFalse(article_publish_approval_receipt_matches(run))
                self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_creation_rejects_inexact_or_stale_hosted_quality(self):
        for changed in ("exact_render", "quality_preview_url", "quality_generation", "missing_quality"):
            with self.subTest(changed=changed):
                run = _review_run()
                run.result["article_preview_quality"]["status"] = "passed_no_baseline"
                if changed == "exact_render":
                    run.result["livePreview"]["exactRender"] = False
                elif changed == "quality_preview_url":
                    run.result["article_preview_quality"]["preview_url"] = "https://preview.example/articles/other"
                elif changed == "quality_generation":
                    run.result["livePreview"]["resumeGeneration"] = 1
                else:
                    run.result.pop("article_preview_quality")
                self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))

    def test_legacy_retry_requires_prior_approval_and_known_child(self):
        run = _review_run()
        run.result["publish_child_run_id"] = "publish-child-1"
        self.assertFalse(_article_publish_retry_authorized(run))
        run.approval_state = "approved"
        self.assertTrue(_article_publish_retry_authorized(run))
        for status in ("blocking_findings", "queued", "self_healing"):
            run.result["article_preview_quality"]["status"] = status
            self.assertTrue(_article_publish_retry_authorized(run))
        del run.result["publish_child_run_id"]
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_requires_hosted_proof_not_fallback_or_forged_commit(self):
        run = _review_run()
        receipt = make_article_publish_approval_receipt(run, actor_id="founder-1")
        del run.result["livePreview"]["proof"]
        run.result["livePreview"]["commitSha"] = "a" * 40
        run.result["branch_commit_sha"] = "a" * 40
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.approval_state = "approved"
        run.result["publish_child_run_id"] = "publish-child-1"
        run.run_request[RECEIPT_KEY] = receipt
        self.assertFalse(article_publish_approval_receipt_matches(run))
        self.assertFalse(_article_publish_retry_authorized(run))

    def test_receipt_requires_valid_commit_and_applicable_quality_hash(self):
        run = _review_run()
        run.result["livePreview"]["proof"]["commitSha"] = "not-a-commit"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["livePreview"]["proof"]["commitSha"] = "a" * 40
        run.result["article_preview_quality"].pop("inputs_sha256")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["article_preview_quality"]["status"] = "advisory_findings"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result["article_preview_quality"]["status"] = "passed_no_baseline"
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))
        run.result.pop("article_preview_quality")
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))


class HostedQualityPublishStateTests(SimpleTestCase):
    def test_self_healing_locks_publishing_and_clears_local_and_remote_stale_blockers(self):
        from content_factory.hosted_quality_state import preview_quality_publish_notice
        from content_factory.vibe_marketing_views import _merge_django_owned_article_result
        quality = {"status": "self_healing", "self_heal_attempt": 1}
        self.assertEqual(preview_quality_publish_notice(quality),
                         ("locked", "We're fixing a few things in the preview automatically."))
        merged = _merge_django_owned_article_result(
            {"approval_blocker": {"message": "An earlier rejection"}},
            {"article_preview_quality": quality, "approval_blocker": {"message": "A stale remote rejection"}})
        self.assertNotIn("approval_blocker", merged)
        self.assertEqual(merged["article_preview_quality"], quality)
        run = _review_run()
        run.result["article_preview_quality"] = quality
        self.assertIsNone(make_article_publish_approval_receipt(run, actor_id="founder-1"))

    def test_exhausted_safety_uses_staff_copy_and_never_projects_raw_claims(self):
        from content_factory.hosted_quality_state import SAFETY_MESSAGE, preview_quality_publish_notice
        from content_factory.hosted_quality_issues import public_hosted_quality_issues
        from content_factory.tests_hosted_quality_issues_unit import sixth_shaped_report
        quality, preview, manifest = sixth_shaped_report()
        quality.update(self_heal_exhausted=True, escalation="safety_review", founder_message="Raw safety details")
        self.assertEqual(preview_quality_publish_notice(quality), ("blocked", SAFETY_MESSAGE))
        self.assertEqual(public_hosted_quality_issues(quality, preview, manifest), [])

    def test_specific_terminal_message_precedes_the_legacy_neutral_fallback(self):
        from django.test import override_settings
        from content_factory.hosted_quality_state import (
            FALLBACK_MESSAGE, UNALERTED_FALLBACK_MESSAGE, preview_quality_publish_notice)
        quality = {"status": "blocking_findings", "self_heal_exhausted": True,
                   "founder_message": "The preview is missing part of the article."}
        self.assertEqual(preview_quality_publish_notice(quality), ("blocked", quality["founder_message"]))
        quality.pop("founder_message")
        # "Our team has been notified" is only true while staff alerts are on.
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True):
            self.assertEqual(preview_quality_publish_notice(quality), ("blocked", FALLBACK_MESSAGE))
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            self.assertEqual(preview_quality_publish_notice(quality), ("blocked", UNALERTED_FALLBACK_MESSAGE))
        self.assertNotIn("notified", UNALERTED_FALLBACK_MESSAGE)

    def test_a_check_that_failed_to_run_blocks_publishing(self):
        from content_factory.hosted_quality_state import FAILED_TO_RUN_MESSAGE, preview_quality_publish_notice
        for status in ("review_error", "queue_failed"):
            with self.subTest(status=status):
                self.assertEqual(preview_quality_publish_notice({"status": status}), ("blocked", FAILED_TO_RUN_MESSAGE))
        self.assertIsNone(preview_quality_publish_notice({"status": "passed"}))


class FounderQualityProjectionTests(SimpleTestCase):
    BLOCKER = {"code": "article_preview_editorial_review_blocked", "message": "content:visible_editorial:claim_7 unsupported",
               "founder_message": "A few details need checking before publishing.", "findings": ["content:visible_editorial:claim_7"],
               "repair_instructions": ["Rebind claim_7"], "retryable": True, "retry_action": "/api/runs/r/retry-preview-quality",
               "detail": "raw", "content_factory_status_code": 409}
    QUALITY = {"status": "blocking_findings", "escalation": "quality_review", "self_heal_exhausted": True,
               "founder_message": "A few details need checking before publishing.", "preview_url": "https://p.test/a",
               "resume_generation": 3, "findings": ["editorial:score_missing", "safety_unverified: Call 000 now"],
               "safety_findings": ["safety_unverified: Call 000 now"], "repair_instructions": ["Rebind claim_7"],
               "self_heal_history": [{"result": "x"}], "visible_content_acceptance": {"review": {"errors": ["raw"]}}}

    def test_blocker_keeps_only_code_and_founder_copy(self):
        from content_factory.hosted_quality_state import founder_blocker
        view = founder_blocker(self.BLOCKER)
        self.assertEqual(set(view), {"code", "founder_message", "retryable", "retry_action"})
        self.assertNotIn("message", view)

    def test_blocker_without_founder_copy_never_shows_the_staff_message(self):
        from content_factory.hosted_quality_state import UNALERTED_FALLBACK_MESSAGE, founder_blocker
        blocker = {"code": "article_preview_quality_running", "message": "Hosted article quality verification is running."}
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            self.assertEqual(founder_blocker(blocker)["founder_message"], UNALERTED_FALLBACK_MESSAGE)

    def test_notified_copy_follows_whether_staff_alerts_are_on(self):
        from content_factory.hosted_quality_state import (
            FALLBACK_MESSAGE, UNALERTED_FALLBACK_MESSAGE, founder_blocker, founder_quality)
        blocker = {"code": "article_preview_quality_blocked", "founder_message": FALLBACK_MESSAGE}
        quality = {"status": "blocking_findings", "founder_message": FALLBACK_MESSAGE}
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            self.assertEqual(founder_blocker(blocker)["founder_message"], UNALERTED_FALLBACK_MESSAGE)
            self.assertEqual(founder_quality(quality)["founder_message"], UNALERTED_FALLBACK_MESSAGE)
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True):
            self.assertEqual(founder_blocker(blocker)["founder_message"], FALLBACK_MESSAGE)
            self.assertEqual(founder_quality(quality)["founder_message"], FALLBACK_MESSAGE)

    def test_quality_drops_staff_diagnostics_and_keeps_founder_fields(self):
        from content_factory.hosted_quality_state import founder_quality
        view = founder_quality(self.QUALITY)
        self.assertEqual(view["findings"], ["editorial:score_missing"])
        for key in ("safety_findings", "repair_instructions", "self_heal_history", "visible_content_acceptance"):
            self.assertNotIn(key, view)
        for key in ("status", "escalation", "self_heal_exhausted", "founder_message", "preview_url", "resume_generation"):
            self.assertEqual(view[key], self.QUALITY[key])

    def test_result_projection_reaches_nested_copies_and_leaves_other_keys(self):
        from content_factory.hosted_quality_state import founder_result
        result = {"approval_blocker": self.BLOCKER, "article_preview_quality": self.QUALITY, "title": "T",
                  "latest_control_response": {"approval_blocker": self.BLOCKER},
                  "result": {"articlePreviewQuality": self.QUALITY}}
        view = founder_result(result)
        import json
        text = json.dumps(view)
        for raw in ("claim_7", "Call 000 now", "Rebind", "\"raw\""):
            self.assertNotIn(raw, text)
        self.assertEqual(view["title"], "T")
        self.assertEqual(result["approval_blocker"]["message"], self.BLOCKER["message"], "input is not mutated")
        self.assertIsNone(founder_result(None))

    def test_staff_rules_detect_exhausted_quality_on_an_awaiting_approval_run(self):
        from django.utils import timezone
        from content_factory.staff_alert_rules import _run_reasons
        from content_factory.founder_journey_alerts import ALERT_REASONS
        for escalation, reason in (("safety_review", "article_safety_review_required"),
                                   ("quality_review", "article_quality_review_required")):
            run = SimpleNamespace(result={"article_preview_quality": {"status": "blocking_findings",
                                  "self_heal_exhausted": True, "escalation": escalation}},
                                  status="awaiting_approval", run_request={})
            self.assertIn(reason, _run_reasons(run, timezone.now()))
            self.assertIn(reason, ALERT_REASONS)
            run.result["article_preview_quality"]["status"] = "self_healing"
            self.assertNotIn(reason, _run_reasons(run, timezone.now()))


@override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True,
                   ARTICLES_STAFF_ALERT_RULES_ENABLED=False,
                   ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED=False,
                   ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False,
                   MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=False,
                   MY_STARTUP_TURN_REMINDERS_ENABLED=False)
class HostedQualityOwnerAlertTests(SimpleTestCase):
    def setUp(self):
        from copy import deepcopy
        self.run = _review_run()
        self.run.pk, self.run.organization_id, self.run.workflow = 17, 23, "article_generation"
        self.run.organization = SimpleNamespace(pk=23)
        self.run.result["article_preview_quality"].update(
            status="blocking_findings", self_heal_exhausted=True, escalation="safety_review")
        self.current = deepcopy(self.run)
        self.manager = self.enterContext(patch("content_factory.hosted_quality_alerts.ContentFactoryRun.objects"))
        self.manager.select_related.return_value.filter.return_value.first.return_value = self.current
        self.callbacks = []
        self.commit = self.enterContext(patch("content_factory.hosted_quality_alerts.transaction.on_commit",
                                             side_effect=self.callbacks.append))
        self.queue = self.enterContext(patch("content_factory.hosted_quality_alerts.queue_founder_journey_alert"))
        self.enterContext(patch("content_factory.run_reconciliation.authoritative_run", return_value=False))

    def test_both_owner_reasons_wait_for_commit_and_use_the_fresh_saved_run(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        for escalation, reason in (("safety_review", "article_safety_review_required"),
                                   ("quality_review", "article_quality_review_required")):
            with self.subTest(escalation=escalation):
                self.run.result["article_preview_quality"]["escalation"] = escalation
                self.current.result["article_preview_quality"]["escalation"] = escalation
                queue_hosted_quality_review_alert(self.run)
                self.queue.assert_not_called()
                self.callbacks.pop()()
                self.queue.assert_called_once_with(
                    organization=self.current.organization, run=self.current, alert_reason=reason)
                self.queue.reset_mock()

    def test_a_new_generation_input_preview_or_stop_wins_before_commit(self):
        from copy import deepcopy
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        changes = (
            lambda r: r.result["article_preview_quality"].update(inputs_sha256="c" * 64),
            lambda r: r.result["article_preview_quality"].update(status="self_healing"),
            lambda r: r.result["livePreview"].update(resumeGeneration=1),
            lambda r: r.result["livePreview"].update(previewUrl="https://preview.example/new"),
            lambda r: setattr(r, "status", "cancelled"),
            lambda r: setattr(r, "organization_id", 99),
        )
        for change in changes:
            with self.subTest(change=change):
                current = deepcopy(self.run)
                self.manager.select_related.return_value.filter.return_value.first.return_value = current
                queue_hosted_quality_review_alert(self.run)
                change(current)
                self.callbacks.pop()()
                self.queue.assert_not_called()

    def test_unverified_owned_metadata_cannot_fall_back_to_a_legacy_alert(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        queue_hosted_quality_review_alert(self.run)
        with patch("content_factory.run_reconciliation.authoritative_run", return_value=True), \
             patch("content_factory.run_review_quality.current_review_verified", return_value=False) as proof:
            self.callbacks.pop()()
        proof.assert_called_once_with(self.current)
        self.queue.assert_not_called()

    def test_primary_gate_inflight_portable_and_scheduler_states_do_not_queue(self):
        from copy import deepcopy
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            queue_hosted_quality_review_alert(self.run)
        for change in (
            lambda r: r.result["article_preview_quality"].update(self_heal_exhausted=False),
            lambda r: setattr(r, "status", "running"),
            lambda r: r.run_request.update(request_source="mlai_backend_scheduler"),
            lambda r: r.run_request.update(delivery_mode="content_only", delivery_mode_confirmed=True),
            lambda r: r.result["article_preview_quality"].update(reviewed_preview_commit_sha="c" * 40),
        ):
            run = deepcopy(self.run)
            change(run)
            queue_hosted_quality_review_alert(run)
        self.commit.assert_not_called()
        self.manager.select_related.assert_not_called()
        self.queue.assert_not_called()

    def test_a_current_owned_stop_intent_wins_before_commit(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        queue_hosted_quality_review_alert(self.run)
        with patch("content_factory.run_ownership.user_stop_intent", return_value={"denied": True}):
            self.callbacks.pop()()
        self.queue.assert_not_called()

    def test_existing_failed_or_unconfigured_receipts_are_not_reset(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        from content_factory.founder_journey_alerts import founder_journey_alert_id
        reason = "article_safety_review_required"
        identifier = founder_journey_alert_id(
            organization_id=self.run.organization_id, alert_reason=reason, run_id=self.run.run_id)
        for state in ("failed", "unconfigured", "sent"):
            self.run.result["ops_alerts"] = {reason: {"alert_id": identifier, "state": state}}
            queue_hosted_quality_review_alert(self.run)
        self.commit.assert_not_called()
        self.queue.assert_not_called()

    def test_delivery_queue_failure_does_not_expose_raw_errors_or_fail_the_status_poll(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        self.queue.side_effect = RuntimeError("secret clinical/provider text")
        with patch("content_factory.hosted_quality_alerts.logger.warning") as log:
            queue_hosted_quality_review_alert(self.run)
            self.callbacks.pop()()
        self.assertNotIn("secret clinical", repr(log.call_args))
        self.assertIn("RuntimeError", repr(log.call_args))

    def test_deleted_runs_and_a_primary_gate_closed_before_commit_do_not_queue(self):
        from content_factory.hosted_quality_alerts import queue_hosted_quality_review_alert
        queue_hosted_quality_review_alert(self.run)
        self.manager.select_related.return_value.filter.return_value.first.return_value = None
        self.callbacks.pop()()
        self.manager.select_related.return_value.filter.return_value.first.return_value = self.current
        queue_hosted_quality_review_alert(self.run)
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            self.callbacks.pop()()
        self.queue.assert_not_called()


class HostedQualitySyncHookTests(SimpleTestCase):
    def test_owned_observations_use_the_refreshed_run_and_never_the_remote_snapshot(self):
        from content_factory.vibe_marketing_views import _sync_local_run_from_remote
        original, current = _review_run(), _review_run()
        with patch("content_factory.run_owned_observations.refresh_owned_run", return_value=current), \
             patch("content_factory.hosted_quality_alerts.queue_hosted_quality_review_alert") as queue:
            self.assertIs(_sync_local_run_from_remote(original, {"forged": "snapshot"}), current)
        queue.assert_called_once_with(current)

    def test_legacy_sync_releases_its_run_lock_before_queuing_and_stale_input_never_queues(self):
        from contextlib import contextmanager
        from content_factory.vibe_marketing_views import _sync_local_run_from_remote
        run = _review_run()
        run.pk, run.workflow, run.refresh_from_db = 1, "article_generation", MagicMock()
        order = []

        @contextmanager
        def atomic():
            order.append("lock")
            try:
                yield
            finally:
                order.append("unlock")

        with patch("content_factory.run_owned_observations.refresh_owned_run", return_value=None), \
             patch("content_factory.vibe_marketing_views.ContentFactoryRun.objects") as model, \
             patch("content_factory.vibe_marketing_views.scoped_run_contract", return_value={}), \
             patch("content_factory.vibe_marketing_views.authority_guard", return_value=nullcontext()), \
             patch("content_factory.vibe_marketing_views.transaction.atomic", side_effect=atomic), \
             patch("content_factory.portable_drafts.portable_run_update_allowed", return_value=False), \
             patch("content_factory.run_state.stale_execution_event", return_value=False) as stale, \
             patch("content_factory.vibe_marketing_views._sync_local_run_from_remote_locked", return_value=run), \
             patch("content_factory.first_article_entitlement.settle_entitlement_for_run",
                   side_effect=lambda _: order.append("settle")), \
             patch("content_factory.hosted_quality_alerts.queue_hosted_quality_review_alert",
                   side_effect=lambda _: order.append("alert")) as queue:
            self.assertIs(_sync_local_run_from_remote(run, {"status": "approval_required"}), run)
            self.assertEqual(order, ["lock", "unlock", "settle", "alert"])
            model.select_for_update.return_value.get.assert_called_once_with(pk=run.pk)
            order.clear()
            queue.reset_mock()
            stale.return_value = True
            self.assertIs(_sync_local_run_from_remote(run, {"status": "approval_required"}), run)
            self.assertEqual(order, ["lock", "unlock"])
            queue.assert_not_called()


class FounderRunResponseProjectionTests(SimpleTestCase):
    def _serialize(self, result):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import patch
        from content_factory import vibe_marketing_views as views
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(run_id="held-article", workflow="article_generation", domain="example.test",
            github_repo="fixture/repo", status="awaiting_approval", current_step="await_review",
            approval_state="approval_required", resume_available=False, created_at=now, updated_at=now,
            step_order=[], steps=SimpleNamespace(order_by=lambda *args: [], all=lambda: []),
            result=result, run_request={}, acceptance_summary={}, verification_summary={}, error="")
        with (
            patch.object(views, "_run_source_run_id", return_value=""),
            patch.object(views, "_article_restart_available", return_value=False),
            patch.object(views, "_article_setup_state", return_value={}),
            patch.object(views, "_workflow_progress", return_value={}),
            patch.object(views, "_content_package_from_run", return_value=None),
            patch.object(views, "_component_feedback_from_run", return_value={}),
            patch.object(views, "_component_manifest_from_run", return_value=None),
            patch.object(views, "_run_content_island_payload", return_value=None),
        ):
            return {mode: views._serialize_run(run, mode=mode)["result"] for mode in ("summary", "status", "full")}

    def test_every_run_response_mode_carries_only_founder_quality_copy(self):
        import json
        blocker, quality = FounderQualityProjectionTests.BLOCKER, FounderQualityProjectionTests.QUALITY
        for mode, result in self._serialize({"approval_blocker": blocker, "article_preview_quality": quality,
                                             "latest_control_response": {"approval_blocker": blocker}}).items():
            with self.subTest(mode=mode):
                text = json.dumps(result, default=str)
                for raw in ("claim_7", "Call 000 now", "Rebind"):
                    self.assertNotIn(raw, text)

    def test_malformed_findings_are_omitted_instead_of_failing_the_response(self):
        quality = {"status": "blocking_findings", "founder_message": "Checking.",
                   "findings": ["editorial:score_missing", {"code": "x"}, ["nested"], None, 7, "content:raw"]}
        for mode, result in self._serialize({"article_preview_quality": quality}).items():
            with self.subTest(mode=mode):
                if mode == "full" or "article_preview_quality" in result:
                    self.assertEqual(result["article_preview_quality"]["findings"], ["editorial:score_missing"])

    def test_rejected_approval_control_response_carries_founder_copy_only(self):
        import json
        blocker = FounderQualityProjectionTests.BLOCKER
        control = {"error": blocker["message"], "errors": [blocker["message"]], "approval_blocker": blocker,
                   "content_factory_status_code": 409, "content_factory_response": {"detail": blocker}}
        for mode, result in self._serialize({"approval_blocker": {**blocker, "detail": blocker["message"]},
                                             "latest_control_response": control}).items():
            with self.subTest(mode=mode):
                text = json.dumps(result, default=str)
                self.assertNotIn("claim_7", text)
                if mode != "full" and "latest_control_response" not in result:
                    continue
                view = result["latest_control_response"]
                self.assertEqual(view["error"], blocker["founder_message"])
                self.assertEqual(view["errors"], [blocker["founder_message"]])
                self.assertEqual(view["content_factory_response"]["detail"]["founder_message"], blocker["founder_message"])
                self.assertEqual(view["content_factory_status_code"], 409)

    def test_non_quality_blockers_and_control_errors_pass_through(self):
        blocker = {"code": "publish_auto_merge_blocked", "message": "Repository rules block auto-merge.", "retryable": False}
        control = {"error": "Repository rules block auto-merge.", "content_factory_response": {"detail": "x"}}
        for mode, result in self._serialize({"approval_blocker": blocker, "latest_control_response": control}).items():
            with self.subTest(mode=mode):
                if mode != "full":
                    continue
                self.assertEqual(result["approval_blocker"], blocker)
                self.assertEqual(result["latest_control_response"], control)
