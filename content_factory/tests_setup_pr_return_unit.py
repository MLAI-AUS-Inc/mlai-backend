"""An open setup pull request stays the next step until it is published.

TalaThrive (14 Oct) had an approved setup with PR #40 open. The prepare worker
could not merge it automatically, so the Articles panel offered "Try again",
which started a brand-new setup and forgot the PR. Its connection was last
verified before the customer's own commits on 9 Oct, so even Publish would have
been refused. These run without a database or provider access.
"""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase

from . import vibe_marketing_views as views
from . import website_connections as authority
from . import website_operations as operations
from . import website_prepare as prepare
from .website_contract import WebsiteAuthorityError
from .website_journey import project_journey
from . import tests_prepare_journey_unit as replay
from . import tests_setup_pr_changes_unit as changes

BASE, OLD = changes.BASE, "d" * 40
PR_URL = "https://github.com/fixture/site/pull/40"


def open_setup_run(run_id="setup-1", *, base=BASE, **fields):
    """An approved setup whose PR #40 waits to merge, built on ``base``."""
    run = SimpleNamespace(run_id=run_id, workflow="article_system_setup", status="completed",
        approval_state="approved", github_repo="fixture/site", save=Mock(),
        run_request={"website_connection_id": "website", "connection_generation": 3, "expected_source_sha": base},
        result={"status": "setup_pr_created", "pr_number": 40, "pr_url": PR_URL, "merge_status": "not_merged",
            "article_system_setup": {"status": "pr_created", "pr_number": 40, "pr_url": PR_URL,
                "merge_status": "not_merged"}})
    for key, value in fields.items():
        setattr(run, key, value)
    return run


def newest_first(*runs):
    query = MagicMock()
    query.order_by.return_value = list(runs)
    return query


class OpenSetupPullRequestTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk="website", generation=3, organization_id=7, github_repo="Fixture/Site")

    def lookup(self, *runs, source_sha=BASE):
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(*runs)) as rows:
            return authority.open_setup_pr_run(self.connection, source_sha=source_sha), rows

    def test_finds_the_newest_open_pull_request_on_the_current_head(self):
        found, rows = self.lookup(open_setup_run("newest"), open_setup_run("older"))
        self.assertEqual(found.run_id, "newest")
        self.assertEqual(rows.call_args.kwargs["run_request__connection_generation"], 3)
        self.assertEqual(rows.call_args.kwargs["run_request__website_connection_id"], "website")

    def test_a_pull_request_built_on_an_older_head_is_not_resumed(self):
        found, _ = self.lookup(open_setup_run(base=OLD))
        self.assertIsNone(found)

    def test_unknown_source_never_queries(self):
        found, rows = self.lookup(open_setup_run(), source_sha="")
        self.assertIsNone(found)
        rows.assert_not_called()

    def test_merged_closed_unapproved_and_other_repository_runs_are_skipped(self):
        def changed(**result_changes):
            run = open_setup_run("changed")
            run.result.update(result_changes)
            run.result["article_system_setup"].update(result_changes)
            return run
        for label, run in (("merged", changed(merge_status="merged")), ("closed", changed(status="pr_closed")),
                ("unapproved", open_setup_run("changed", approval_state="approval_required")),
                ("no pull request", changed(pr_number=None, pr_url="")),
                ("other repository", open_setup_run("changed", github_repo="fixture/other"))):
            with self.subTest(label):
                found, _ = self.lookup(run, open_setup_run("fallback"))
                self.assertEqual(found.run_id, "fallback")


