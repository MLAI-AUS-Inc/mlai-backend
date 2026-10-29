"""Run-scoped portable progress tests with synthetic seams; no DB or network."""
from contextlib import ExitStack, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import vibe_marketing_views as views


class PortableWorkflowProgressTests(SimpleTestCase):
    def run_progress(self, *, status="completed", request=None, bound_repo="", scoped=True):
        run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo=bound_repo,
            workflow="article_generation", status=status, current_step="finalize", result={},
            run_request=request if request is not None else {
                "delivery_mode": "content_only", "delivery_mode_confirmed": True,
            },
            component_comments=Mock(),
        )
        checks = {name: {"passed": True} for name in ("websiteProfile", "baseline", "github", "research")}
        checks["scaffold"] = {
            "passed": False, "setupBlocked": True, "setupRunId": "separate-website-setup",
            "setupStatus": "blocked",
        }
        patches = {
            "_workflow_progress_context": (None, None, [run], checks),
            "_latest_publish_child_run": None,
            "_accepted_revision_source_run": None,
            "_publish_evidence_from_run": {},
            "_content_package_from_run": {"contentPackaged": True, "title": "Saved draft"},
            "_component_feedback_from_run": {"comments": [], "latestBatch": None},
            "_source_accepts_revision": False,
            "_run_can_promote_package": False,
            "_publish_handoff_stale_for_run": False,
            "_publish_child_run_recoverable": False,
            "_component_manifest_from_run": {"components": [{"id": "title"}]},
        }
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(views, name, return_value=value))
            stack.enter_context(patch.object(views, "_latest_run_matching", side_effect=lambda rows, workflows: run if run.workflow in workflows else None))
            result = views._workflow_progress(run=run if scoped else None, topic_candidates=[])
        return result, {step["id"]: step for step in result["steps"]}

    def test_completed_original_portable_run_keeps_its_own_review_without_unlocking_publication(self):
        result, steps = self.run_progress()
        self.assertEqual(result["currentStepId"], "review")
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "ready")
        self.assertEqual(steps["package"]["status"], "complete")
        for name in ("generate", "review", "package"):
            self.assertEqual(steps[name]["runId"], "synthetic-draft")
            self.assertIn("synthetic-draft", steps[name]["href"])
            self.assertNotIn("setup", steps[name]["summary"].lower())
        self.assertEqual(steps["publish"]["status"], "locked")
        self.assertIsNone(steps["publish"]["primaryAction"])
        self.assertEqual(steps["automation"]["status"], "locked")

    def test_failed_original_portable_run_keeps_its_own_failure(self):
        result, steps = self.run_progress(status="failed")
        self.assertEqual(result["currentStepId"], "generate")
        self.assertEqual(steps["generate"]["status"], "blocked")
        self.assertEqual(steps["generate"]["runId"], "synthetic-draft")
        self.assertIn("synthetic-draft", steps["generate"]["href"])

    def test_unconfirmed_bound_or_promoted_intent_cannot_hide_website_failure(self):
        for request, repo in (
            ({"delivery_mode": "content_only"}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": False}, ""),
            ({"delivery_mode": "publish_code", "delivery_mode_confirmed": True}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True}, "owner/site"),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True, "repository_id": 42}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True, "website_connection_id": "connection"}, ""),
        ):
            with self.subTest(request=request, repo=repo):
                _, steps = self.run_progress(request=request, bound_repo=repo)
                self.assertEqual(steps["generate"]["status"], "blocked")
                self.assertEqual(steps["generate"]["runId"], "separate-website-setup")
                self.assertEqual(steps["review"]["status"], "locked")
                self.assertEqual(steps["package"]["status"], "locked")

    def test_company_overview_keeps_real_setup_failure_despite_portable_history(self):
        _, steps = self.run_progress(scoped=False)
        self.assertEqual(steps["generate"]["status"], "blocked")
        self.assertEqual(steps["generate"]["runId"], "separate-website-setup")
        self.assertEqual(steps["review"]["status"], "locked")


