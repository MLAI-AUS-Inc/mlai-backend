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

    def artifacts(self, *, published=False):
        stack = ExitStack()
        stack.enter_context(patch.object(views, "_content_package_from_run", return_value={"contentPackaged": True}))
        stack.enter_context(patch.object(views, "_component_manifest_from_run", return_value={"components": [{"id": "title"}]}))
        stack.enter_context(patch.object(views, "_portable_draft_published_hosted", return_value=published))
        return stack

    def with_preview(self, live_preview, *, result=None, **changes):
        stored = {} if live_preview is None else {"livePreview": live_preview}
        return SimpleNamespace(**{**vars(self.run), "result": {**stored, **(result or {})}, "save": Mock(),
                                  "updated_at": timezone.now(), **changes})

    LEGACY_NOT_AVAILABLE = {
        "available": False, "status": "not_available", "retryable": False,
        "previewUnavailableReason": "content_only_no_render_artifact",
        "proof": {"previewUnavailableReason": "content_only_no_render_artifact"},
    }

    def ready(self, **changes):
        """A ready Cloudflare preview of the ladder (rung 1) and its expiry."""
        return {
            "available": True, "status": "running", "previewMode": "platform_deployment", "previewRung": "exact",
            "previewUrl": "https://cf-synthetic.mlai-review-static.pages.dev/blog/draft/",
            "previewExpiresAt": (timezone.now() + timedelta(hours=100)).isoformat(),
            "proof": {"hostedBuild": "a" * 32, "articleReviewRevision": "rev-1",
                      "previewLadder": {"revision": "rev-1", "cycle": 1, "autoRetryCount": 0, "attempts": []}},
            **changes,
        }

    def shell(self, **changes):
        """The saved-HTML shell rung: a local session that lapses after its own expiresAt."""
        return {
            "available": True, "status": "running", "previewMode": "content_only", "previewRung": "shell",
            "previewUrl": "/api/runs/synthetic-draft/live-preview/proxy/blog/draft?cfInspector=1",
            "expiresAt": (timezone.now() + timedelta(minutes=30)).isoformat(), "previewExpiresAt": "",
            "proof": {"articleReviewRevision": "rev-1",
                      "previewLadder": {"revision": "rev-1", "cycle": 1, "autoRetryCount": 0, "attempts": []}},
            **changes,
        }

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

    def attempt_proof(self, **attempt):
        """The ready build's proof with its ladder attempt (same nonce) carrying ``attempt``."""
        proof = self.ready()["proof"]
        ladder = {**proof["previewLadder"], "attempts": [
            {"rung": "exact", "nonce": "f" * 32, "outcome": "failed"},
            {"rung": "exact", "nonce": proof["hostedBuild"], "outcome": "ready", **attempt},
        ]}
        return {**proof, "previewLadder": ladder}

    def last_resort_shell(self, **changes):
        """The shell Content Factory serves once every Cloudflare rung failed through all 3 automatic retries."""
        failed = [{"rung": rung, "nonce": "", "outcome": "failed"} for rung in ("exact", "snapshot", "template")]
        return self.shell(proof={"articleReviewRevision": "rev-1", "previewRung": "shell", "previewLadder": {
            "revision": "rev-1", "cycle": 4, "autoRetryCount": 3,
            "attempts": [*failed, {"rung": "shell", "nonce": "", "outcome": "ready"}]}}, **changes)

    def test_auto_prepare_matrix_for_portable_and_repository_drafts(self):
        past = (timezone.now() - timedelta(minutes=1)).isoformat()
        future = (timezone.now() + timedelta(minutes=30)).isoformat()
        long_ago = (timezone.now() - timedelta(hours=200)).isoformat()
        recent = (timezone.now() - timedelta(hours=2)).isoformat()
        ready = {"available": True, "status": "running", "previewMode": "platform_deployment", "previewRung": "exact",
                 "previewUrl": "https://cf-synthetic.mlai-review-static.pages.dev/blog/draft/"}
        ladder_failed = {
            "available": False, "status": "failed", "retryable": False, "previewMode": "platform_deployment",
            "previewRung": "exact", "autoRetry": True, "nextRetryAt": "",
            "proof": {"hostedBuild": "b" * 32, "previewLadder": {"cycle": 1, "attempts": []}, "previewLadderExhausted": False},
        }
        # Content Factory lists the ladder's fields, empty, on payloads no ladder produced.
        empty_ladder_keys = {"previewRung": "", "previewFallback": [], "autoRetry": None, "nextRetryAt": ""}
        transport = {"available": False, "status": "failed", "error": "timed out", "retryable": True}
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
            ("legacy_refusal_with_empty_ladder_keys", {**self.LEGACY_REFUSAL, **empty_ladder_keys}, True, False),
            ("other_refusal", {**self.LEGACY_REFUSAL, "content_factory_status_code": 400}, False, False),
            # Older content-only drafts stored Content Factory's 2xx not_available (no render artifact).
            ("legacy_not_available", self.LEGACY_NOT_AVAILABLE, True, True),
            ("legacy_not_available_rearmed", {**self.LEGACY_NOT_AVAILABLE, "autoStartRearmedAt": past}, False, True),
            ("legacy_artifacts_unavailable", {"available": False, "status": "failed", "retryable": False,
                                              "error": "No portable artifacts.",
                                              "errorCode": "portable_preview_artifacts_unavailable"}, True, False),
            # A failed rung that is not the end of the cycle: only Content Factory's start moves it on.
            ("ladder_failed_not_exhausted", ladder_failed, True, False),
            ("building", {"available": False, "status": "building", "previewRung": "exact"}, False, True),
            ("ready", ready, False, False),
            ("ready_before_expiry", self.ready(), False, False),
            ("ready_past_expiry", self.ready(previewExpiresAt=past), True, False),
            ("ready_past_expiry_in_proof", self.ready(previewExpiresAt="", proof={
                **self.ready()["proof"], "previewExpiresAt": past}), True, False),
            ("shell_before_expiry", self.shell(), False, False),
            ("shell_past_expiry", self.shell(expiresAt=past), True, False),
            # A hosted build's expiresAt is its build deadline, never the preview's expiry.
            ("hosted_build_deadline_passed", self.ready(previewExpiresAt="", expiresAt=past), False, False),
            # A hosted build stored with no previewExpiresAt (built before the ladder, or read before
            # Content Factory recorded it ready) expires as preview_ladder.previewed_revision counts it.
            ("hosted_no_expiry_started_200h_ago", self.ready(previewExpiresAt="", startedAt=long_ago), True, False),
            ("hosted_no_expiry_dispatched_200h_ago", self.ready(previewExpiresAt="", proof={
                **self.ready()["proof"], "builderDispatchedAt": long_ago}), True, False),
            ("hosted_no_expiry_started_2h_ago", self.ready(previewExpiresAt="", startedAt=recent), False, False),
            ("hosted_no_expiry_ready_170h_ago", self.ready(previewExpiresAt="", startedAt=recent, proof=self.attempt_proof(
                readyAt=(timezone.now() - timedelta(hours=170)).isoformat())), True, False),
            ("hosted_no_expiry_attempt_expiry_later", self.ready(previewExpiresAt="", startedAt=long_ago, proof=self.attempt_proof(
                previewExpiresAt=future)), False, False),
            # CF restarts every rung and its retries for an expired last-resort shell: the owner's Retry asks.
            ("last_resort_shell_past_expiry", self.last_resort_shell(expiresAt=past), False, False),
            ("last_resort_shell_before_expiry", self.last_resort_shell(), False, False),
            # The backend's own floor on automatic starts (autoStartAt), backing off while nothing answers.
            ("transport_failure_just_asked", {**transport, "autoStartCount": 1, "autoStartAt": (
                timezone.now() - timedelta(seconds=10)).isoformat()}, False, False),
            ("transport_failure_asked_2m_ago", {**transport, "autoStartCount": 1, "autoStartAt": (
                timezone.now() - timedelta(minutes=2)).isoformat()}, True, False),
            ("transport_failure_asked_twice_2m_ago", {**transport, "autoStartCount": 2, "autoStartAt": (
                timezone.now() - timedelta(minutes=2)).isoformat()}, False, False),
            ("transport_failure_asked_5x_14m_ago", {**transport, "autoStartCount": 5, "autoStartAt": (
                timezone.now() - timedelta(minutes=14)).isoformat()}, False, False),
            ("transport_failure_asked_5x_16m_ago", {**transport, "autoStartCount": 5, "autoStartAt": (
                timezone.now() - timedelta(minutes=16)).isoformat()}, True, False),
            ("queued_just_asked", {"available": False, "status": "queued", "previewRung": "snapshot",
                                   "autoStartAt": (timezone.now() - timedelta(seconds=30)).isoformat(),
                                   "autoStartCount": 5}, False, True),
            ("queued_asked_a_minute_ago", {"available": False, "status": "queued", "previewRung": "snapshot",
                                           "autoStartAt": (timezone.now() - timedelta(seconds=61)).isoformat(),
                                           "autoStartCount": 5}, True, True),
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
        past = (timezone.now() - timedelta(minutes=1)).isoformat()
        for label, live_preview, expected in (
            ("legacy_refusal", self.LEGACY_REFUSAL, False),
            ("legacy_not_available", self.LEGACY_NOT_AVAILABLE, True),
            ("failed_retry_due", self.exhausted(), False),
            ("ladder_failed_not_exhausted", self.exhausted(retryable=False, proof={"previewLadderExhausted": False}), False),
            ("ready_past_expiry", self.ready(previewExpiresAt=past), False),
            ("shell_past_expiry", self.shell(expiresAt=past), False),
            ("queued_just_asked", {"available": False, "status": "queued", "autoStartAt": past}, True),
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

    def poll(self, run, answer, *, minutes, every_seconds=2.5):
        """Read run detail at chat's fastest poll for ``minutes`` of simulated time.

        ``answer`` is Content Factory's reply, or a callable of the method. The
        stand-in persist skips an empty payload, as the real one does. Returns
        each Content Factory call as (method, seconds since the first read).
        """
        start = timezone.now()
        clock = [start]
        calls = []

        def call(run_id, method="GET", payload=None):
            self.assertEqual(run_id, run.run_id)
            if method == "POST":
                self.assertEqual(payload, {"force": False})
            calls.append((method, round((clock[0] - start).total_seconds())))
            reply = answer(method) if callable(answer) else answer
            return dict(reply) if isinstance(reply, dict) else reply

        def persist(current, payload):
            if isinstance(payload, dict) and payload:
                current.result = {**current.result, "livePreview": views._normalize_live_preview_payload(payload)}
            return current

        with self.artifacts(), patch.object(views.timezone, "now", side_effect=lambda: clock[0]), \
                patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_call_content_factory_live_preview", side_effect=call), \
                patch.object(views, "_persist_live_preview_payload", side_effect=persist):
            while clock[0] - start < timedelta(minutes=minutes):
                self.assertIs(views._ensure_article_live_preview(run), run)
                clock[0] += timedelta(seconds=every_seconds)
        return calls

    def test_backend_made_failures_back_off_instead_of_asking_on_every_poll(self):
        """A Content Factory outage once drove one POST per 2.5 s poll per open draft, indefinitely."""
        blocked = {**views._blocked_worker_payload(workflow="article_generation", technical_error="disabled", retryable=True),
                   "available": False}
        for label, answer in (
            ("cf_503", {"available": False, "status": "failed", "error": "Service Unavailable",
                        "content_factory_status_code": 503, "content_factory_response": {}, "retryable": True}),
            ("transport", {"available": False, "status": "failed", "error": "Connection refused", "retryable": True}),
            ("cf_disabled", blocked),
        ):
            with self.subTest(label):
                run = self.with_preview(None)
                calls = self.poll(run, answer, minutes=30)
                # One start, then one after 1, 5 and 15 more minutes.
                self.assertEqual(calls, [("POST", 0), ("POST", 60), ("POST", 360), ("POST", 1260)])
                self.assertEqual(run.result["livePreview"]["autoStartCount"], 4)

    def test_a_queued_ladder_is_asked_at_most_once_a_minute(self):
        queued = {"available": False, "status": "queued", "previewUrl": "", "retryable": True, "error": "",
                  "previewRung": "snapshot", "previewFidelity": "live_article_layout", "nextRetryAt": "", "autoRetry": True,
                  "proof": {"previewLadder": {"revision": "rev-1", "cycle": 1, "autoRetryCount": 0, "attempts": []},
                            "previewLadderExhausted": False}}
        calls = self.poll(self.with_preview(None), queued, minutes=10)
        self.assertEqual(calls, [("POST", 60 * minute) for minute in range(10)])

    def test_a_ladder_answer_restarts_the_backoff(self):
        streak = {"available": False, "status": "failed", "retryable": True, "autoStartCount": 3,
                  "autoStartAt": timezone.now().isoformat()}
        building = {"available": False, "status": "building", "previewRung": "exact",
                    "proof": {"previewLadder": {"cycle": 1, "attempts": []}}}
        self.assertEqual(views._stamp_portable_auto_start(streak, building, legacy_rearm=False)["autoStartCount"], 1)
        self.assertEqual(views._stamp_portable_auto_start(streak, {"available": False, "status": "failed"},
                                                          legacy_rearm=False)["autoStartCount"], 4)

    def test_an_empty_answer_still_bounds_the_next_start(self):
        """Content Factory answers 200 {} for an approved draft it never previewed."""
        run = self.with_preview(self.LEGACY_REFUSAL)
        self.assertEqual(self.poll(run, {}, minutes=30), [("POST", 0)])
        stored = run.result["livePreview"]
        self.assertTrue(stored["autoStartRearmedAt"])
        self.assertEqual({key: stored[key] for key in self.LEGACY_REFUSAL}, self.LEGACY_REFUSAL)

        run = self.with_preview(None)
        self.assertEqual(self.poll(run, {}, minutes=30), [("POST", 0), ("POST", 60), ("POST", 360), ("POST", 1260)])
        self.assertEqual(views._live_preview_from_run(run)["status"], "not_started")

    def test_a_legacy_not_available_draft_is_rearmed_once(self):
        """Older content-only drafts stored a 2xx, non-retryable not_available that the ladder now previews."""
        run = self.with_preview(self.LEGACY_NOT_AVAILABLE)
        # A Content Factory without the ladder answers the same: asked once, then stamped.
        self.assertEqual(self.poll(run, self.LEGACY_NOT_AVAILABLE, minutes=30), [("POST", 0)])
        self.assertTrue(run.result["livePreview"]["autoStartRearmedAt"])
        # The ladder answers with its first rung, which is then read back.
        building = {"available": False, "status": "building", "previewMode": "platform_deployment", "previewRung": "exact",
                    "proof": {"hostedBuild": "a" * 32, "previewLadder": {"cycle": 1, "attempts": []}}}
        run = self.with_preview(self.LEGACY_NOT_AVAILABLE)
        calls = self.poll(run, lambda method: building, minutes=1, every_seconds=20)
        self.assertEqual([method for method, _seconds in calls], ["POST", "GET", "GET"])
        self.assertNotIn("autoStartRearmedAt", run.result["livePreview"])

    def test_an_expired_ready_preview_starts_a_new_cycle(self):
        """A ready preview is stored as ready; past its expiry only a start lets Content Factory rebuild it."""
        past = (timezone.now() - timedelta(hours=10)).isoformat()
        building = {"available": False, "status": "building", "previewMode": "platform_deployment", "previewRung": "exact",
                    "proof": {"hostedBuild": "c" * 32, "previewLadder": {"cycle": 2, "attempts": []}}}
        for label, stored in (("cloudflare", self.ready(previewExpiresAt=past)), ("shell", self.shell(expiresAt=past))):
            with self.subTest(label):
                run = self.with_preview(stored)
                calls = self.poll(run, lambda method: building, minutes=1, every_seconds=20)
                self.assertEqual([method for method, _seconds in calls], ["POST", "GET", "GET"])
        with self.subTest("before_expiry"):
            self.assertEqual(self.poll(self.with_preview(self.ready()), AssertionError, minutes=5), [])

    def test_a_failed_rung_that_does_not_end_the_cycle_is_moved_on(self):
        failed = {"available": False, "status": "failed", "retryable": False, "previewRung": "snapshot",
                  "errorCode": "platform_preview_failed", "autoRetry": True, "nextRetryAt": "",
                  "proof": {"hostedBuild": "b" * 32, "previewLadder": {"cycle": 1, "attempts": []},
                            "previewLadderExhausted": False}}
        building = {"available": False, "status": "building", "previewRung": "template",
                    "proof": {"hostedBuild": "d" * 32, "previewLadder": {"cycle": 1, "attempts": []}}}
        calls = self.poll(self.with_preview(failed), lambda method: building, minutes=1, every_seconds=20)
        self.assertEqual([method for method, _seconds in calls], ["POST", "GET", "GET"])

    BUILDING = {"available": False, "status": "building", "previewMode": "platform_deployment", "previewRung": "exact",
                "proof": {"hostedBuild": "c" * 32, "articleReviewRevision": "rev-2",
                          "previewLadder": {"revision": "rev-2", "cycle": 2, "autoRetryCount": 0, "attempts": []}}}

    def test_a_hosted_build_without_a_recorded_expiry_is_rebuilt_after_160_hours(self):
        """Pre-ladder hosted previews, and builds read before the ladder recorded them ready, have no previewExpiresAt."""
        stale = self.ready(previewExpiresAt="", startedAt=(timezone.now() - timedelta(hours=200)).isoformat())
        calls = self.poll(self.with_preview(stale), lambda method: self.BUILDING, minutes=1, every_seconds=20)
        self.assertEqual([method for method, _seconds in calls], ["POST", "GET", "GET"])
        # A Content Factory that cannot see the expiry either answers the same preview: that is no
        # progress, so the floor backs off instead of asking once a minute on every read.
        self.assertEqual(self.poll(self.with_preview(stale), stale, minutes=30),
                         [("POST", 0), ("POST", 60), ("POST", 360), ("POST", 1260)])

    def test_a_ready_preview_of_an_older_revision_is_rearmed(self):
        """The draft's review revision moved on (Content Factory's run result, mirrored here) under a ready preview."""
        edited = {"article_review_revision": "rev-2"}
        calls = self.poll(self.with_preview(self.ready(), result=edited), lambda method: self.BUILDING,
                          minutes=1, every_seconds=20)
        self.assertEqual([method for method, _seconds in calls], ["POST", "GET", "GET"])
        # Content Factory may know a newer revision than this backend: when it answers with the
        # same preview, that revision is not asked about again.
        run = self.with_preview(self.ready(), result=edited)
        self.assertEqual(self.poll(run, self.ready(), minutes=30), [("POST", 0)])
        self.assertEqual(run.result["livePreview"]["autoStartRevision"], "rev-2")
        with self.subTest("an exhausted ladder of the earlier revision"):
            exhausted = self.exhausted(autoRetry=False, proof={**self.exhausted()["proof"], "articleReviewRevision": "rev-1"})
            self.assertEqual(self.poll(self.with_preview(exhausted, result=edited), exhausted, minutes=30), [("POST", 0)])
        for label, result in (("same_revision", {"article_review_revision": "rev-1"}), ("unknown_revision", {})):
            with self.subTest(label):
                self.assertEqual(self.poll(self.with_preview(self.ready(), result=result), AssertionError, minutes=5), [])

    def test_an_expired_last_resort_shell_is_left_to_the_owners_retry(self):
        """Content Factory would restart every Cloudflare rung and reset its automatic retries."""
        expired = self.last_resort_shell(expiresAt=(timezone.now() - timedelta(minutes=10)).isoformat())
        self.assertEqual(self.poll(self.with_preview(expired), AssertionError, minutes=30), [])
        # A shell that is an ordinary rung (snapshot rungs off) is rebuilt on expiry as before.
        ordinary = self.shell(expiresAt=(timezone.now() - timedelta(minutes=10)).isoformat())
        calls = self.poll(self.with_preview(ordinary), lambda method: self.BUILDING, minutes=1, every_seconds=20)
        self.assertEqual(calls[0], ("POST", 0))

    def test_a_published_hosted_article_is_not_rebuilt(self):
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        for label, stored, result in (
            ("expired", self.ready(previewExpiresAt=past), {}),
            ("missing", None, {}),
            ("legacy_refusal", self.LEGACY_REFUSAL, {}),
            ("failed_rung", {**self.exhausted(), "proof": {**self.exhausted()["proof"], "previewLadderExhausted": False}}, {}),
            ("older_revision", self.ready(), {"article_review_revision": "rev-2"}),
        ):
            with self.subTest(label), self.artifacts(published=True):
                self.assertFalse(views._article_preview_should_auto_prepare(self.with_preview(stored, result=result)))
            with self.subTest(label + "_unpublished"), self.artifacts():
                self.assertTrue(views._article_preview_should_auto_prepare(self.with_preview(stored, result=result)))
        with self.artifacts(published=True):
            # A build already running is still read back until it settles.
            self.assertTrue(views._article_preview_should_refresh(self.with_preview(self.BUILDING)))

    def test_published_means_a_queued_rendering_deployed_or_live_publication(self):
        from .hosted_article_models import HostedArticlePublication
        with patch.object(HostedArticlePublication.objects, "filter") as rows:
            rows.return_value.exists.return_value = True
            self.assertTrue(views._portable_draft_published_hosted(self.run))
        rows.assert_called_once_with(run_id="synthetic-draft", status__in=("pending", "rendering", "deployed", "live"))

    def poll_through_persist(self, run, *, post, get, minutes, every_seconds=2.5):
        """Read run detail through the real Content Factory call and the real locked persist.

        ``post`` and ``get`` are what the HTTP call does: an exception to raise,
        or a (status code, JSON) answer. Returns each HTTP attempt as (method,
        seconds since the first read).
        """
        start = timezone.now()
        clock = [start]
        attempts = []

        def http(method, reply):
            attempts.append((method, round((clock[0] - start).total_seconds())))
            if isinstance(reply, BaseException):
                raise reply
            status_code, body = reply
            return SimpleNamespace(status_code=status_code, content=b"{}", json=lambda: dict(body), text="")

        with self.artifacts(), patch.object(views.timezone, "now", side_effect=lambda: clock[0]), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "http://cf.invalid"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "require_unlocked_remote_call", return_value=None), \
                patch.object(views.http_client, "post", side_effect=lambda url, **kwargs: http("POST", post)), \
                patch.object(views.http_client, "get", side_effect=lambda url, **kwargs: http("GET", get)), \
                patch.object(views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked:
            locked.return_value.get.return_value = run
            while clock[0] - start < timedelta(minutes=minutes):
                self.assertIs(views._ensure_article_live_preview(run), run)
                clock[0] += timedelta(seconds=every_seconds)
        return attempts

    def test_a_timed_out_content_factory_backs_off_through_the_read_back(self):
        """A timed-out start reads as "starting", and its GET read-back once dropped the floor's stamps."""
        from integrations import http_client
        for label, error in (("connect_timeout", http_client.exceptions.ConnectTimeout("connect timed out")),
                             ("read_timeout", http_client.exceptions.ReadTimeout("read timed out"))):
            with self.subTest(label):
                run = self.with_preview(None)
                attempts = self.poll_through_persist(run, post=error, get=error, minutes=30)
                # One timed-out start and one read-back, then again after 1, 5 and 15 more minutes.
                self.assertEqual(attempts, [("POST", 0), ("GET", 2), ("POST", 60), ("GET", 62),
                                            ("POST", 360), ("GET", 362), ("POST", 1260), ("GET", 1262)])
                self.assertEqual(run.result["livePreview"]["autoStartCount"], 4)

    def test_a_failed_read_back_keeps_the_ladder_building_for_chat(self):
        """One failed Content Factory call once turned a ladder build into an unpreviewed "Draft ready"."""
        from integrations import http_client
        run = self.with_preview(dict(self.BUILDING))
        attempts = self.poll_through_persist(run, post=http_client.exceptions.ConnectionError("Connection refused"),
                                             get=http_client.exceptions.ConnectionError("Connection refused"), minutes=10)
        self.assertEqual(attempts, [("GET", 0), ("POST", 2), ("POST", 62), ("POST", 362)])
        serialized = views._live_preview_from_run(run)
        self.assertEqual((serialized["status"], serialized["error"]), ("failed", "Connection refused"))
        self.assertEqual(serialized["proof"]["previewLadder"], self.BUILDING["proof"]["previewLadder"])
        self.assertEqual(serialized["proof"]["previewRung"], "exact")
        self.assertEqual(views._article_preview_unavailable_reason(run), "")

    def test_backend_stand_ins_keep_the_ladder_marker_and_real_answers_replace_it(self):
        ladder = self.BUILDING["proof"]["previewLadder"]
        blocked = {**views._blocked_worker_payload(workflow="article_generation", technical_error="disabled", retryable=True),
                   "available": False}
        stand_ins = (
            ("transport", {"available": False, "status": "failed", "error": "timed out", "errors": ["timed out"], "retryable": True}),
            ("start_timeout", {"available": False, "status": "starting", "previewUrl": "", "error": "", "errors": [],
                               "errorCode": "preview_start_timeout", "error_code": "preview_start_timeout", "retryable": True}),
            ("cf_503", {"available": False, "status": "failed", "error": "Bad gateway", "errors": ["Bad gateway"],
                        "content_factory_status_code": 503, "content_factory_response": {}, "retryable": True}),
            ("blocked", blocked),
        )
        for label, raw in stand_ins:
            with self.subTest(label):
                current = SimpleNamespace(pk=1, result={"livePreview": dict(self.BUILDING)}, updated_at=timezone.now(), save=Mock())
                self.persist(self.with_preview(dict(self.BUILDING)), current, dict(raw))
                stored = current.result["livePreview"]
                self.assertEqual(stored["proof"]["previewLadder"], ladder)
                self.assertEqual(stored["proof"]["previewRung"], "exact")
                self.assertEqual({key: stored[key] for key in raw if key != "proof"},
                                 {key: value for key, value in raw.items() if key != "proof"})
        answers = (
            ("cf_409", {"available": False, "status": "failed", "error": "Refused", "content_factory_status_code": 409,
                        "content_factory_response": {}, "retryable": False}),
            ("cf_400", {"available": False, "status": "failed", "error": "Bad request", "content_factory_status_code": 400,
                        "content_factory_response": {}, "retryable": False}),
            ("cf_2xx", {**self.LEGACY_NOT_AVAILABLE, "previewMode": "content_only"}),
        )
        for label, raw in answers:
            with self.subTest(label):
                current = SimpleNamespace(pk=1, result={"livePreview": dict(self.BUILDING)}, updated_at=timezone.now(), save=Mock())
                self.persist(self.with_preview(dict(self.BUILDING)), current, dict(raw))
                self.assertNotIn("previewLadder", current.result["livePreview"].get("proof") or {})
        with self.subTest("no ladder stored"):
            current = SimpleNamespace(pk=1, result={"livePreview": self.LEGACY_REFUSAL}, updated_at=timezone.now(), save=Mock())
            self.persist(self.with_preview(self.LEGACY_REFUSAL), current, dict(stand_ins[0][1]))
            self.assertNotIn("proof", current.result["livePreview"])

    def test_answers_without_ladder_state_keep_the_floor_stamps(self):
        stamps = {"autoStartAt": timezone.now().isoformat(), "autoStartCount": 3,
                  "autoStartRearmedAt": timezone.now().isoformat(), "autoStartRevision": "rev-2"}
        for label, raw, kept in (
            ("transport", {"available": False, "status": "failed", "error": "timed out", "retryable": True}, True),
            ("legacy_answer", dict(self.LEGACY_NOT_AVAILABLE), True),
            ("ladder_answer", dict(self.BUILDING), False),
            ("stamped_start", {"available": False, "status": "failed", "error": "x", "autoStartAt": "2026-10-30T00:00:00+00:00",
                               "autoStartCount": 1}, False),
        ):
            with self.subTest(label):
                current = SimpleNamespace(pk=1, result={"livePreview": {**self.LEGACY_REFUSAL, **stamps}},
                                          updated_at=timezone.now(), save=Mock())
                self.persist(self.with_preview(self.LEGACY_REFUSAL), current, raw)
                stored = current.result["livePreview"]
                if kept:
                    self.assertEqual({key: stored.get(key) for key in stamps}, stamps)
                else:
                    self.assertNotEqual({key: stored.get(key) for key in stamps}, stamps)

    def test_a_publish_without_a_rendered_preview_asks_the_ladder_for_it(self):
        """Allowing AI on a field changes the revision with no rebuild this backend can see."""
        stored = []

        def persist(current, payload):
            stored.append(payload)
            return current

        for label, run, changes, asked in (
            ("ready", self.with_preview(self.ready(), result={"article_review_revision": "rev-1"}), {}, True),
            ("missing", self.with_preview(None), {}, True),
            ("just_asked", self.with_preview({**self.ready(), "autoStartAt": timezone.now().isoformat(), "autoStartCount": 1}), {}, False),
            ("repository_draft", self.with_preview(self.ready()), {"github_repo": "owner/site"}, False),
        ):
            with self.subTest(label), self.artifacts(), \
                    patch.object(views, "_call_content_factory_live_preview", return_value=dict(self.BUILDING)) as dispatch, \
                    patch.object(views, "_persist_live_preview_payload", side_effect=persist):
                for key, value in changes.items():
                    setattr(run, key, value)
                stored.clear()
                views._start_portable_preview_of_unpreviewed_revision(run)
                if asked:
                    dispatch.assert_called_once_with(run_id=run.run_id, method="POST", payload={"force": False})
                    self.assertTrue(stored[0]["autoStartAt"])
                    self.assertEqual(stored[0]["previewRung"], "exact")
                    self.assertEqual(stored[0].get("autoStartRevision"), run.result.get("article_review_revision"))
                else:
                    dispatch.assert_not_called()
        with override_settings(CONTENT_FACTORY_PORTABLE_PREVIEW_REARM=False), self.artifacts(), \
                patch.object(views, "_call_content_factory_live_preview") as dispatch:
            views._start_portable_preview_of_unpreviewed_revision(self.with_preview(None))
        dispatch.assert_not_called()

    def test_every_automatic_start_logs_the_rearm_line(self):
        """Spec section 5 step 3 greps for this line; 1d1aebc0 may have no stored preview at all."""
        run = self.with_preview(None)
        with self.assertLogs(views.logger, level="INFO") as logs:
            self.ensure(run, {"available": False, "status": "building", "previewRung": "exact"})
        self.assertTrue(any("content_factory_live_preview_rearm run_id=synthetic-draft" in line
                            and "previous_status=missing" in line for line in logs.output), logs.output)

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
            # Content Factory lists the ladder's fields, empty, on every payload: that is no ladder.
            ("empty_ladder_keys", {**legacy, "previewRung": "", "preview_rung": "", "previewFallback": [],
                                   "autoRetry": None, "proof": {"previewUnavailableReason": "content_only_no_render_artifact"}},
             "content_only_no_render_artifact", True),
            ("rung", {**legacy, "previewRung": "exact"}, "", False),
            ("snake_rung", {**legacy, "preview_rung": "exact"}, "", False),
            ("proof_ladder", {**legacy, "proof": {"previewLadder": {"cycle": 1}}}, "", False),
            ("proof_exhausted", {**legacy, "proof": {"previewLadderExhausted": True}}, "", False),
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


class HostedPublishRenderedPreviewTests(SimpleTestCase):
    """Hosted publish can require that the owner saw a rendered preview of this exact revision.

    Content Factory's article-review read reports ``previewedRevision``: the
    current revision when its stored preview is ready for it, "" otherwise.
    """

    def setUp(self):
        self.run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo="",
            workflow="direct_generate", status="completed",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True}, result={},
        )

    def publish(self, review, reviewed="rev-1", start_error=None):
        from . import hosted_article_views
        view = hosted_article_views.HostedArticlePublishView()
        request = SimpleNamespace(data={"reviewedRevision": reviewed, "waivedComments": []})
        publication = SimpleNamespace(pk=7)
        context = SimpleNamespace(profile=SimpleNamespace(user=None))
        with patch.object(view, "_resolve", return_value=(context, self.run, SimpleNamespace(domain="publisher.example"), None)), \
                patch.object(views, "_latest_review_ready_component_revision", return_value=None), \
                patch("content_factory.article_review_views.remote_review", return_value=review), \
                patch("content_factory.article_review_views.check_approval_comments", return_value=None), \
                patch("content_factory.article_review_views.record_review_approval") as approve, \
                patch.object(hosted_article_views.hosted, "request_publication", return_value=publication) as queue, \
                patch.object(hosted_article_views.hosted, "publication_payload", return_value={"status": "pending"}), \
                patch.object(hosted_article_views, "_dispatch_hosted_publish", return_value={"queued": True}) as dispatch, \
                patch.object(views, "_start_portable_preview_of_unpreviewed_revision", side_effect=start_error) as start:
            response = view.post(request, self.run.run_id)
        return response, SimpleNamespace(approve=approve, queue=queue, dispatch=dispatch, start=start)

    def assert_refused(self, response, effects, reason):
        self.assertEqual((response.status_code, response.data["reason"]), (409, reason))
        effects.queue.assert_not_called()
        effects.approve.assert_not_called()
        effects.dispatch.assert_not_called()
        if reason == "preview_required":
            # Content Factory's ladder is asked for this revision's preview (a no-op unless it previews portable drafts).
            effects.start.assert_called_once_with(self.run)
        else:
            effects.start.assert_not_called()

    def test_flag_off_publishes_on_the_reviewed_revision_alone(self):
        for label, review in (
            ("older_content_factory", {"revision": "rev-1", "previewPending": False}),
            ("no_preview", {"revision": "rev-1", "previewPending": False, "previewedRevision": ""}),
        ):
            with self.subTest(label):
                response, effects = self.publish(review)
                self.assertEqual(response.status_code, 202, response.data)
                effects.dispatch.assert_called_once_with(effects.queue.return_value, "rev-1")
                effects.start.assert_not_called()

    @override_settings(HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW=True)
    def test_flag_on_requires_a_rendered_preview_of_the_reviewed_revision(self):
        response, effects = self.publish({"revision": "rev-1", "previewPending": False, "previewedRevision": "rev-1"})
        self.assertEqual(response.status_code, 202, response.data)
        effects.dispatch.assert_called_once_with(effects.queue.return_value, "rev-1")
        effects.start.assert_not_called()
        with self.assertLogs("content_factory.hosted_article_views", level="ERROR"):
            response, effects = self.publish({"revision": "rev-1", "previewPending": False, "previewedRevision": ""},
                                             start_error=RuntimeError("Content Factory is down"))
        # A failed ask never hides the refusal.
        self.assert_refused(response, effects, "preview_required")
        for label, review in (
            ("no_preview", {"revision": "rev-1", "previewPending": False, "previewedRevision": ""}),
            ("older_revision_previewed", {"revision": "rev-1", "previewPending": False, "previewedRevision": "rev-0"}),
            ("older_content_factory", {"revision": "rev-1", "previewPending": False}),
        ):
            with self.subTest(label):
                response, effects = self.publish(review)
                self.assert_refused(response, effects, "preview_required")
                self.assertEqual(response.data["detail"], "Open the rendered preview of this version before publishing.")

    @override_settings(HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW=True)
    def test_a_changed_review_is_still_reported_first(self):
        for label, review, reviewed in (
            ("pending_preview", {"revision": "rev-1", "previewPending": True, "previewedRevision": "rev-1"}, "rev-1"),
            ("newer_revision", {"revision": "rev-2", "previewPending": False, "previewedRevision": "rev-1"}, "rev-1"),
            ("nothing_reviewed", {"revision": "rev-1", "previewPending": False, "previewedRevision": "rev-1"}, ""),
        ):
            with self.subTest(label):
                response, effects = self.publish(review, reviewed=reviewed)
                self.assert_refused(response, effects, "review_changed")
