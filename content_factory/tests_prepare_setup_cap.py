"""La Vie, 16 Nov 2026: one preparation kept dispatching setups and left MLAI's older PR open.

R2: a preparation operation dispatches at most ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT
setup runs across all its attempts, then waits for staff with an alert.
R3: once preparation holds a newer setup pull request, MLAI's own older open
setup pull requests of the same connection generation are closed.
These run without a database or provider access.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import founder_stuck_alerts as stuck
from . import tests_prepare_journey_unit as replay
from . import website_prepare as prepare
from . import website_prepare_setup_cap as setup_cap
from . import website_superseded_setup_prs as superseded

NOW = timezone.now()


class SetupDispatchCountTests(SimpleTestCase):
    def op(self, runs=None):
        receipt = {} if runs is None else {setup_cap.RECEIPT_KEY: list(runs)}
        return SimpleNamespace(pk="prepare", receipt=receipt)

    def test_each_setup_run_counts_once(self):
        op = self.op()
        setup_cap.record(op, "setup-1")
        setup_cap.record(op, "setup-1")
        setup_cap.record(op, "")
        setup_cap.record(op, "setup-2")
        self.assertEqual(setup_cap.dispatched(op), ["setup-1", "setup-2"])

    def test_the_cap_is_reached_at_the_limit(self):
        self.assertFalse(setup_cap.reached(self.op(["a", "b"])))
        self.assertTrue(setup_cap.reached(self.op(["a", "b", "c"])))

    @override_settings(ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT=5)
    def test_the_limit_is_a_setting(self):
        self.assertFalse(setup_cap.reached(self.op(["a", "b", "c", "d"])))
        self.assertTrue(setup_cap.reached(self.op(["a", "b", "c", "d", "e"])))

    @override_settings(ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT=0)
    def test_zero_switches_the_cap_off(self):
        self.assertFalse(setup_cap.reached(self.op([str(index) for index in range(20)])))

    def test_a_new_preparation_starts_at_zero(self):
        self.assertEqual(setup_cap.dispatched(self.op()), [])
        self.assertFalse(setup_cap.reached(self.op()))

    def test_retries_merges_and_replacements_keep_the_count(self):
        op = replay.PrepareJourneyReplayTests()
        op.setUp()
        op = op.op
        op.receipt = {setup_cap.RECEIPT_KEY: ["a", "b"], "setup_run_id": "b", "child_run_id": "b"}
        op.payload.update(attempt=2, repair_setup=True)
        values = prepare._source_recheck_values(op, message="Rechecking.")
        self.assertEqual(values["receipt"][setup_cap.RECEIPT_KEY], ["a", "b"])
        with patch("content_factory.website_hosted_setup_lineage.retain_post_merge_inputs"):
            prepare._begin_post_merge_attempt(op, SimpleNamespace(run_id="b"))
        self.assertEqual(op.receipt[setup_cap.RECEIPT_KEY], ["a", "b"])
        with patch("content_factory.website_hosted_setup_lineage.discard_for_new_setup"):
            prepare._begin_replacement_setup_attempt(op, SimpleNamespace(run_id="c"))
        self.assertEqual(op.receipt[setup_cap.RECEIPT_KEY], ["a", "b"])


class SetupDispatchCapPrepareTests(SimpleTestCase):
    def setUp(self):
        self.replay = replay.PrepareJourneyReplayTests()
        self.replay.setUp()
        self.op = self.replay.op

    def advance(self, setup):
        with patch.object(stuck, "queue_operation_stopped_alert", return_value=1) as alert:
            self.replay.advance({"setup": setup})
        return alert

    def test_repeated_owner_prepare_preserves_a_staff_hold_even_after_stop(self):
        from .website_contract import WebsiteAuthorityError
        from .website_operations import prepare_receipt
        connection = self.op.connection
        connection.blockers = []
        connection.operations = Mock()
        connection.operations.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = self.op
        self.op.receipt = {setup_cap.HELD_KEY: {"limit": 3}, setup_cap.RECEIPT_KEY: ["s1", "s2", "s3"]}
        self.op.payload["attempt"] = 4
        for state in ("needs_attention", "cancelled"):
            for enrolled in (False, True):
                self.op.state = state
                self.assertEqual(prepare_receipt(self.op)["owner_can_retry"], state == "cancelled")
                with self.subTest(state=state, enrolled=enrolled), \
                        patch.object(prepare, "authority_guard", return_value=nullcontext(connection)), \
                        patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)), \
                        patch.object(prepare.WebsiteConnectionOperation.objects, "filter",
                                     return_value=replay.FrozenPrepareContextTests.rows(first=None)), \
                        patch("content_factory.website_goal.enrolled", return_value=enrolled), \
                        patch.object(prepare, "_cancel_prepare_child") as cancel:
                    if state == "cancelled":
                        self.assertIn("ask the MLAI team", prepare_receipt(self.op)["message"])
                        resumed = prepare.start_prepare(self.replay.config, expected={}, company_id=9, retry=True)
                        self.assertIs(resumed, self.op)
                        self.assertIsNone(resumed.next_attempt_at)
                    else:
                        with self.assertRaises(WebsiteAuthorityError) as raised:
                            prepare.start_prepare(self.replay.config, expected={}, company_id=9, retry=True)
                        self.assertEqual((raised.exception.status, raised.exception.code), (409, "website_prepare_blocked"))
                self.assertEqual((self.op.state, self.op.payload["attempt"]), ("needs_attention", 4))
                self.assertEqual(setup_cap.dispatched(self.op), ["s1", "s2", "s3"])
                cancel.assert_not_called()

    def test_owner_actions_distinguish_an_active_setup_from_a_saved_cap_stop(self):
        from .website_journey import project_journey
        website = {"status": "connected", "repositoryId": 123, "writePolicy": {"allowed": True}}
        for state, receipt, expected in (("pending", {}, True),
                ("needs_attention", {}, True), ("needs_attention", {setup_cap.HELD_KEY: {"limit": 3}}, False)):
            with self.subTest(state=state, receipt=receipt):
                result = project_journey(company_id=9, domain="fixture.test", website=website,
                    capabilities={"repositoryAccessVerified": True, "repositoryWriteVerified": True},
                    operation={"action": "prepare", "state": state, "receipt": receipt})
                for action in ("prepare", "cancel-operation"):
                    self.assertEqual(action in result["allowedActions"], expected)

    def test_setups_below_the_cap_are_dispatched_and_counted(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2"]}
        setup = Mock(return_value={"run_id": "setup-3"})
        alert = self.advance(setup)
        setup.assert_called_once()
        alert.assert_not_called()
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(setup_cap.dispatched(self.op), ["setup-1", "setup-2", "setup-3"])

    def test_the_cap_waits_for_staff_instead_of_dispatching_another_setup(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2", "setup-3"],
            "failure_occurrence_id": "stale", "failure_class": "repairable"}
        self.op.payload["repair_setup"] = True  # The owner's Try again counts too.
        setup = Mock(return_value={"run_id": "setup-4"})
        alert = self.advance(setup)
        setup.assert_not_called()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertEqual(self.op.receipt["reason_code"], "prepare_no_progress")
        self.assertEqual(self.op.receipt[setup_cap.HELD_KEY]["dispatched"], 3)
        self.assertEqual(self.op.receipt[setup_cap.HELD_KEY]["last_run_id"], "setup-3")
        self.assertIn("told the MLAI team", self.op.receipt["message"])
        self.assertNotIn("failure_occurrence_id", self.op.receipt)
        self.assertEqual(setup_cap.dispatched(self.op), ["setup-1", "setup-2", "setup-3"])
        alert.assert_called_once()
        self.assertEqual(alert.call_args.kwargs["code"], "prepare_no_progress")

    def test_an_alert_failure_never_fails_preparation(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2", "setup-3"]}
        setup = Mock(return_value={"run_id": "setup-4"})
        with patch.object(stuck, "queue_operation_stopped_alert", side_effect=RuntimeError("slack down")):
            self.replay.advance({"setup": setup})
        setup.assert_not_called()
        self.assertEqual(self.op.state, "needs_attention")

    def test_no_alert_when_the_stop_was_not_saved(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2", "setup-3"]}
        setup = Mock(return_value={"run_id": "setup-4"})
        with patch.object(stuck, "queue_operation_stopped_alert") as alert, \
                patch.object(prepare, "_finish_prepare", return_value=None):
            self.replay.advance({"setup": setup})
        setup.assert_not_called()
        alert.assert_not_called()

    @override_settings(ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT=0)
    def test_with_the_cap_off_setup_is_dispatched(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2", "setup-3"]}
        setup = Mock(return_value={"run_id": "setup-4"})
        self.advance(setup)
        setup.assert_called_once()
        self.assertEqual(self.op.state, "pending")

    def test_other_steps_are_not_capped(self):
        self.op.receipt = {setup_cap.RECEIPT_KEY: ["setup-1", "setup-2", "setup-3"]}
        self.replay.facts["inventory"] = False
        scan = Mock(return_value={"run_id": "scan-1"})
        with patch.object(stuck, "queue_operation_stopped_alert") as alert:
            self.replay.advance({"scan": scan})
        scan.assert_called_once()
        alert.assert_not_called()


class StoppedOperationAlertTests(SimpleTestCase):
    def op(self):
        return SimpleNamespace(pk=41, action="prepare", state="needs_attention", attempts=6,
            connection=SimpleNamespace(organization_id=7))

    def test_the_alert_uses_the_sweeps_own_slot(self):
        with patch.object(stuck, "_queue", return_value=1) as queue:
            self.assertEqual(stuck.queue_operation_stopped_alert(self.op(), code="prepare_no_progress", now=NOW), 1)
        [condition] = queue.call_args.args[0]
        self.assertEqual(condition.slot, "op-41-stopped")
        self.assertEqual(condition.kind, "operation_stopped")
        self.assertEqual(condition.code, "prepare_no_progress")
        self.assertEqual(condition.details["website_operation_id"], "41")
        self.assertEqual(queue.call_args.kwargs["organization_ids"], {7})

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False)
    def test_the_alert_kill_switch_is_honoured(self):
        with patch.object(stuck, "_queue") as queue:
            self.assertEqual(stuck.queue_operation_stopped_alert(self.op(), code="prepare_no_progress", now=NOW), 0)
        queue.assert_not_called()


def setup_run(run_id, number, *, created, generation=3, merge_status="not_merged", repo="fixture/site", branch=None):
    result = {"status": "pr_created", "merge_status": merge_status, "pr_number": number,
        "pr_url": f"https://github.com/{repo}/pull/{number}",
        "article_system_setup": {"status": "pr_created", "merge_status": merge_status}}
    if branch:
        result["head_branch"] = branch
    return SimpleNamespace(run_id=run_id, pk=run_id, workflow="article_system_setup", status="completed",
        github_repo=repo, domain="fixture.test", created_at=created, result=result, verification_summary={}, acceptance_summary={}, approval_state="approved",
        run_request={"website_connection_id": "website", "connection_generation": generation})


def pull(number, *, state="open", user="Bot", head_repo="fixture/site", ref=None, merged=False, base="main",
         base_repo="fixture/site"):
    return {"number": number, "state": state, "merged": merged, "user": {"type": user},
        "head": {"ref": ref or f"cf-setup/{number}", "repo": {"full_name": head_repo}},
        "base": {"ref": base, "repo": {"full_name": base_repo}}}


class SupersededSetupPullRequestTests(SimpleTestCase):
    def setUp(self):
        connection = SimpleNamespace(pk="website", generation=3, state="connected", organization_id=7,
            github_repo="fixture/site", branch="main", organization=SimpleNamespace(domain="fixture.test"))
        self.op = SimpleNamespace(pk="prepare", connection=connection, generation=3, receipt={})
        self.child = setup_run("setup-38", 38, created=NOW)
        self.older = setup_run("setup-37", 37, created=NOW - timedelta(hours=1))
        self.pulls = {37: pull(37)}
        self.calls = []
        self.current = True

    def github(self, method, path, *, token, body=None, expected=(200,)):
        self.calls.append((method, path, body))
        if method == "GET":
            number = int(path.rsplit("/", 1)[1])
            if isinstance(self.pulls.get(number), Exception):
                raise self.pulls[number]
            return self.pulls[number]
        return {}

    def close(self, candidates=None):
        with patch.object(superseded, "_candidates", return_value=[self.older] if candidates is None else candidates), \
                patch("content_factory.vibe_marketing_views._github_token_for_repo_operation",
                      return_value=("token", "github_app_installation")) as token, \
                patch("content_factory.vibe_marketing_views._github_api_request", side_effect=self.github), \
                patch("content_factory.run_provider_facts.save_provider_result") as save, \
                patch.object(superseded, "_still_current", return_value=self.current):
            summary = superseded.close_superseded_setup_prs(self.op, self.child)
        self.token, self.save = token, save
        return summary

    def methods(self):
        return [(method, path) for method, path, _ in self.calls]

    def test_mlai_closes_its_own_older_setup_pr_with_a_link_to_the_new_one(self):
        summary = self.close()
        self.assertEqual(summary, {"status": "completed", "closed": [37]})
        self.assertEqual(self.methods(), [("GET", "/repos/fixture/site/pulls/37"),
            ("POST", "/repos/fixture/site/issues/37/comments"), ("PATCH", "/repos/fixture/site/pulls/37")])
        self.assertIn("https://github.com/fixture/site/pull/38", self.calls[1][2]["body"])
        self.assertEqual(self.calls[2][2], {"state": "closed"})
        self.assertEqual(self.token.call_args.kwargs["permission_mode"], "write")
        saved = self.save.call_args.args[1]
        self.assertEqual(saved["merge_status"], "pr_closed")
        self.assertEqual(saved["status"], "pr_closed")
        self.assertEqual(saved["article_system_setup"]["merge_status"], "pr_closed")
        self.assertEqual(saved["superseded_by_run_id"], "setup-38")
        self.assertEqual(self.op.receipt[superseded.RECEIPT_KEY]["closed"], [37])
        self.assertTrue(self.op.receipt[superseded.RECEIPT_KEY]["done"])

    def test_one_pass_per_newer_pull_request(self):
        self.close()
        self.calls.clear()
        self.close()
        self.assertEqual(self.calls, [])

    def test_a_pull_request_a_person_opened_is_left_open(self):
        self.pulls[37] = pull(37, user="User")
        self.assertEqual(self.close()["closed"], [])
        self.assertEqual(self.methods(), [("GET", "/repos/fixture/site/pulls/37")])
        self.save.assert_not_called()

    def test_forks_default_branch_and_other_branches_are_left_open(self):
        for case in (pull(37, head_repo="someone/site"), pull(37, ref="main")):
            self.op.receipt, self.calls = {}, []
            self.pulls[37] = case
            self.assertEqual(self.close()["closed"], [])
            self.assertEqual(len(self.calls), 1)
        self.op.receipt, self.calls = {}, []
        self.older = setup_run("setup-37", 37, created=NOW - timedelta(hours=1), branch="cf-setup/recorded")
        self.pulls[37] = pull(37, ref="cf-setup/other")
        self.assertEqual(self.close()["closed"], [])

    def test_a_merged_pull_request_is_not_touched(self):
        self.pulls[37] = pull(37, merged=True, state="closed")
        self.assertEqual(self.close()["closed"], [])
        self.assertEqual(len(self.calls), 1)
        self.save.assert_not_called()

    def test_one_already_closed_on_github_is_only_recorded_as_closed(self):
        # An earlier pass closed it on GitHub but failed to record that locally.
        self.pulls[37] = pull(37, state="closed")
        self.assertEqual(self.close()["closed"], [37])
        self.assertEqual(self.methods(), [("GET", "/repos/fixture/site/pulls/37")])
        self.assertEqual(self.save.call_args.args[1]["merge_status"], "pr_closed")

    def test_a_pull_request_into_another_branch_or_repository_is_left_open(self):
        for case in (pull(37, base="staging"), pull(37, base_repo="someone/site")):
            self.op.receipt, self.calls = {}, []
            self.pulls[37] = case
            self.assertEqual(self.close()["closed"], [])
            self.assertEqual(len(self.calls), 1)
            self.save.assert_not_called()

    def test_no_github_write_once_website_access_changed(self):
        self.current = False
        self.assertEqual(self.close()["closed"], [])
        self.assertEqual(self.methods(), [("GET", "/repos/fixture/site/pulls/37")])
        self.save.assert_not_called()

    def test_the_newer_pull_request_itself_is_never_closed(self):
        self.assertEqual(self.close([setup_run("setup-dup", 38, created=NOW - timedelta(hours=2))])["closed"], [])
        self.assertEqual(self.calls, [])

    def test_a_github_error_is_logged_and_retried_a_bounded_number_of_times(self):
        self.pulls[37] = RuntimeError("GitHub returned 502.")
        for _ in range(superseded.MAX_PASSES):
            self.assertEqual(self.close()["status"], "partial")
        self.calls.clear()
        self.assertEqual(self.close()["status"], "done")
        self.assertEqual(self.calls, [])

    def test_unexpected_errors_never_raise(self):
        with patch.object(superseded, "_close", side_effect=RuntimeError("boom")):
            self.assertEqual(superseded.close_superseded_setup_prs(self.op, self.child), {"status": "error"})

    @override_settings(ARTICLES_CLOSE_SUPERSEDED_SETUP_PRS_ENABLED=False)
    def test_the_kill_switch_leaves_everything_open(self):
        self.assertEqual(self.close()["status"], "skipped")
        self.assertEqual(self.calls, [])

    def test_a_newer_run_without_its_own_pull_request_closes_nothing(self):
        self.child.result = {"status": "running", "article_system_setup": {}}
        with patch("content_factory.vibe_marketing_views._pull_request_number_from_run", return_value=None):
            self.assertEqual(self.close()["status"], "skipped")
        self.assertEqual(self.calls, [])

    def test_a_newer_run_of_another_generation_closes_nothing(self):
        self.child.run_request["connection_generation"] = 2
        self.assertEqual(self.close()["status"], "skipped")
        self.op.receipt = {}
        self.child.run_request["connection_generation"] = 3
        self.op.connection.state = "disconnected"
        self.assertEqual(self.close()["status"], "skipped")
        self.assertEqual(self.calls, [])

    def test_candidates_are_older_open_mlai_setups_of_this_generation(self):
        rows = [setup_run("old-gen", 30, created=NOW - timedelta(days=2), generation=2),
                setup_run("merged", 31, created=NOW - timedelta(days=1), merge_status="merged"),
                setup_run("closed", 32, created=NOW - timedelta(days=1), merge_status="pr_closed"),
                setup_run("other-repo", 33, created=NOW - timedelta(days=1), repo="fixture/other"),
                self.older]
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as query:
            query.return_value.exclude.return_value.order_by.return_value.__getitem__.return_value = rows
            found = superseded._candidates(self.op.connection, self.child)
        self.assertEqual([row.run_id for row in found], ["setup-37"])
        self.assertEqual(query.call_args.kwargs, {"organization_id": 7, "workflow": "article_system_setup",
            "created_at__lt": self.child.created_at})
        query.return_value.exclude.assert_called_once_with(run_id="setup-38")


class PrepareClosesSupersededSetupTests(SimpleTestCase):
    def test_preparation_offers_its_setup_child_for_superseding(self):
        fixture = replay.PrepareJourneyReplayTests()
        fixture.setUp()
        child = setup_run("setup-38", 38, created=NOW)
        child.status, child.approval_state = "running", None
        fixture.op.receipt = {"child_run_id": child.run_id, "setup_run_id": child.run_id}
        with patch.object(superseded, "close_superseded_setup_prs") as close:
            fixture.advance({}, child)
        close.assert_called_once_with(fixture.op, child)