class PortablePreviewTests(SimpleTestCase):
    def setUp(self):
        self.run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo="",
            workflow="article_generation", status="completed",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True},
            result={},
        )

    def artifacts(self, package=True, manifest=True):
        stack = ExitStack()
        stack.enter_context(patch.object(views, "_content_package_from_run", return_value={"contentPackaged": package}))
        stack.enter_context(patch.object(views, "_component_manifest_from_run", return_value={"components": [{"id": "title"}]} if manifest else None))
        return stack

    def test_private_portable_preview_never_mints_repository_authority(self):
        with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")):
            self.assertEqual(views._live_preview_github_token_payload(self.run), {})

    def test_incomplete_bound_unconfirmed_and_nonarticle_runs_keep_normal_authority_guard(self):
        for changes, intent in (
            ({"status": "running"}, {}),
            ({"github_repo": "owner/site"}, {}),
            ({"workflow": "article_system_setup"}, {}),
            ({}, {"delivery_mode_confirmed": False}),
            ({}, {"website_connection_id": "connection"}),
        ):
            with self.subTest(changes=changes, intent=intent):
                run = SimpleNamespace(**{**vars(self.run), **changes, "run_request": {**self.run.run_request, **intent}})
                with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=views.WebsiteAuthorityError("connection_required", "Reconnect.")) as authority:
                    with self.assertRaises(views.WebsiteAuthorityError):
                        views._live_preview_github_token_payload(run)
                    authority.assert_called_once_with(run)
        for package, manifest in ((False, True), (True, False)):
            with self.artifacts(package, manifest), patch.object(views, "scoped_run_contract", side_effect=views.WebsiteAuthorityError("connection_required", "Reconnect.")):
                with self.assertRaises(views.WebsiteAuthorityError):
                    views._live_preview_github_token_payload(self.run)

    def test_actual_preview_post_cannot_forward_local_checkout_or_credentials_for_portable_copy(self):
        request = SimpleNamespace(data={"force": True, "localRepoPath": "/private/checkout", "github_token": "untrusted"})
        view = views.VibeMarketingRunLivePreviewView()
        from . import website_views
        with self.artifacts(), patch.object(website_views, "_context", return_value=(object(), None, None)), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(view, "_resolve_run", return_value=(object(), self.run, None)), \
                patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_call_content_factory_live_preview", return_value={"available": True}) as dispatch, \
                patch.object(view, "_persist_preview", return_value=self.run), \
                patch.object(views, "_serialize_run", return_value={"runId": self.run.run_id}):
            runs.return_value.first.return_value = self.run
            result = view.post(request, self.run.run_id)
        self.assertEqual(result.status_code, 200)
        dispatch.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": True, "local_repo_path": ""})

    def test_automatic_private_preview_retains_the_exact_run_and_uses_no_repository_authority(self):
        with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_article_preview_should_refresh", return_value=False), \
                patch.object(views, "_article_preview_should_auto_prepare", return_value=True), \
                patch.object(views, "_call_content_factory_live_preview", return_value={"available": True}) as dispatch, \
                patch.object(views, "_persist_live_preview_payload", return_value=self.run):
            self.assertIs(views._ensure_article_live_preview(self.run), self.run)
        dispatch.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": False})


    def test_foreign_portable_preview_cannot_dispatch_or_resolve_saved_artifacts(self):
        from . import website_views
        request = SimpleNamespace(data={})
        view = views.VibeMarketingRunLivePreviewView()
        with patch.object(website_views, "_context", return_value=(object(), None, None)), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=False), \
                patch.object(view, "_resolve_run") as resolve, \
                patch.object(views, "_call_content_factory_live_preview") as dispatch:
            runs.return_value.first.return_value = self.run
            result = view.post(request, self.run.run_id)
        self.assertEqual(result.status_code, 404)
        resolve.assert_not_called()
        dispatch.assert_not_called()