class StaleVerifiedSourceTests(SimpleTestCase):
    """A connection verified before the customer's later commits keeps an older verified_sha."""

    def setUp(self):
        self.website = SimpleNamespace(id=changes.CONNECTION, pk=changes.CONNECTION, generation=2,
            repository_id=123, github_repo="example/site", state="connected", verified_sha=OLD,
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.website.targets.filter.return_value = []
        self.run = changes.static_setup_run()

    def base(self, *, observed, automatic=False):
        with patch.object(authority, "_source_observed", return_value=observed) as seen:
            return authority._verified_setup_run_base(self.run, changes.OLD, self.website,
                automatic_approval=automatic), seen

    def test_approved_setup_on_an_observed_newer_head_can_merge(self):
        base, seen = self.base(observed=True)
        self.assertEqual(base, BASE)
        seen.assert_called_once_with(self.website, BASE)

    def test_unobserved_source_is_still_refused(self):
        self.assertEqual(self.base(observed=False)[0], "")

    def test_automatic_approval_still_requires_the_verified_source(self):
        self.run.status, self.run.approval_state = "awaiting_approval", "approval_required"
        self.run.result["status"] = self.run.result["article_system_setup"]["status"] = "preview_ready"
        self.run.result["directory_quality_gates"]["local_build_skipped"] = False
        self.assertEqual(self.base(observed=True, automatic=True)[0], "")
        self.website.verified_sha = BASE
        self.assertEqual(self.base(observed=False, automatic=True)[0], BASE)

    def test_observation_is_a_current_generation_snapshot_of_that_exact_head(self):
        self.assertFalse(authority._source_observed(self.website, BASE))
        connection = authority.WebsiteConnection(pk=changes.CONNECTION, generation=2)
        with patch.object(authority.WebsiteScanSnapshot.objects, "filter") as snapshots:
            snapshots.return_value.exists.return_value = True
            self.assertTrue(authority._source_observed(connection, BASE))
        self.assertEqual(snapshots.call_args.kwargs, {"connection_id": connection.pk, "generation": 2, "source_sha": BASE})


class PrepareReturnsToOpenPullRequestTests(SimpleTestCase):
    def setUp(self):
        self.replay = replay.PrepareJourneyReplayTests()
        self.replay.setUp()
        self.op = self.replay.op
        self.config = self.replay.config
        self.config.requires_review = False
        self.config.save = Mock()
        self.config.article_system = {"pending_article_system_setup": {"mode": "existing", "routePath": "/stories",
            "setupRunId": "cancelled-retry", "setup_run_id": "cancelled-retry", "status": "running"}}
        self.run = open_setup_run()
        self.setup = Mock(return_value={"run_id": "new-setup"})

    def journey(self, *args, **kwargs):
        # Current inventory of BASE; the connection was last verified at OLD.
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": OLD},
            capabilities={"repositoryAccessVerified": True, "repositorySourceSha": BASE},
            discovery={"complete": True, "sourceSha": BASE, "resolution": self.replay.facts["resolution"]})

    def advance(self, *, merge=None, run=None):
        # The merge path's run guard returns refusals as an "error" outcome.
        merge = merge or Mock(return_value={"outcome": "error", "code": "setup_verification_required",
            "retryable": False, "detail": "Changed."})
        with ExitStack() as stack:
            stack.enter_context(patch.object(prepare, "_claim_prepare", return_value=self.op))
            stack.enter_context(patch.object(prepare, "_prepare_context",
                return_value=(self.replay.context, self.config, self.replay.user)))
            stack.enter_context(patch.object(prepare, "journey_for_context", side_effect=self.journey))
            stack.enter_context(patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kw: op))
            stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.filter",
                return_value=newest_first(run or self.run)))
            stack.enter_context(patch.object(views, "_attempt_setup_publish_merge", merge))
            prepare.advance_prepare(self.op.pk, handlers={"setup": self.setup})
        return merge

    def test_retry_returns_to_the_open_pull_request_instead_of_a_new_setup(self):
        merge = self.advance()
        self.setup.assert_not_called()
        self.assertEqual(merge.call_args.kwargs["automatic"], True)
        self.assertEqual(self.op.state, "needs_user")
        self.assertEqual(self.op.receipt["user_action"], {"id": "review_setup", "label": "Review and publish",
            "run_id": "setup-1", "pr_number": 40, "url": PR_URL})
        self.assertEqual(self.op.receipt["current_step"], "merge")
        self.assertIn("#40", self.op.receipt["message"])
        self.assertEqual((self.op.receipt["child_run_id"], self.op.receipt["setup_run_id"]), ("setup-1", "setup-1"))
        pending = self.config.article_system["pending_article_system_setup"]
        self.assertEqual((pending["setup_run_id"], pending["setupRunId"], pending["pr_number"], pending["status"]),
            ("setup-1", "setup-1", 40, "pr_created"))
        self.assertEqual((pending["mode"], pending["routePath"]), ("existing", "/stories"))
        self.config.save.assert_called_once_with(update_fields=["article_system", "updated_at"])

    def test_a_verified_unchanged_pull_request_still_merges_automatically(self):
        merged = Mock(return_value={"outcome": "merged"})
        self.advance(merge=merged)
        self.setup.assert_not_called()
        self.assertEqual(self.op.state, "pending")

    def test_required_review_happens_in_mlai_chat(self):
        self.config.requires_review = True
        merge = self.advance()
        merge.assert_not_called()
        self.assertEqual(self.op.receipt["user_action"]["id"], "review_setup")
        self.assertEqual(self.op.receipt["user_action"]["run_id"], "setup-1")

    def test_a_closed_pull_request_is_replaced_by_a_new_setup(self):
        self.advance(merge=Mock(return_value={"outcome": "closed"}))
        self.setup.assert_called_once()
        self.assertEqual(self.op.receipt["child_run_id"], "new-setup")

    def test_a_stale_pull_request_is_replaced_by_a_new_setup(self):
        merge = self.advance(run=open_setup_run(base=OLD))
        merge.assert_not_called()
        self.setup.assert_called_once()
        self.assertEqual(self.config.article_system["pending_article_system_setup"]["setup_run_id"], "cancelled-retry")

    def test_other_refusals_are_still_failures(self):
        self.advance(merge=Mock(return_value={"outcome": "error", "code": "website_source_changed",
            "retryable": False, "detail": "Moved."}))
        self.setup.assert_not_called()
        self.assertNotEqual(self.op.state, "needs_user")
        self.assertEqual(self.op.receipt["reason_code"], "website_source_changed")


