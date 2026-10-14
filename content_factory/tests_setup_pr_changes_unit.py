"""In-app merge of an approved setup PR: static build proof and changed heads.

TalaThrive PR #40 (14 Oct) could not be merged in MLAI Chat. Its static site was
verified on the hosted platform build (``local_build_skipped``), which the merge
guard refused even at the verified commit, and the owner's follow-up commits
moved the head off that commit with no way to re-verify it. These run without
a database or provider access.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import vibe_marketing_views as views
from . import website_connections as authority
from . import website_reconciliation as reconciliation

BASE, OLD, NEW = "a" * 40, "b" * 40, "c" * 40
CONNECTION = "11111111-1111-4111-8111-111111111111"


def static_setup_run(head=OLD):
    """An approved static-site setup whose PR head ``head`` passed on the hosted build."""
    return SimpleNamespace(
        run_id="setup-1", workflow="article_system_setup", status="completed", approval_state="approved",
        domain="example.test", github_repo="example/site", save=Mock(),
        run_request={"website_connection_id": CONNECTION, "connection_generation": 2,
                     "repository_id": 123, "expected_source_sha": BASE},
        result={"status": "setup_pr_created", "source_sha": BASE, "branch_commit_sha": head, "resume_generation": 0,
                "article_system_setup": {"status": "pr_created", "setup_run_id": "setup-1", "source_sha": BASE,
                                         "branch_commit_sha": head, "resume_generation": 0},
                "live_preview": {"status": "running", "exactRender": True, "commitSha": head,
                                 "previewMode": "platform_deployment", "proof": {"commitSha": head}},
                "directory_quality_gates": {"status": "passed", "local_build_skipped": True,
                                            "local_build_status": "skipped", "local_build_environment_blocked": False,
                                            "local_build_baseline_blocked": False,
                                            **{key: True for key in ("dependency_validation", "static_policy", "local_build",
                                                                     "dom_slot_compliance", "publish_surface", "browser",
                                                                     "visual_style")}},
                "directory_quality_verification": {"status": "passed", "commit_sha": head,
                                                   "preview_identity": f"commit:{head}", "resume_generation": 0,
                                                   "task_id": "verification-1",
                                                   "completed_at": "2026-10-13T07:51:52+00:00"}})


class StaticSetupMergeProofTests(SimpleTestCase):
    def setUp(self):
        self.website = SimpleNamespace(id=CONNECTION, pk=CONNECTION, generation=2, repository_id=123,
            github_repo="example/site", state="connected", verified_sha="",
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.website.targets.filter.return_value = []
        self.run = static_setup_run()

    def validate(self, head=OLD):
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "validate_authority"), \
                patch.object(authority, "verify_repository_head", return_value=BASE):
            authority.validate_setup_merge_source(self.run, head)

    def test_static_setup_proven_by_its_hosted_build_can_merge(self):
        self.validate()

    def test_hosted_build_must_be_the_exact_platform_render_of_the_head(self):
        valid = deepcopy(self.run.result)
        for path, value in ((("live_preview", "previewMode"), "local"),
                            (("live_preview", "proof"), {"commitSha": NEW}),
                            (("directory_quality_gates", "local_build_environment_blocked"), True),
                            (("directory_quality_gates", "local_build_status"), "failed")):
            with self.subTest(path=path):
                self.run.result = deepcopy(valid)
                self.run.result[path[0]][path[1]] = value
                with self.assertRaises(authority.WebsiteAuthorityError):
                    self.validate()

    def test_hosted_build_does_not_make_a_pending_setup_auto_approvable(self):
        self.run.status, self.run.approval_state = "awaiting_approval", "approval_required"
        self.run.result.update(status="preview_ready")
        self.run.result["article_system_setup"]["status"] = "preview_ready"
        self.assertEqual(authority._verified_setup_run_base(self.run, OLD, self.website, automatic_approval=True), "")
        self.run.result["directory_quality_gates"]["local_build_skipped"] = False
        self.assertEqual(authority._verified_setup_run_base(self.run, OLD, self.website, automatic_approval=True), BASE)

    def test_a_refused_merge_attempt_does_not_lock_the_setup_out(self):
        for blocked in ("checks_failed", "manual_merge_required", "setup_changes_failed"):
            with self.subTest(status=blocked):
                self.run.result["status"] = self.run.result["article_system_setup"]["status"] = blocked
                self.validate()

    def test_verified_setup_head_names_the_commit_the_run_proved(self):
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website):
            self.assertEqual(authority.verified_setup_head(self.run), OLD)
            self.run.result["directory_quality_verification"]["status"] = "failed"
            self.assertEqual(authority.verified_setup_head(self.run), "")


class ChangedSetupHeadTests(SimpleTestCase):
    def setUp(self):
        self.run = static_setup_run()
        self.config = SimpleNamespace(website_connection=SimpleNamespace(generation=2), article_system={}, save=Mock())
        self.context = SimpleNamespace(profile=SimpleNamespace(user_id=7), company=SimpleNamespace(pk=3),
                                       organization=SimpleNamespace(domain="example.test"))
        self.pull = {"head": {"sha": NEW}}

    def outcome(self, *, automatic=False, verified=OLD, remote=None):
        with patch.object(authority, "verified_setup_head", return_value=verified), \
                patch.object(views, "_request_setup_pr_reverification",
                             return_value=remote or {"status": "queued", "head_sha": NEW}) as request, \
                patch.object(views, "_schedule_setup_pr_merge") as schedule:
            outcome = views._setup_pr_changes_outcome(run=self.run, context=self.context, config=self.config,
                pull=self.pull, pr_number=40, checks={"state": "success"}, automatic=automatic)
        return outcome, request, schedule

    def test_owner_publish_of_a_changed_head_starts_a_check_and_waits(self):
        outcome, request, schedule = self.outcome()

        self.assertEqual(outcome["outcome"], "waiting")
        self.assertEqual(outcome["detail"], views.SETUP_PR_CHECKING_DETAIL)
        request.assert_called_once_with(run=self.run, head_sha=NEW, context=self.context)
        schedule.assert_called_once_with(run=self.run, context=self.context, config=self.config, head_sha=NEW,
                                         verified_sha=OLD, pr_number=40)
        self.assertEqual(self.run.result["merge_status"], "publishing")
        self.assertEqual(self.run.result["article_system_setup"]["merge_status"], "publishing")
        self.assertEqual(self.run.result["merge_blocked_reason"], "")
        self.assertEqual(self.run.result["status"], "setup_pr_created")
        self.assertEqual(self.config.article_system["pending_article_system_setup"]["merge_status"], "publishing")

    def test_the_verified_head_and_unproven_runs_keep_the_exact_head_guard(self):
        self.pull = {"head": {"sha": OLD}}
        self.assertIsNone(self.outcome()[0])
        self.pull = {"head": {"sha": NEW}}
        outcome, request, _ = self.outcome(verified="")
        self.assertIsNone(outcome)
        request.assert_not_called()

    def test_background_merges_never_accept_changes_pushed_after_review(self):
        # None hands the decision to the exact-head guard, which refuses the changed head.
        outcome, request, schedule = self.outcome(automatic=True)
        self.assertIsNone(outcome)
        request.assert_not_called()
        schedule.assert_not_called()
        self.run.save.assert_not_called()

    def test_a_running_check_for_the_same_head_is_not_started_twice(self):
        self.run.result["setup_pr_reverification"] = {"head_sha": NEW, "status": "building"}
        for automatic in (False, True):
            outcome, request, _ = self.outcome(automatic=automatic)
            self.assertEqual(outcome["outcome"], "waiting")
            request.assert_not_called()

    def test_a_head_content_factory_already_verified_refreshes_and_merges_normally(self):
        with patch.object(views, "_call_content_factory_run_status", return_value={"run_id": "setup-1"}) as status, \
                patch.object(views, "_sync_local_run_from_remote") as sync:
            outcome, _, schedule = self.outcome(remote={"status": "verified", "head_sha": NEW})
        self.assertIsNone(outcome)
        status.assert_called_once_with("setup-1", workflow="article_system_setup")
        sync.assert_called_once_with(self.run, {"run_id": "setup-1"})
        schedule.assert_not_called()

    def test_an_unreachable_content_factory_is_a_retryable_error(self):
        self.run.result["failure"] = {"code": "DIRECTORY_STYLE_REVIEW_FAILED", "failure_class": "repairable"}
        saved_result = deepcopy(self.run.result)
        with patch.object(authority, "verified_setup_head", return_value=OLD), \
                patch.object(views, "_request_setup_pr_reverification", side_effect=ValueError("down")):
            outcome = views._setup_pr_changes_outcome(run=self.run, context=self.context, config=self.config,
                pull=self.pull, pr_number=40, checks={}, automatic=False)
        self.assertEqual((outcome["outcome"], outcome["retryable"]), ("error", True))
        self.assertEqual(outcome["code"], "setup_reverification_unavailable")
        self.assertEqual(self.run.result, saved_result)
        self.run.save.assert_not_called()

    def test_merge_is_scheduled_once_per_head_and_reopened_by_a_later_publish(self):
        import json
        from uuid import UUID
        from .website_models import WebsiteConnectionOperation
        # Production company keys are UUIDs (TalaThrive's Publish failed with
        # "Object of type UUID is not JSON serializable" on 14 Oct).
        company = UUID("83b7b463-cebc-4743-bd9f-562429e273b5")
        self.context.company = SimpleNamespace(pk=company)
        created = SimpleNamespace(pk="op-1", state="pending")
        with patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(created, True)) as create:
            views._schedule_setup_pr_merge(run=self.run, context=self.context, config=self.config,
                head_sha=NEW, verified_sha=OLD, pr_number=40)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], f"setup-pr-merge:setup-1:{NEW}")
        self.assertEqual(kwargs["defaults"]["action"], "setup-pr-merge")
        payload = json.loads(json.dumps(kwargs["defaults"]["payload"]))
        self.assertEqual({key: payload[key] for key in
                          ("run_id", "head_sha", "verified_sha", "company_id", "requested_by_user_id")},
                         {"run_id": "setup-1", "head_sha": NEW, "verified_sha": OLD, "company_id": str(company),
                          "requested_by_user_id": "7"})
        finished = SimpleNamespace(pk="op-1", state="completed")
        with patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(finished, False)), \
                patch.object(WebsiteConnectionOperation.objects, "filter") as reopen:
            views._schedule_setup_pr_merge(run=self.run, context=self.context, config=self.config,
                head_sha=NEW, verified_sha=OLD, pr_number=40)
        reopen.assert_called_once_with(pk="op-1")
        self.assertEqual(reopen.return_value.update.call_args.kwargs["state"], "pending")


class SetupPrMergeFollowupTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.operation = SimpleNamespace(pk="op-1", state="pending", attempts=0, next_attempt_at=None, save=Mock(),
            payload={"run_id": "setup-1", "head_sha": NEW, "verified_sha": OLD, "company_id": 3,
                     "requested_by_user_id": 7, "requested_at": self.now.isoformat()})
        self.run = static_setup_run(head=NEW)
        self.context = SimpleNamespace(organization=SimpleNamespace(domain="example.test"))
        self.config = SimpleNamespace(article_system={})

    def process(self, record, merge=None, now=None, context_error=None):
        from .website_models import WebsiteConnectionOperation
        self.run.result["setup_pr_reverification"] = record
        self.operation.next_attempt_at = None  # due now; a previous pass pushed it forward
        objects = Mock()
        objects.select_for_update.return_value.filter.return_value.first.return_value = self.operation
        with patch.object(WebsiteConnectionOperation, "objects", objects), \
                patch.object(reconciliation.transaction, "atomic", nullcontext), \
                patch("django.contrib.auth.get_user_model") as users, \
                patch("workflow_runs.models.ContentFactoryRun.objects") as runs, \
                patch.object(views, "get_founder_company_context", return_value=self.context,
                             side_effect=context_error) as context, \
                patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_call_content_factory_run_status", return_value={"run_id": "setup-1"}), \
                patch.object(views, "_sync_local_run_from_remote", side_effect=lambda run, remote: run), \
                patch.object(views, "_attempt_setup_publish_merge", return_value=merge or {"outcome": "merged"}) as attempt, \
                patch.object(views, "_mark_setup_merge_blocked") as blocked:
            runs.filter.return_value.first.return_value = self.run
            users.return_value.objects.get.return_value = SimpleNamespace(pk=7)
            state = reconciliation._process_setup_pr_merge("op-1", now or self.now)
        final = objects.filter.return_value.update.call_args.kwargs
        return state, final, attempt, blocked, context

    def test_a_passed_check_merges_for_the_owner_who_published(self):
        state, final, attempt, blocked, context = self.process({"head_sha": NEW, "status": "passed"})
        self.assertEqual(state, "completed")
        self.assertEqual(final["receipt"]["status"], "merged")
        attempt.assert_called_once_with(run=self.run, context=self.context, automatic=False)
        context.assert_called_once()
        blocked.assert_not_called()

    def test_a_running_check_waits_and_a_github_wait_retries(self):
        state, final, attempt, _, _ = self.process({"head_sha": NEW, "status": "verifying"})
        self.assertEqual((state, final["receipt"]["status"]), ("pending", "checking"))
        attempt.assert_not_called()
        state, final, _, _, _ = self.process({"head_sha": NEW, "status": "passed"}, merge={"outcome": "waiting"})
        self.assertEqual((state, final["receipt"]["status"]), ("pending", "merge_waiting"))

    def test_a_failed_check_tells_the_owner_why(self):
        state, final, attempt, blocked, _ = self.process({"head_sha": NEW, "status": "failed",
            "error": "The updated pull request did not build: npm run build exited 1", "error_code": "PREVIEW_BUILD_FAILED"})
        self.assertEqual((state, final["receipt"]["status"]), ("completed", "verification_failed"))
        self.assertEqual(final["receipt"]["code"], "PREVIEW_BUILD_FAILED")
        attempt.assert_not_called()
        reason = blocked.call_args.kwargs["reason"]
        self.assertIn("npm run build exited 1", reason)
        self.assertIn("Fix it on GitHub, then publish again.", reason)
        self.assertEqual(blocked.call_args.kwargs["status_value"], "setup_changes_failed")

    def test_revoked_authority_keeps_its_registered_code_without_changing_the_run(self):
        from .website_contract import WebsiteAuthorityError
        saved_result = deepcopy(self.run.result)
        state, final, attempt, blocked, _ = self.process({}, context_error=WebsiteAuthorityError(
            "website_disconnected", "This website is disconnected."))
        self.assertEqual((state, final["receipt"]["status"]), ("cancelled", "authority_revoked"))
        self.assertEqual(final["receipt"]["code"], "website_disconnected")
        self.assertFalse(final["receipt"]["repository_modified"])
        self.assertEqual(self.run.result, {**saved_result, "setup_pr_reverification": {}})
        self.run.save.assert_not_called()
        attempt.assert_not_called()
        blocked.assert_not_called()

    def test_a_newer_publish_or_a_stuck_check_ends_the_followup(self):
        state, final, _, _, _ = self.process({"head_sha": "d" * 40, "status": "queued"})
        self.assertEqual((state, final["receipt"]["status"]), ("cancelled", "superseded"))
        state, final, _, blocked, _ = self.process({"head_sha": NEW, "status": "building"},
                                                   now=self.now + timedelta(minutes=61))
        self.assertEqual((state, final["receipt"]["status"]), ("completed", "timed_out"))
        self.assertIn("took too long", blocked.call_args.kwargs["reason"])


class MergeGuardOrderTests(SimpleTestCase):
    """The change check runs only after the exact-head guard refuses the PR."""

    def merge(self, *, guard_errors, changes=None):
        from .website_contract import WebsiteAuthorityError
        run = static_setup_run()
        config = SimpleNamespace(website_connection=SimpleNamespace(generation=2), github_repo="example/site")
        errors = [WebsiteAuthorityError(code, "refused") for code in guard_errors]
        with patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "_pull_request_number_from_run", return_value=40), \
                patch.object(views, "_github_token_for_repo_operation", return_value=("token", "fixture")), \
                patch.object(views, "_github_pull_checks_state_lenient",
                             return_value=({"head": {"sha": NEW}, "merged": False}, {"state": "success"})), \
                patch.object(authority, "validate_setup_merge_source", side_effect=errors) as guard, \
                patch.object(views, "_setup_pr_changes_outcome", return_value=changes) as changed:
            try:
                outcome = views._attempt_setup_publish_merge_authorized.__wrapped__(
                    run=run, context=SimpleNamespace(organization=SimpleNamespace(domain="example.test")))
            except WebsiteAuthorityError as exc:
                outcome = exc
        return outcome, guard, changed

    def test_a_changed_head_waits_for_its_check(self):
        waiting = {"outcome": "waiting", "detail": views.SETUP_PR_CHECKING_DETAIL}
        outcome, guard, changed = self.merge(guard_errors=["setup_verification_required"], changes=waiting)
        self.assertEqual(outcome, waiting)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(changed.call_args.kwargs["automatic"], False)

    def test_other_refusals_and_unproven_heads_are_raised_as_before(self):
        outcome, guard, changed = self.merge(guard_errors=["website_source_changed"])
        self.assertEqual(outcome.code, "website_source_changed")
        changed.assert_not_called()
        outcome, guard, changed = self.merge(guard_errors=["setup_verification_required"] * 2)
        self.assertEqual(outcome.code, "setup_verification_required")
        self.assertEqual(guard.call_count, 2)


class SecondPublishDuringRecheckTests(SimpleTestCase):
    """A second Publish while Content Factory re-checks the changed head waits.

    Rebuilding the preview mirrors ``status=running`` onto the run, so the run's
    own proof is unavailable. The active check for that exact head must still
    answer "waiting" instead of the exact-head guard's 409.
    """

    def setUp(self):
        self.run = static_setup_run()
        self.run.status = "running"
        self.run.result["setup_pr_reverification"] = {"head_sha": NEW, "status": "building"}
        self.website = SimpleNamespace(id=CONNECTION, pk=CONNECTION, generation=2, repository_id=123,
            github_repo="example/site", state="connected", verified_sha="",
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.website.targets.filter.return_value = []
        self.config = SimpleNamespace(website_connection=SimpleNamespace(generation=2), github_repo="example/site",
            article_system={}, save=Mock())
        self.context = SimpleNamespace(organization=SimpleNamespace(domain="example.test"),
            profile=SimpleNamespace(user_id=7), company=SimpleNamespace(pk=3))

    def publish(self):
        from .website_contract import WebsiteAuthorityError
        refused = WebsiteAuthorityError("setup_verification_required",
            "The setup pull request changed or its exact preview has not passed verification.")
        with patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "_pull_request_number_from_run", return_value=40), \
                patch.object(views, "_github_token_for_repo_operation", return_value=("token", "fixture")), \
                patch.object(views, "_github_pull_checks_state_lenient", return_value=(
                    {"head": {"sha": NEW}, "merged": False, "state": "open"}, {"state": "unknown", "ready": True})), \
                patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext(self.website)), \
                patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "validate_setup_merge_source", side_effect=refused) as guard, \
                patch.object(views, "_request_setup_pr_reverification",
                             side_effect=AssertionError("An active check must not be requested again")), \
                patch.object(views, "_schedule_setup_pr_merge") as schedule:
            proven = authority.verified_setup_head(self.run)
            merged, error = views._merge_setup_pr_for_run(run=self.run, context=self.context)
        return proven, merged, error, guard, schedule

    def test_second_publish_while_the_head_is_rechecked_waits_instead_of_409(self):
        proven, merged, error, guard, schedule = self.publish()
        self.assertEqual(proven, "")  # The trigger: no proof while the run is running.
        self.assertIsNone(error)
        self.assertIs(merged, self.run)
        self.assertEqual(guard.call_count, 1)
        schedule.assert_not_called()
        self.run.save.assert_not_called()

    def test_a_finished_or_other_head_check_still_uses_the_exact_head_guard(self):
        for record in ({"head_sha": NEW, "status": "failed"}, {"head_sha": "d" * 40, "status": "building"}):
            with self.subTest(record=record):
                self.run.result["setup_pr_reverification"] = record
                _, merged, error, guard, _ = self.publish()
                self.assertIsNone(merged)
                self.assertEqual(error.status_code, 409)
                self.assertEqual(guard.call_count, 2)