@override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=True)
class PortablePreviewRearmTests(SimpleTestCase):
    """PawShare run 1d1aebc0 (29 Oct 2026) showed "Draft ready" with no preview.

    Content Factory refused its hosted preview with 409, the backend stored the
    refusal as a final, non-retryable failure, and nothing asked again. With
    the re-arm flag on, a completed portable draft asks Content Factory's
    preview ladder again; Content Factory owns the cycle and its backoff.
    """

    LEGACY_REFUSAL = {
        "available": False, "status": "failed", "retryable": False,
        "error": "This is a portable draft. Start a new website-bound run to preview it.",
        "content_factory_status_code": 409,
        "content_factory_response": {"detail": "This is a portable draft."},
    }

    def setUp(self):
        self.run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo="",
            workflow="article_generation", status="completed",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True},
            result={}, save=Mock(),
        )

    def artifacts(self):
        stack = ExitStack()
        stack.enter_context(patch.object(views, "_content_package_from_run", return_value={"contentPackaged": True}))
        stack.enter_context(patch.object(views, "_component_manifest_from_run", return_value={"components": [{"id": "title"}]}))
        return stack

    def with_preview(self, live_preview, **changes):
        result = {} if live_preview is None else {"livePreview": live_preview}
        return SimpleNamespace(**{**vars(self.run), "result": result, "save": Mock(), **changes})

    def exhausted(self, **changes):
        """Content Factory's payload once every rung of a cycle has failed."""
        return {
            "available": False, "status": "failed", "error": "We couldn't build a preview yet.",
            "retryable": True, "previewMode": "platform_deployment", "previewRung": "exact",
            "autoRetry": True, "nextRetryAt": "", "failureReason": "hosted_build_not_configured",
            "proof": {"previewLadder": {"revision": "rev-1", "cycle": 1, "autoRetryCount": 0, "attempts": []},
                      "previewLadderExhausted": True},
            **changes,
        }

    def test_auto_prepare_matrix_for_portable_and_repository_drafts(self):
        past = (timezone.now() - timedelta(minutes=1)).isoformat()
        future = (timezone.now() + timedelta(minutes=30)).isoformat()
        ready = {"available": True, "status": "running", "previewMode": "platform_deployment", "previewRung": "exact",
                 "previewUrl": "https://cf-synthetic.mlai-review-static.pages.dev/blog/draft/"}
        cases = (
            # label, stored livePreview, portable draft, repository draft (today's rule)
            ("missing", None, True, True),
            ("not_started", {"available": False, "status": "not_started"}, True, True),
            ("queued", {"available": False, "status": "queued", "previewRung": "exact"}, True, True),
            ("expired", {"available": False, "status": "expired", "error": "Preview expired.", "retryable": True,
                         "previewRung": "exact"}, True, False),
            ("failed_retry_due", self.exhausted(), True, False),
            ("failed_retry_passed", self.exhausted(nextRetryAt=past), True, False),
            ("failed_retry_later", self.exhausted(nextRetryAt=future), False, False),
            ("auto_retry_off", self.exhausted(autoRetry=False), False, False),
            ("transport_failure", {"available": False, "status": "failed", "error": "timed out", "retryable": True}, True, False),
            ("legacy_refusal", self.LEGACY_REFUSAL, True, False),
            ("legacy_refusal_rearmed", {**self.LEGACY_REFUSAL, "autoStartRearmedAt": past}, False, False),
            ("ladder_refusal", {**self.LEGACY_REFUSAL, "previewRung": ""}, False, False),
            ("other_refusal", {**self.LEGACY_REFUSAL, "content_factory_status_code": 400}, False, False),
            ("building", {"available": False, "status": "building", "previewRung": "exact"}, False, True),
            ("ready", ready, False, False),
        )
        for label, live_preview, portable, repository in cases:
            with self.subTest(label), self.artifacts():
                self.assertIs(views._article_preview_should_auto_prepare(self.with_preview(live_preview)), portable)
                self.assertIs(views._article_preview_should_auto_prepare(
                    self.with_preview(live_preview, github_repo="owner/site")), repository)
        with self.subTest("incomplete"), self.artifacts():
            self.assertFalse(views._article_preview_should_auto_prepare(self.with_preview(self.LEGACY_REFUSAL, status="running")))

    @override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=False)
    def test_flag_off_keeps_todays_auto_prepare_rule(self):
        for label, live_preview, expected in (
            ("legacy_refusal", self.LEGACY_REFUSAL, False),
            ("failed_retry_due", self.exhausted(), False),
            ("building", {"available": False, "status": "building"}, True),
            ("missing", None, True),
        ):
            with self.subTest(label), self.artifacts():
                self.assertIs(views._article_preview_should_auto_prepare(self.with_preview(live_preview)), expected)
                self.assertFalse(views._article_preview_should_refresh(self.with_preview(live_preview)))

    def test_ladder_attempt_in_progress_is_read_back_with_get(self):
        for status in ("preparing", "building", "dispatched", "starting", "running"):
            live_preview = {"available": False, "status": status, "previewRung": "template",
                            "error": "The site build could not start."}
            with self.subTest(status), self.artifacts():
                self.assertTrue(views._article_preview_should_refresh(self.with_preview(live_preview)))
                # A repository draft keeps today's rule: an error is final, building is a POST.
                self.assertFalse(views._article_preview_should_refresh(self.with_preview(live_preview, github_repo="owner/site")))
        for label, live_preview in (
            ("queued", {"available": False, "status": "queued"}),
            ("failed", self.exhausted()),
            ("ready", {"available": True, "status": "running", "previewUrl": "https://cf-synthetic.pages.dev/"}),
            ("missing", None),
        ):
            with self.subTest(label), self.artifacts():
                self.assertFalse(views._article_preview_should_refresh(self.with_preview(live_preview)))

    def ensure(self, run, *responses):
        """Drive the run-detail poll; each persist replaces livePreview as the real one does."""
        persisted = []

        def persist(current, payload):
            persisted.append(payload)
            current.result = {**current.result, "livePreview": payload}
            return current

        with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_call_content_factory_live_preview", side_effect=list(responses)) as dispatch, \
                patch.object(views, "_persist_live_preview_payload", side_effect=persist):
            for _ in responses:
                self.assertIs(views._ensure_article_live_preview(run), run)
        return dispatch, persisted

    def test_legacy_refusal_is_rearmed_once_with_force_false(self):
        run = self.with_preview(self.LEGACY_REFUSAL)
        with self.assertLogs(views.logger, level="INFO") as logs:
            dispatch, persisted = self.ensure(run, dict(self.LEGACY_REFUSAL), AssertionError("asked twice"))
        dispatch.assert_called_once_with(run_id=run.run_id, method="POST", payload={"force": False})
        self.assertEqual(len(persisted), 1)
        self.assertTrue(persisted[0]["autoStartRearmedAt"])
        self.assertTrue(any("content_factory_live_preview_rearm run_id=synthetic-draft" in line for line in logs.output))
        with self.artifacts():
            self.assertFalse(views._article_preview_should_auto_prepare(run))

    def test_rearm_hands_over_to_the_ladder_and_then_reads_it_back(self):
        building = {"available": False, "status": "building", "previewMode": "platform_deployment", "previewRung": "exact",
                    "proof": {"hostedBuild": "a" * 32, "previewLadder": {"cycle": 1, "attempts": []}}}
        run = self.with_preview(self.LEGACY_REFUSAL)
        dispatch, persisted = self.ensure(run, dict(building), {**building, "status": "running", "available": True,
                                                                "previewUrl": "https://cf-synthetic.pages.dev/blog/draft/"})
        self.assertEqual([call.kwargs["method"] for call in dispatch.call_args_list], ["POST", "GET"])
        self.assertEqual(dispatch.call_args_list[0].kwargs["payload"], {"force": False})
        self.assertNotIn("autoStartRearmedAt", persisted[0])
        self.assertTrue(persisted[1]["available"])

    def test_a_due_ladder_retry_posts_without_force(self):
        run = self.with_preview(self.exhausted(nextRetryAt=(timezone.now() - timedelta(seconds=5)).isoformat()))
        dispatch, _ = self.ensure(run, {"available": False, "status": "building", "previewRung": "exact"})
        dispatch.assert_called_once_with(run_id=run.run_id, method="POST", payload={"force": False})

    @override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=False)
    def test_flag_off_leaves_a_stored_refusal_final(self):
        run = self.with_preview(self.LEGACY_REFUSAL)
        with self.artifacts(), patch.object(views, "_call_content_factory_live_preview") as dispatch:
            self.assertIs(views._ensure_article_live_preview(run), run)
        dispatch.assert_not_called()

    def test_ladder_fields_pass_through_in_camel_and_snake_case(self):
        ladder = {"revision": "rev-1", "cycle": 1, "autoRetryCount": 0, "attempts": []}
        camel = {
            "available": True, "status": "running", "previewMode": "platform_deployment",
            "previewUrl": "https://cf-synthetic.mlai-review-static.pages.dev/blog/draft/",
            "previewRung": "snapshot", "previewFidelity": "live_article_layout",
            "templateSourceUrl": "https://publisher.example/blog/older-post/",
            "previewFallback": [{"rung": "exact", "reason": "hosted_build_not_configured", "at": "2026-10-30T00:00:00Z"}],
            "previewExpiresAt": "2026-11-06T16:00:00Z", "nextRetryAt": "", "autoRetry": False, "failureReason": "",
            "proof": {"previewLadder": ladder, "previewLadderExhausted": False},
        }
        aliases = {camel_key: snake_key for camel_key, snake_key, _empty in views.LIVE_PREVIEW_LADDER_FIELDS}
        aliases.update(previewMode="preview_mode", previewUrl="preview_url")
        snake = {aliases.get(key, key): value for key, value in camel.items()}
        expected = {key: camel[key] for key, _snake, _empty in views.LIVE_PREVIEW_LADDER_FIELDS}
        for label, payload in (("camel", camel), ("snake", snake)):
            with self.subTest(label):
                serialized = views._live_preview_from_run(SimpleNamespace(run_id="synthetic-draft", result={"livePreview": payload}))
                self.assertEqual({key: serialized[key] for key in expected}, expected)
                self.assertEqual(serialized["proof"]["previewLadder"], ladder)
                self.assertEqual(serialized["previewUrl"], camel["previewUrl"])
                self.assertEqual(views._normalize_live_preview_payload(payload)["previewRung"], "snapshot")
        serialized = views._live_preview_from_run(SimpleNamespace(run_id="synthetic-draft", result={"livePreview": {
            "available": False, "status": "building", "previewFallback": "not-a-list", "autoRetry": "yes"}}))
        self.assertEqual({key: serialized[key] for key in expected}, {
            "previewRung": "", "previewFidelity": "", "templateSourceUrl": "", "previewFallback": [],
            "previewExpiresAt": "", "nextRetryAt": "", "autoRetry": None, "failureReason": ""})
        with override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=False):
            serialized = views._live_preview_from_run(SimpleNamespace(run_id="synthetic-draft", result={"livePreview": camel}))
            self.assertFalse(set(serialized) & set(expected))
            self.assertNotIn("previewRung", views._normalize_live_preview_payload(snake))

    def test_unavailable_helpers_and_restart_yield_to_the_ladder(self):
        legacy = {"available": False, "status": "not_available", "retryable": False,
                  "previewUnavailableReason": "content_only_no_render_artifact"}
        cases = (
            ("legacy", legacy, "content_only_no_render_artifact", True),
            ("rung", {**legacy, "previewRung": ""}, "", False),
            ("snake_rung", {**legacy, "preview_rung": "exact"}, "", False),
            ("proof_ladder", {**legacy, "proof": {"previewLadder": {"cycle": 1}}}, "", False),
        )
        with patch.object(views, "_article_draft_title_keyword", return_value=("Dog beach playdates", "dog beach")):
            for label, live_preview, reason, unavailable in cases:
                with self.subTest(label):
                    run = self.with_preview(live_preview)
                    self.assertEqual(views._article_preview_unavailable_reason(run), reason)
                    self.assertIs(views._article_content_only_preview_not_available(run), unavailable)
                    # The Restart that reuses this draft's payment is offered only for the legacy reason.
                    self.assertIs(views._article_restart_available(run), unavailable)
            with override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=False):
                run = self.with_preview({**legacy, "previewRung": "exact"})
                self.assertEqual(views._article_preview_unavailable_reason(run), "content_only_no_render_artifact")
                self.assertTrue(views._article_content_only_preview_not_available(run))

    def persist(self, loaded, current, payload):
        with patch.object(views.transaction, "atomic", return_value=nullcontext()) as atomic, \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked:
            locked.return_value.get.return_value = current
            returned = views._persist_live_preview_payload(loaded, payload)
        return returned, atomic, locked

    def test_persist_writes_only_live_preview_onto_the_locked_current_row(self):
        loaded = self.with_preview(self.LEGACY_REFUSAL)
        loaded.result.update(status="completed", markdown="Copy loaded before the Content Factory call")
        # A Content Factory status mirror saved these keys while the preview call was in flight.
        current_keys = {"status": "completed", "markdown": "Edited copy saved by the status mirror",
                        "article_review_revision": "rev-2", "state_version": 9}
        current = SimpleNamespace(pk=1, result={**current_keys, "livePreview": self.LEGACY_REFUSAL},
                                  updated_at=timezone.now(), save=Mock())
        payload = {"available": False, "status": "building", "previewMode": "platform_deployment", "preview_rung": "exact"}
        returned, atomic, locked = self.persist(loaded, current, payload)
        atomic.assert_called_once_with()
        locked.return_value.get.assert_called_once_with(pk=1)
        current.save.assert_called_once_with(update_fields=["result", "updated_at"])
        loaded.save.assert_not_called()
        self.assertEqual({key: value for key, value in current.result.items() if key != "livePreview"}, current_keys)
        self.assertEqual((current.result["livePreview"]["status"], current.result["livePreview"]["previewRung"]), ("building", "exact"))
        self.assertIs(returned, loaded)
        self.assertEqual(loaded.result, current.result)

    def test_persist_keeps_todays_write_for_setup_runs_and_with_the_flag_off(self):
        payload = {"available": False, "status": "building", "previewMode": "platform_deployment"}
        for label, workflow, flag in (("flag_off", "article_generation", False), ("setup", "article_system_setup", True)):
            with self.subTest(label), override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=flag):
                loaded = self.with_preview(self.LEGACY_REFUSAL, workflow=workflow, current_step="", approval_state="", error="")
                current = SimpleNamespace(pk=1, result={}, save=Mock())
                _returned, _atomic, locked = self.persist(loaded, current, payload)
                locked.assert_not_called()
                current.save.assert_not_called()
                loaded.save.assert_called_once()
                self.assertEqual(loaded.result["livePreview"]["status"], "building")

    def test_refund_sweep_ignores_a_failed_preview_on_a_completed_paid_draft(self):
        """A preview failure is never a run failure: run status and result.failure stay untouched."""
        from . import reconciliation
        from .article_publication_evidence import (
            denied_refund_code, dispatch_rejected_refund_candidate, exhausted_article_candidate, unrecoverable_refund_code)
        for label, live_preview in (
            ("exhausted", self.exhausted(autoRetry=False)),
            ("legacy_refusal", self.LEGACY_REFUSAL),
            ("rearmed_refusal", {**self.LEGACY_REFUSAL, "autoStartRearmedAt": timezone.now().isoformat()}),
        ):
            with self.subTest(label):
                run = SimpleNamespace(
                    pk=41, run_id="1d1aebc0-pawshare", organization_id=124, domain="pawshare.test", github_repo="",
                    workflow="article_generation", status="completed", current_step="finalize", approval_state="not_required",
                    resume_available=False, acceptance_summary={}, verification_summary={}, error="", save=Mock(),
                    updated_at=timezone.now() - timedelta(hours=1), reconciled_at=None,
                    run_request={"client_request_id": "vibe-article-job:1d1aebc0", "domain": "pawshare.test", "github_repo": "",
                                 "delivery_mode": "content_only", "delivery_mode_confirmed": True, "roo_points_cost": 12,
                                 "roo_points_ledger_id": "5479", "roo_points_billing_status": "charged"},
                    result={"status": "completed", "content_package": {"content_packaged": True}, "livePreview": live_preview})
                self.assertIsNone(unrecoverable_refund_code(run))
                self.assertFalse(dispatch_rejected_refund_candidate(run))
                self.assertIsNone(denied_refund_code(run))
                self.assertFalse(exhausted_article_candidate(run))
                rows = Mock()
                rows.filter.return_value = rows
                rows.order_by.return_value = rows
                rows.__getitem__ = Mock(return_value=[run])
                with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows), \
                        patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                        patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                        patch.object(reconciliation, "_refund_failed_run") as refund:
                    locked.return_value.filter.return_value.first.return_value = run
                    self.assertEqual(reconciliation._queue_unrecoverable_article_refunds(limit=10, now=timezone.now()), 0)
                refund.assert_not_called()
                run.save.assert_not_called()
                self.assertNotIn("article_terminal_refund", run.result)