class RealMergeRefusalTests(SimpleTestCase):
    """Through the real run guard, a changed head waits for the owner's Publish."""

    def test_a_changed_head_asks_the_owner_to_publish(self):
        run = changes.static_setup_run()
        config = SimpleNamespace(website_connection=SimpleNamespace(generation=2, github_repo="example/site"),
            github_repo="example/site", requires_review=False)
        refused = WebsiteAuthorityError("setup_verification_required", "Changed.")
        with patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext()), \
                patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "_pull_request_number_from_run", return_value=40), \
                patch.object(views, "_github_token_for_repo_operation", return_value=("token", "fixture")), \
                patch.object(views, "_github_pull_checks_state_lenient", return_value=(
                    {"head": {"sha": changes.NEW}, "merged": False, "state": "open"}, {"state": "success"})), \
                patch.object(authority, "validate_setup_merge_source", side_effect=[refused, refused]), \
                patch.object(authority, "verified_setup_head", return_value=changes.OLD), \
                patch.object(views, "_request_setup_pr_reverification") as reverify:
            result = prepare.maybe_merge_prepared_setup(run, SimpleNamespace(
                organization=SimpleNamespace(domain="example.test")), config, user=SimpleNamespace(pk=4))
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["user_action"]["id"], "review_setup")
        self.assertEqual(result["user_action"]["run_id"], "setup-1")
        reverify.assert_not_called()


class StopPreparingKeepsOpenPullRequestTests(SimpleTestCase):
    def test_stopping_preparation_never_cancels_the_open_setup_run(self):
        from uuid import uuid4
        fixture = replay.PrepareJourneyReplayTests()
        fixture.setUp()
        op, config = fixture.op, fixture.config
        op.pk, op.state = uuid4(), "needs_user"
        op.receipt = {"child_run_id": "setup-1", "setup_run_id": "setup-1"}
        op.payload = {**op.payload, "run_id": "setup-1"}
        op.connection.organization = SimpleNamespace(domain="fixture.test")
        op.connection.blockers = []
        op.connection.operations = Mock()
        op.connection.repository_mutations = Mock()
        op.connection.repository_mutations.filter.return_value.values.return_value = []
        op.connection.operations.select_for_update.return_value.filter.return_value.first.return_value = op
        op.connection.operations.filter.return_value.values_list.return_value = []
        config.organization, config.github_repo = op.connection.organization, op.connection.github_repo
        updates = []

        def runs(**kwargs):
            if kwargs.get("workflow") == "article_system_setup":
                return [open_setup_run()]
            rows = MagicMock()
            rows.values_list.return_value = ["queued-scan"]
            rows.first.return_value = None
            rows.update.side_effect = lambda **values: updates.append((kwargs, values))
            return rows
        with patch("content_factory.website_connections.authority_guard", return_value=nullcontext(op.connection)), \
                patch("workflow_runs.models.ContentFactoryRun.objects.filter", side_effect=runs), \
                patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create",
                    return_value=(Mock(), True)) as followup:
            operations.cancel_operation(config, data={"website_connection_id": str(uuid4()), "connection_generation": 3,
                "operation_id": str(op.pk)}, idempotency_key="stop")
        self.assertEqual(op.state, "cancelled")
        cancelled = [kwargs["run_id__in"] for kwargs, values in updates if values.get("status") == "cancelled"]
        self.assertEqual(cancelled, [["queued-scan"]])
        self.assertEqual(followup.call_args.kwargs["defaults"]["payload"]["cancel_run_ids"], ["queued-scan"])


