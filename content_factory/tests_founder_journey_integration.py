"""P1 sync: the founder journey against the real producers it reads.

be-journey projects the journey from facts other P1 packages write. These
cases drive those producers through their own entry points, not stubs:

- be-start's owner approve of a setup run through ``VibeMarketingRunControlView``
  records ``setupApproval`` (be-github-b's ``website_setup_owner_approval``);
  the journey then moves from ``approve_setup`` to ``publishing`` ("auto") or
  to ``approve_on_github`` with the pull request URL ("manual"), an ask that
  ends once the pull request is recorded merged or closed, or the branch moved
  on, and a copied draft is then done;
- with V2 switched off afterwards, ``_maybe_auto_publish_setup`` (reaching its
  owner-click guard) and ``maybe_merge_prepared_setup`` still never merge an
  unapproved setup (G10);
- be-draft's copy receipt or posted link makes a content_only draft done with
  ``next: view_draft``, and a draft with neither stays in review;
- a GitHub return reason that be-github-a's callback stores shows in
  ``githubReturn`` for 15 minutes, then disappears.

Content Factory, GitHub, prices and balances are mocked; nothing leaves the
process.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from integrations.tests_github_callback_reasons import CallbackReasonTestBase, _json
from integrations.services import github_callback_reasons as reasons
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

from . import founder_journey as journeys
from . import founder_journey_cohort as cohort
from . import vibe_marketing_views as views
from . import website_prepare
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_new_journey_org
from .tests_founder_draft_export import snapshot
from .website_connections import contract_for
from .website_models import WebsiteConnection, WebsiteConnectionTarget

FREE = {"ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": False,
        "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 0}
FLAGS = {**ALL_P1_FLAGS_ON, **FREE}
V2_OFF = {**FLAGS, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40
PR_NUMBER = 7
ONE_LINER = "Booking software for physiotherapy clinics"


def _journey_reads():
    """Patch the price, balance and topic pool reads every projection makes."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(views, "get_content_factory_article_cost_points", return_value=12))
    stack.enter_context(mock.patch.object(views, "_roo_points_balance_for_user", return_value=0))
    stack.enter_context(mock.patch.object(views, "_topic_selection_candidate_pool", return_value=[]))
    return stack


def _project(built, **kwargs):
    with _journey_reads():
        return journeys.project_founder_journey(built.organization, user=built.owner, company=built.company, **kwargs)


def _describe(organization):
    StartupProfile.objects.update_or_create(organization=organization, defaults={"short_description": ONE_LINER})


def _unverified_connection(built):
    """A connected repository whose articles target the setup has not verified yet (setup still to land)."""
    connection = WebsiteConnection.objects.create(
        organization=built.organization, github_repo="banksia/website", repository_id=777, installation_id="900",
        branch="main", site_url=f"https://{built.organization.domain}", state=WebsiteConnection.State.CONNECTED,
        verified_sha=BASE_SHA)
    WebsiteConnectionTarget.objects.create(connection=connection, target_key="articles", generation=connection.generation,
                                           adapter="next_app_router", source_sha=BASE_SHA,
                                           contract={"route": "/blog/{slug}"}, verified_at=None)
    built.config.website_connection = connection
    built.config.github_repo = connection.github_repo
    built.config.save(update_fields=["website_connection", "github_repo", "updated_at"])
    built.connection = connection
    return connection


def _setup_run(built, run_id=None):
    connection = built.connection
    run_id = run_id or f"setup-{uuid4().hex[:10]}"
    pr_url = f"https://github.com/{connection.github_repo}/pull/{PR_NUMBER}"
    return ContentFactoryRun.objects.create(
        run_id=run_id, workflow="article_system_setup", domain=built.organization.domain,
        organization=built.organization, github_repo=connection.github_repo, status="awaiting_approval",
        current_step="await_review", approval_state="approval_required",
        run_request={**contract_for(connection), "expected_source_sha": BASE_SHA},
        result={"status": "preview_ready", "source_sha": BASE_SHA, "branch_commit_sha": HEAD_SHA,
                "pr_number": PR_NUMBER, "pr_url": pr_url,
                "live_preview": {"url": "https://preview.example.test/blog"},
                "article_system_setup": {"setup_run_id": run_id, "status": "preview_ready",
                                         "merge_status": "not_merged", "pr_number": PR_NUMBER, "pr_url": pr_url}})


