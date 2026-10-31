"""P1.14: a new-journey setup merges only after the owner's click; legacy merge policy is unchanged."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import founder_journey_cohort as cohort
from . import website_prepare
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from .tests_setup_merge import PR, PR_URL, SetupMergeBase, _open_pull
from .website_connections import contract_for
from .website_models import WebsiteConnection, WebsiteConnectionTarget
from .website_setup_owner_approval import may_merge_setup, on_owner_setup_approve, require_owner_click, requires_owner_click
from content_factory import vibe_marketing_views as views

BASE, HEAD = "a" * 40, "b" * 40
LEGACY = ("talathrive", "vyavos", "pawshare", "la_vie", "mlai")
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


def _connect(built):
    connection = WebsiteConnection.objects.create(
        organization=built.organization, github_repo="banksia/website", repository_id=777, installation_id="900",
        branch="main", site_url=f"https://{built.organization.domain}", state=WebsiteConnection.State.CONNECTED,
        verified_sha=BASE)
    built.config.website_connection = connection
    built.config.github_repo = connection.github_repo
    built.config.save(update_fields=["website_connection", "github_repo", "updated_at"])
    built.connection = connection
    return connection


def _setup_run(built, run_id="setup-1", *, pr=True):
    connection = built.connection
    result = {"status": "preview_ready", "source_sha": BASE, "branch_commit_sha": HEAD,
        "live_preview": {"url": "https://preview.example.test/blog"},
        "article_system_setup": {"setup_run_id": run_id, "status": "preview_ready", "merge_status": "not_merged"}}
    if pr:
        result.update(pr_number=PR, pr_url=f"https://github.com/{connection.github_repo}/pull/{PR}")
    return ContentFactoryRun.objects.create(run_id=run_id, workflow="article_system_setup", domain=built.organization.domain,
        organization=built.organization, github_repo=connection.github_repo, status="awaiting_approval",
        approval_state="approval_required", run_request={**contract_for(connection), "expected_source_sha": BASE}, result=result)


def _context(built):
    return SimpleNamespace(organization=built.organization, company=built.company)


class OwnerApprovalHooksTests(TestCase):
    @override_settings(**ALL_P1_FLAGS_ON)
    def test_both_choices_record_the_receipt_and_turn_on_publish_automatically(self):
        for choice in ("auto", "manual"):
            with self.subTest(choice=choice):
                built = make_new_journey_org()
                run = SimpleNamespace(run_id=f"setup-{choice}")
                self.assertTrue(requires_owner_click(built.organization))
                self.assertEqual(on_owner_setup_approve(organization=built.organization, user=built.owner, run=run,
                    data={"merge": choice} if choice == "manual" else {}), {"merge": choice})
                marker = cohort.read_marker(built.organization)
                self.assertEqual({key: marker["setupApproval"][key] for key in ("runId", "merge", "by")},
                    {"runId": run.run_id, "merge": choice, "by": built.owner.pk})
                self.assertEqual(marker["publishAutomatically"]["origin"], "owner_setup_approval")
                self.assertIs(marker["ownerClickRequired"], True)
                self.assertIs(may_merge_setup(organization=built.organization, run=run), choice == "auto")
                self.assertIs(may_merge_setup(organization=built.organization, run=SimpleNamespace(run_id="another")), False)

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_an_existing_publish_automatically_keeps_its_origin(self):
        built = make_new_journey_org()
        cohort.update_marker(built.organization, publish_automatically={"enabledAt": "2026-10-30T00:00:00+00:00",
            "enabledBy": built.owner.pk, "origin": "opt_in_existing_publisher"})
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=SimpleNamespace(run_id="s"), data={})
        self.assertEqual(cohort.read_marker(built.organization)["publishAutomatically"]["origin"], "opt_in_existing_publisher")

    def test_g10_the_owner_click_survives_v2_off_and_the_disabled_list(self):
        with override_settings(**ALL_P1_FLAGS_ON):
            built = make_new_journey_org()
            _connect(built)
            with patch("content_factory.website_goal.enrolled", return_value=False):
                website_prepare.start_prepare(built.config, company_id=built.company.pk, user=built.owner)
            self.assertIs(cohort.read_marker(built.organization)["ownerClickRequired"], True)
        run = SimpleNamespace(run_id="setup-1")
        for label, flags in (("v2 off", V2_OFF), ("disabled", {**ALL_P1_FLAGS_ON,
                "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS": built.organization.domain})):
            with self.subTest(label), override_settings(**flags):
                self.assertFalse(cohort.is_new_journey(built.organization))
                self.assertTrue(requires_owner_click(built.organization))
                self.assertIs(may_merge_setup(organization=built.organization, run=run), False)

    def test_legacy_orgs_never_need_the_click_and_the_hooks_return_none(self):
        for name in LEGACY:
            with self.subTest(org=name), override_settings(**ALL_P1_FLAGS_ON):
                built = make_named_legacy_org(name)
                before = cohort.read_marker(built.organization)
                run = SimpleNamespace(run_id=f"{name}-setup")
                require_owner_click(built.organization)
                self.assertFalse(requires_owner_click(built.organization))
                self.assertIsNone(on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={"merge": "manual"}))
                self.assertIsNone(may_merge_setup(organization=built.organization, run=run))
                self.assertEqual(cohort.read_marker(built.organization), before)


class PrepareOwnerClickTests(TestCase):
    def setUp(self):
        super().setUp()
        for target in ("content_factory.website_connections.verify_repository_head",):
            patcher = patch(target, side_effect=lambda connection, sha: sha)
            patcher.start()
            self.addCleanup(patcher.stop)

    def approve(self, built, run):
        response = SimpleNamespace(status_code=200, data={})
        with patch("content_factory.website_connections._verified_setup_run_base", return_value=BASE), \
                patch.object(views.VibeMarketingRunControlView, "post", return_value=response) as post:
            outcome = website_prepare.approve_prepared_setup(run, _context(built), built.config, user=built.owner)
        return outcome, post

    def merge(self, built, run):
        with patch.object(views, "_attempt_setup_publish_merge", return_value={"outcome": "merged"}) as merge:
            outcome = website_prepare.maybe_merge_prepared_setup(run, _context(built), built.config, user=built.owner)
        return outcome, merge

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_preparation_never_approves_a_new_journey_setup_itself(self):
        built = make_new_journey_org()
        _connect(built)
        run = _setup_run(built, pr=False)
        outcome, post = self.approve(built, run)
        self.assertEqual((outcome["status"], outcome["user_action"]["id"]), ("needs_user", "review_setup"))
        post.assert_not_called()
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={})
        outcome, post = self.approve(built, run)
        self.assertEqual(outcome, {"status": "pending"})
        post.assert_not_called()

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_an_approval_that_never_reached_the_run_offers_the_review_again(self):
        """The owner's receipt was recorded but the run-control approve failed (for example a 409)."""
        built = make_new_journey_org()
        _connect(built)
        run = _setup_run(built, pr=False)
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={})
        marker = cohort.read_marker(built.organization)
        stale = (timezone.now() - website_prepare.OWNER_APPROVAL_SETTLE - timedelta(minutes=1)).isoformat()
        cohort.update_marker(built.organization, setup_approval={**marker["setupApproval"], "at": stale})
        outcome, post = self.approve(built, run)
        self.assertEqual((outcome["status"], outcome["user_action"]["id"]), ("needs_user", "review_setup"))
        post.assert_not_called()
        # Once the approve reached the run, preparation waits for its pull request.
        run.status, run.approval_state = "running", "approved"
        run.save(update_fields=["status", "approval_state"])
        outcome, post = self.approve(built, run)
        self.assertEqual(outcome, {"status": "pending"})
        post.assert_not_called()

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_the_manual_choice_uses_the_plain_github_copy(self):
        built = make_new_journey_org()
        _connect(built)
        run = _setup_run(built)
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={"merge": "manual"})
        outcome, merge = self.merge(built, run)
        self.assertEqual(outcome["message"], "Your change is waiting on GitHub. Approve it on GitHub when you're ready.")
        self.assertNotIn("pull request", outcome["message"])
        merge.assert_not_called()

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_a_new_journey_setup_merges_only_with_an_auto_receipt_for_that_run(self):
        built = make_new_journey_org()
        _connect(built)
        run = _setup_run(built)
        outcome, merge = self.merge(built, run)
        self.assertEqual((outcome["status"], outcome["user_action"]["id"]), ("needs_user", "review_setup"))
        merge.assert_not_called()
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=SimpleNamespace(run_id="older"), data={})
        self.assertEqual(self.merge(built, run)[1].call_count, 0)
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={"merge": "manual"})
        outcome, merge = self.merge(built, run)
        self.assertEqual(outcome["user_action"]["id"], "merge_pr")
        self.assertEqual(outcome["user_action"]["url"], f"https://github.com/banksia/website/pull/{PR}")
        merge.assert_not_called()
        on_owner_setup_approve(organization=built.organization, user=built.owner, run=run, data={})
        outcome, merge = self.merge(built, run)
        self.assertEqual(outcome, {"status": "pending"})
        merge.assert_called_once()
        self.assertIs(merge.call_args.kwargs["automatic"], True)

    def test_g10_an_unapproved_setup_never_auto_merges_after_v2_is_switched_off(self):
        with override_settings(**ALL_P1_FLAGS_ON):
            built = make_new_journey_org()
            _connect(built)
            require_owner_click(built.organization)
            run = _setup_run(built)
        for label, flags in (("v2 off", V2_OFF), ("disabled", {**ALL_P1_FLAGS_ON,
                "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS": str(built.organization.pk)})):
            with self.subTest(label), override_settings(**flags):
                outcome, merge = self.merge(built, run)
                merge.assert_not_called()
                self.assertEqual(outcome["status"], "needs_user")
                _, post = self.approve(built, _setup_run(built, run_id=f"setup-{label.replace(' ', '-')}", pr=False))
                post.assert_not_called()

    def test_legacy_merge_policy_is_unchanged_with_every_flag_on(self):
        for name in LEGACY:
            with self.subTest(org=name), override_settings(**ALL_P1_FLAGS_ON):
                built = make_named_legacy_org(name)
                run = _setup_run(built, run_id=f"{name}-setup")
                outcome, merge = self.merge(built, run)
                self.assertEqual(outcome, {"status": "pending"})
                merge.assert_called_once()
                self.assertIs(merge.call_args.kwargs["automatic"], True)
                outcome, post = self.approve(built, _setup_run(built, run_id=f"{name}-preview", pr=False))
                self.assertEqual(outcome, {"status": "pending"})
                post.assert_called_once()