class JourneyNextStepTests(SimpleTestCase):
    source_changed = {"repositoryAccessVerified": True, "repositorySourceSha": BASE,
        "reasonCode": "repository_source_changed", "reason": "The repository source changed. Scan and verify the current source."}

    def journey(self, *, capabilities=None, setup_pull_request=None):
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": OLD},
            capabilities=capabilities or self.source_changed, discovery={"complete": True, "sourceSha": BASE},
            setup_pull_request=setup_pull_request)

    def test_an_open_setup_pull_request_is_the_next_step(self):
        setup = {"runId": "setup-1", "prNumber": 40, "prUrl": PR_URL, "status": "pr_created", "savedSetup": True}
        journey = self.journey(setup_pull_request=setup)
        self.assertEqual(journey["nextAction"], {"id": "setup", "label": "Review setup changes", "step": "integration",
            "setupRunId": "setup-1"})
        self.assertEqual((journey["reasonCode"], journey["prerequisites"]["integration"]["reasonCode"]),
            ("setup_pr_unmerged", "setup_pr_unmerged"))
        self.assertIn("ready", journey["reason"])
        self.assertEqual(journey["setupPullRequest"], setup)

    def test_until_preparation_returns_to_it_the_label_leads_to_the_setup_page(self):
        # MLAI Chat links "Review setup changes" to the saved setup run, which a
        # cancelled retry still names; any other label opens the setup page.
        journey = self.journey(setup_pull_request={"runId": "setup-1", "savedSetup": False})
        self.assertEqual(journey["nextAction"]["label"], "Return to your setup pull request")

    def test_without_one_the_journey_is_unchanged(self):
        journey = self.journey()
        self.assertEqual(journey["nextAction"]["label"], self.source_changed["reason"])
        self.assertEqual(journey["reasonCode"], "repository_source_changed")
        self.assertIsNone(journey["setupPullRequest"])

    def test_missing_github_access_still_comes_first(self):
        journey = self.journey(capabilities={"repositoryAccessVerified": False, "accountAccessVerified": False},
            setup_pull_request={"runId": "setup-1"})
        self.assertNotEqual(journey["nextAction"]["label"], "Review setup changes")


class ClosedSetupPullRequestTests(SimpleTestCase):
    def test_a_closed_pull_request_is_marked_and_never_rechecked(self):
        run = changes.static_setup_run()
        config = SimpleNamespace(website_connection=SimpleNamespace(generation=2), github_repo="example/site")
        with patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "_pull_request_number_from_run", return_value=40), \
                patch.object(views, "_github_token_for_repo_operation", return_value=("token", "fixture")), \
                patch.object(views, "_github_pull_checks_state_lenient", return_value=(
                    {"head": {"sha": changes.NEW}, "merged": False, "state": "closed"}, {"state": "closed"})), \
                patch.object(views, "_mark_setup_merge_blocked") as blocked, \
                patch.object(authority, "validate_setup_merge_source") as guard, \
                patch.object(views, "_setup_pr_changes_outcome") as changed:
            outcome = views._attempt_setup_publish_merge_authorized.__wrapped__(
                run=run, context=SimpleNamespace(organization=SimpleNamespace(domain="example.test")))
        self.assertEqual(outcome["outcome"], "closed")
        self.assertEqual(blocked.call_args.kwargs["status_value"], "pr_closed")
        guard.assert_not_called()
        changed.assert_not_called()
        self.assertFalse(authority.setup_pr_open(SimpleNamespace(**{**vars(open_setup_run()),
            "result": {**open_setup_run().result, "status": "pr_closed"}})))