@override_settings(**FLAGS)
class OwnerSetupApproveTests(TestCase):
    """be-start's approve hook writes be-github-b's receipt; the journey follows it."""

    def setUp(self):
        self.built = make_new_journey_org(domain=f"banksia-{uuid4().hex[:6]}.example.com")
        _describe(self.built.organization)
        _unverified_connection(self.built)
        self.run = _setup_run(self.built)

    def approve(self, run, body):
        """POST the approve through VibeMarketingRunControlView with Content Factory and GitHub mocked."""
        repo = self.built.connection.github_repo
        reply = {"status": "setup_pr_created", "pr_url": f"https://github.com/{repo}/pull/{PR_NUMBER}",
                 "pr_number": PR_NUMBER, "current_step": "create_pull_request"}
        client = APIClient()
        client.force_authenticate(self.built.owner)
        url = f"/api/v1/vibe-marketing/runs/{run.run_id}/approve/?company_id={self.built.company.pk}"
        # The repository identity and head reads are GitHub's; the guard itself runs.
        native = {"id": self.built.connection.repository_id, "full_name": repo, "default_branch": "main"}
        with mock.patch("content_factory.website_connections.verify_repository_head",
                        side_effect=lambda connection, sha: sha), \
                mock.patch("content_factory.website_connections.read_repository_native_target", return_value=native), \
                mock.patch.object(views, "_call_content_factory_run_action", return_value=reply) as remote, \
                mock.patch.object(views, "_attempt_setup_publish_merge") as merge:
            response = client.post(url, {**contract_for(self.built.connection), **body}, format="json")
        return response, remote, merge

    def test_auto_moves_the_journey_from_approve_setup_to_publishing(self):
        before = _project(self.built)
        self.assertEqual((before["stage"], before["state"], (before["action"] or {}).get("id"), before["runId"]),
                         ("setup", "your_turn", "approve_setup", self.run.run_id))
        response, remote, merge = self.approve(self.run, {"merge": "auto"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", response.content))
        remote.assert_called_once()
        merge.assert_not_called()
        approval = cohort.read_marker(self.built.organization)["setupApproval"]
        self.assertEqual((approval["runId"], approval["merge"], approval["by"]),
                         (self.run.run_id, "auto", self.built.owner.pk))
        after = _project(self.built)
        self.assertEqual((after["stage"], after["state"], after["action"], after["holdingReason"]),
                         ("publishing", "working", None, "website_update"))
        self.assertEqual(after["website"]["publishAutomatically"], "in_progress")
        self.assertNotEqual(after["etag"], before["etag"])

    def test_manual_moves_the_journey_to_approve_on_github_with_the_pull_request(self):
        response, _remote, merge = self.approve(self.run, {"merge": "manual"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", response.content))
        merge.assert_not_called()
        self.assertEqual(cohort.read_marker(self.built.organization)["setupApproval"]["merge"], "manual")
        after = _project(self.built)
        self.run.refresh_from_db()
        # The approved setup run completes with its pull request open.
        self.assertEqual(self.run.status, "completed")
        self.assertEqual((after["stage"], after["state"], after["action"]["id"]),
                         ("setup", "your_turn", "approve_on_github"))
        self.assertEqual(after["action"]["url"], f"https://github.com/banksia/website/pull/{PR_NUMBER}")
        self.assertEqual((after["headline"], after["body"], after["action"]["label"]),
                         journeys.COPY["approve_on_github"])
        # Once the change merges on GitHub, nothing waits on the founder there.
        result = dict(self.run.result)
        result["article_system_setup"] = {**result["article_system_setup"], "merge_status": "merged"}
        result["merge_status"] = "merged"
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=result)
        self.assertNotEqual((_project(self.built)["action"] or {}).get("id"), "approve_on_github")

    def manual_with_open_pull_request(self):
        """Approve with "Let me review it on GitHub": the journey asks approve_on_github."""
        response, _remote, _merge = self.approve(self.run, {"merge": "manual"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", response.content))
        self.assertEqual((_project(self.built)["action"] or {}).get("id"), "approve_on_github")
        self.run.refresh_from_db()
        return self.run

    def test_manual_ask_ends_when_the_pull_request_is_recorded_closed(self):
        run = self.manual_with_open_pull_request()
        # The merge status check's own record of a pull request closed on GitHub.
        views._mark_setup_merge_blocked(run=run, config=self.built.config, reason="closed", status_value="pr_closed")
        after = _project(self.built)
        self.assertNotEqual((after["action"] or {}).get("id"), "approve_on_github", after)

    def test_manual_ask_ends_when_the_owner_merged_onto_a_moved_branch(self):
        """Merged with other changes: reconciliation records no merge, the repository moved on, then verifies."""
        self.manual_with_open_pull_request()
        capabilities = views._article_capabilities_for_context
        moved = "c" * 40

        def observed(*args, **kwargs):
            return {**capabilities(*args, **kwargs), "repositorySourceSha": moved}
        with mock.patch.object(views, "_article_capabilities_for_context", side_effect=observed):
            journeys.cache.clear()
            after = _project(self.built)
        self.assertNotEqual((after["action"] or {}).get("id"), "approve_on_github", after)
        # Once the moved source is verified the website is connected, with or without a sha read.
        WebsiteConnectionTarget.objects.filter(connection=self.built.connection).update(verified_at=timezone.now())
        journeys.cache.clear()
        after = _project(self.built)
        self.assertNotEqual((after["action"] or {}).get("id"), "approve_on_github", after)

    def test_a_copied_draft_after_a_manual_setup_that_ended_is_done(self):
        run = self.manual_with_open_pull_request()
        views._mark_setup_merge_blocked(run=run, config=self.built.config, reason="closed", status_value="pr_closed")
        draft = ContentFactoryRun.objects.create(
            run_id=f"run-{uuid4().hex[:12]}", workflow="article_generation", domain=self.built.organization.domain,
            organization=self.built.organization, status="completed", current_step="deliver",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True},
            result={"delivery_mode": "content_only"})
        client = APIClient()
        client.force_authenticate(self.built.owner)
        url = f"/api/v1/vibe-marketing/runs/{draft.run_id}/copied/?company_id={self.built.company.pk}"
        with mock.patch("content_factory.article_review_views.remote_review",
                        return_value=deepcopy(snapshot(draft.run_id))), \
                mock.patch("content_factory.founder_draft_views.views._latest_review_ready_component_revision",
                           return_value=None):
            response = client.post(url, {"revision": "rev-1"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        after = _project(self.built)
        self.assertEqual((after["stage"], after["state"], after["action"]), ("done", "done", None), after)
        self.assertEqual((after["next"]["kind"], after["next"]["runId"]), ("view_draft", draft.run_id))

    def test_g10_with_v2_off_an_unapproved_setup_never_merges(self):
        response, _remote, _merge = self.approve(self.run, {"merge": "auto"})
        self.assertEqual(response.status_code, 200, getattr(response, "data", response.content))
        unapproved = _setup_run(self.built)
        # The shape the auto-publish hook acts on: a completed setup with its pull request open and not merged.
        result = deepcopy(unapproved.result)
        result["status"] = "pr_created"
        result["article_system_setup"].update(status="pr_created", merge_status="not_merged")
        ContentFactoryRun.objects.filter(pk=unapproved.pk).update(
            status="completed", approval_state="approved", current_step="create_pull_request", result=result)
        unapproved.refresh_from_db()
        self.assertTrue(views._setup_pending_status_allows_auto_publish(unapproved))
        self.assertFalse(self.built.config.requires_review)
        context = SimpleNamespace(organization=self.built.organization, company=self.built.company)
        with override_settings(**V2_OFF):
            self.assertFalse(cohort.is_new_journey(self.built.organization))
            with mock.patch.object(views, "_org_config_enables_auto_publish", return_value=True), \
                    mock.patch.object(views, "_attempt_setup_publish_merge", return_value={"outcome": "merged"}) as merge:
                with self.assertLogs("content_factory.vibe_marketing_views", "INFO") as logs:
                    views._maybe_auto_publish_setup(run=unapproved, context=context)
                self.assertTrue(any("vibe_marketing_setup_auto_publish_waits_for_owner" in line
                                    and unapproved.run_id in line for line in logs.output), logs.output)
                outcome = website_prepare.maybe_merge_prepared_setup(unapproved, context, self.built.config,
                                                                     user=self.built.owner)
            merge.assert_not_called()
            self.assertEqual(outcome["status"], "needs_user")
            # The journey is the legacy body while V2 is off.
            self.assertEqual(_project(self.built)["stage"], "legacy")


@override_settings(**FLAGS)
@mock.patch("content_factory.founder_draft_views.views._latest_review_ready_component_revision", return_value=None)
class DraftCopiedOrPostedTests(TestCase):
    """be-draft's receipts end the review: copied or posted is done; neither stays review."""

    def setUp(self):
        self.built = make_new_journey_org(domain=f"site-{uuid4().hex[:8]}.example.com")
        _describe(self.built.organization)
        self.run = ContentFactoryRun.objects.create(
            run_id=f"run-{uuid4().hex[:12]}", workflow="article_generation", domain=self.built.organization.domain,
            organization=self.built.organization, status="completed", current_step="deliver",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True},
            result={"delivery_mode": "content_only"})

    def post(self, route, body):
        client = APIClient()
        client.force_authenticate(self.built.owner)
        url = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/{route}/?company_id={self.built.company.pk}"
        with mock.patch("content_factory.article_review_views.remote_review",
                        return_value=deepcopy(snapshot(self.run.run_id))):
            return client.post(url, body, format="json")

    def assertReview(self, body):
        self.assertEqual((body["stage"], body["state"], body["action"]["id"], body["runId"]),
                         ("review", "your_turn", "review_draft", self.run.run_id))

    def assertDone(self, body):
        self.assertEqual((body["stage"], body["state"], body["action"]), ("done", "done", None))
        self.assertEqual((body["next"]["kind"], body["next"]["runId"]), ("view_draft", self.run.run_id))
        self.assertEqual((body["headline"], body["body"], body["next"]["label"]), journeys.COPY["done_copy_ready"])

    def test_a_draft_with_neither_receipt_stays_in_review(self, _latest):
        self.assertReview(_project(self.built))

    def test_a_copied_draft_is_done(self, _latest):
        self.assertReview(_project(self.built))
        response = self.post("copied", {"revision": "rev-1"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertDone(_project(self.built))

    def test_a_posted_link_makes_the_draft_done(self, _latest):
        response = self.post("posted-link", {"url": "https://medium.com/@founder/how-to-start"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["postedLink"]["check"], "skipped")
        self.assertDone(_project(self.built))

    def test_a_refused_copy_leaves_the_draft_in_review(self, _latest):
        response = self.post("copied", {"revision": "rev-0"})
        self.assertEqual(response.status_code, 409)
        self.assertReview(_project(self.built))


class GithubReturnFromCallbackTests(CallbackReasonTestBase):
    """be-github-a's callback stores the reason; the journey shows it for 15 minutes."""

    def test_a_stored_return_reason_shows_for_fifteen_minutes(self):
        _describe(self.new.organization)
        with override_settings(**FREE):
            self.assertIsNone(_project(self.new)["githubReturn"])
            state = self.chat_state(self.new, native=True)
            response = self.callback(self.params(state), get=[_json(USER_PAYLOAD), _json({}, status=403)])
            self.assertEqual(response.status_code, 302)
            stored = cohort.read_marker(self.new.organization)["githubReturn"]
            self.assertEqual(stored["reason"], reasons.REPOSITORY_ACCESS)
            at = parse_datetime(stored["at"])
            for offset, expected in ((timedelta(0), True), (timedelta(minutes=14, seconds=59), True),
                                     (timedelta(minutes=15, seconds=1), False)):
                with self.subTest(offset=offset):
                    body = _project(self.new, now=at + offset)
                    if expected:
                        self.assertEqual(body["githubReturn"], {"reason": reasons.REPOSITORY_ACCESS, "at": stored["at"]})
                    else:
                        self.assertIsNone(body["githubReturn"])
            self.assertLessEqual(at, timezone.now())


USER_PAYLOAD = {"login": "founder"}