@override_settings(**ALL_P1_FLAGS_ON)
class ZeroRequiredChecksTests(SetupMergeBase):
    """A repository with no required checks merges only after MLAI verified that exact head."""

    def setUp(self):
        super().setUp()
        self.assertTrue(cohort.is_new_journey(self.org))
        on_owner_setup_approve(organization=self.org, user=None, run=self.run, data={})
        self.puts = []

    def fake_api(self, method, path, *, token=None, body=None, expected=(200,)):
        if method == "GET" and path.endswith(f"/pulls/{PR}"):
            return _open_pull()
        if method == "GET" and "/status" in path:
            return {"total_count": 0, "state": ""}
        if method == "GET" and "/check-runs" in path:
            return {"check_runs": []}
        if method == "PUT" and path.endswith(f"/pulls/{PR}/merge"):
            self.puts.append(body)
            return {"merged": True}
        raise AssertionError(f"unexpected {method} {path}")

    def maybe_merge(self):
        with self._patch_token(), patch.object(views, "_github_api_request", side_effect=self.fake_api):
            return website_prepare.maybe_merge_prepared_setup(self.run, self.context, self.config, user=None)

    def test_mlai_build_verification_of_the_head_is_required(self):
        WebsiteConnectionTarget.objects.filter(connection=self.website).delete()
        outcome = self.maybe_merge()
        self.assertEqual(outcome["status"], "needs_user")
        self.assertEqual(self.puts, [])

    def test_a_verified_head_merges_after_the_owner_click(self):
        self.assertEqual(self.maybe_merge(), {"status": "pending"})
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.puts[0]["sha"], "c" * 40)
        self.assertEqual(PR_URL, f"https://github.com/{self.website.github_repo}/pull/{PR}")
