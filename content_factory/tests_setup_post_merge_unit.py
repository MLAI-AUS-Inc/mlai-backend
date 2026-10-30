"""After a setup PR merges, preparation verifies the branch; it never sets up again.

Vyavos (14 Oct): the prepare worker merged setup PR #2 at 07:01 and PR #3 at
08:54 itself. Each time its next pass re-dispatched setup with the same request
identity (operation_key_conflict). The owner's Try again then ran a fresh
adoption that only re-stamped page.tsx with a new marker and merged another PR.
These run without a database or provider access.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase

from . import website_connections as authority
from . import website_prepare as prepare
from . import website_verification as verification
from .website_contract import WebsiteAuthorityError
from .website_journey import project_journey
from . import tests_prepare_journey_unit as replay

BASE, HEAD, OTHER = "a" * 40, "b" * 40, "c" * 40


KIT = [".mlai/articles-verification.json", ".mlai/articles-verification.py", ".github/workflows/mlai-articles-verification.yml"]


def setup_run(run_id="setup-3", *, merged=True, merge_sha=HEAD, adopted=False, pr=True, base=BASE, kit=True, **fields):
    result = {"status": "merged" if merged else "adopted", "merge_status": "merged" if merged else None,
        "merge_sha": merge_sha if merged else None, "adopted": adopted, "adoption": {"kit_paths": KIT if kit else []},
        "article_system_setup": {"status": "merged" if merged else "adopted", "merge_status": "merged" if merged else None}}
    if pr:
        result.update(pr_number=3, pr_url="https://github.com/fixture/site/pull/3")
    run = SimpleNamespace(run_id=run_id, workflow="article_system_setup", status="completed", approval_state="approved",
        github_repo="fixture/site", run_request={"website_connection_id": "website", "connection_generation": 3,
            "expected_source_sha": base}, result=result)
    for key, value in fields.items():
        setattr(run, key, value)
    return run


def newest_first(*runs):
    query = MagicMock()
    query.order_by.return_value = list(runs)
    query.first.return_value = runs[0] if runs else None
    return query


class SettledSetupTests(SimpleTestCase):
    def test_a_setup_is_settled_at_its_own_merge_commit(self):
        self.assertTrue(authority.setup_settled_at(setup_run(), HEAD))
        self.assertFalse(authority.setup_settled_at(setup_run(), OTHER))
        run = setup_run(merge_sha=None)
        run.result["merge_response"] = {"sha": HEAD}
        self.assertTrue(authority.setup_settled_at(run, HEAD))

    def test_an_adopted_kit_without_a_pr_is_settled_at_its_source(self):
        self.assertTrue(authority.setup_settled_at(setup_run(merged=False, adopted=True, pr=False, base=HEAD), HEAD))
        self.assertFalse(authority.setup_settled_at(setup_run(merged=False, adopted=True, pr=True, base=HEAD), HEAD))
        self.assertFalse(authority.setup_settled_at(setup_run(merged=False, adopted=True, pr=False, base=BASE), HEAD))

    def test_unfinished_or_other_runs_are_not_settled(self):
        self.assertFalse(authority.setup_settled_at(setup_run(status="running"), HEAD))
        self.assertFalse(authority.setup_settled_at(setup_run(workflow="repo_scan"), HEAD))
        self.assertFalse(authority.setup_settled_at(setup_run(), ""))

    def test_lookup_is_the_current_generation_on_the_observed_head(self):
        connection = SimpleNamespace(pk="website", generation=3, organization_id=7, github_repo="Fixture/Site")
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(
                setup_run("elsewhere", github_repo="fixture/other"), setup_run("setup-3"))) as rows:
            self.assertEqual(authority.settled_setup_run(connection, source_sha=HEAD).run_id, "setup-3")
            self.assertIsNone(authority.settled_setup_run(connection, source_sha=OTHER))
        self.assertEqual(rows.call_args.kwargs["run_request__connection_generation"], 3)
        # TalaThrive PR #40 carried no kit (native CI needed configuration): the kit is next, not CI proof.
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(setup_run(kit=False))):
            self.assertIsNone(authority.settled_setup_run(connection, source_sha=HEAD))
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as rows:
            self.assertIsNone(authority.settled_setup_run(connection, source_sha=""))
        rows.assert_not_called()


class PostMergePrepareTests(SimpleTestCase):
    def setUp(self):
        self.replay = replay.PrepareJourneyReplayTests()
        self.replay.setUp()
        self.op = self.replay.op
        self.op.payload.update(attempt=10, retry_epoch="pre-merge")
        self.handlers = {name: Mock(return_value={"status": "completed"}) for name in ("setup", "ci-attestation", "scan")}
        self.head = HEAD

    def journey(self, *args, **kwargs):
        # Inventory of the merged head is current; the integration is not verified yet.
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": ""},
            capabilities={"repositoryAccessVerified": True, "repositorySourceSha": self.head},
            discovery={"complete": True, "sourceSha": self.head, "resolution": self.replay.facts["resolution"]})

    def advance(self, *, child=None, settled=(setup_run(),)):
        with patch.object(prepare, "_claim_prepare", return_value=self.op), \
                patch.object(prepare, "_prepare_context", return_value=(self.replay.context, self.replay.config, self.replay.user)), \
                patch.object(prepare, "journey_for_context", side_effect=self.journey), \
                patch.object(prepare, "_cleanup_retry_journey", side_effect=lambda op, context, config, journey: journey), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kw: op), \
                patch.object(prepare, "maybe_merge_prepared_setup", return_value={"status": "completed"}), \
                patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(*settled)) as rows:
            if child is not None:
                rows.return_value.first.return_value = child
            prepare.advance_prepare(self.op.pk, handlers=self.handlers)

    def test_the_workers_own_merge_renews_the_request_identity_once(self):
        merged = setup_run()
        self.op.receipt = {"child_run_id": merged.run_id, "setup_run_id": merged.run_id, "scan_run_id": "scan-9",
            "completed_steps": ["scan", "setup"]}
        self.advance(child=merged)
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(self.op.payload["attempt"], 11)
        self.assertNotEqual(self.op.payload["retry_epoch"], "pre-merge")
        self.assertTrue(self.op.payload["source_reverify"])
        self.assertEqual(self.op.receipt["merged_setup_run_id"], merged.run_id)
        for key in ("scan_run_id", "setup_run_id", "completed_steps"):
            self.assertNotIn(key, self.op.receipt)
        self.assertFalse(any(handler.called for handler in self.handlers.values()))
        self.advance(child=merged)
        self.assertEqual(self.op.payload["attempt"], 11)

    def test_after_the_merge_preparation_waits_for_ci_proof_instead_of_setting_up(self):
        self.op.receipt = {"merged_setup_run_id": "setup-3"}
        self.advance()
        self.handlers["setup"].assert_not_called()
        self.handlers["ci-attestation"].assert_called_once()

    def test_a_ci_wait_backs_off_without_a_new_setup(self):
        self.handlers["ci-attestation"].side_effect = WebsiteAuthorityError("ci_attestation_required",
            "Waiting for native CI proof at the observed source.", retryable=True)
        self.advance()
        self.handlers["setup"].assert_not_called()
        self.assertEqual(self.op.state, "pending")
        self.assertIsNotNone(self.op.next_attempt_at)

    def test_an_owner_retry_may_repair_a_settled_setup(self):
        self.op.payload["repair_setup"] = True
        self.advance()
        self.handlers["setup"].assert_called_once()
        self.handlers["ci-attestation"].assert_not_called()

    def test_a_merged_setup_without_the_kit_still_installs_it(self):
        self.advance(settled=(setup_run(kit=False),))
        self.handlers["setup"].assert_called_once()
        self.handlers["ci-attestation"].assert_not_called()

    def test_a_head_moved_past_the_merge_still_sets_up(self):
        self.head = OTHER
        self.advance()
        self.handlers["setup"].assert_called_once()

    def test_an_owner_retry_marks_a_repair_but_check_again_does_not(self):
        for state, action, repair in (("needs_attention", None, True), ("needs_user", {"id": "deploy_site"}, False)):
            with self.subTest(state=state):
                op = SimpleNamespace(pk="prepare", action="prepare", generation=3, state=state,
                    payload={"attempt": 4, "user_clicks": 1, "run_id": "old"}, receipt={"user_action": action, "child_run_id": "old"},
                    save=Mock())
                connection = SimpleNamespace(pk="website", generation=3, organization_id=7, organization=SimpleNamespace(domain="fixture.test"),
                    github_repo="fixture/site", operations=Mock())
                config = SimpleNamespace(website_connection=connection)
                with patch.object(prepare, "authority_guard", return_value=nullcontext(connection)), \
                        patch.object(prepare, "contract_for", return_value={}), \
                        patch("content_factory.website_goal.enrolled", return_value=False), \
                        patch("content_factory.website_goal_runner.owns_goal", return_value=False), \
                        patch.object(prepare, "_cancel_prepare_child"), \
                        patch.object(prepare.WebsiteConnectionOperation.objects, "filter", return_value=replay.FrozenPrepareContextTests.rows(first=None)), \
                        patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(op, False)):
                    prepare.start_prepare(config, company_id=9, user=SimpleNamespace(pk=4), retry=True)
                self.assertEqual(bool(op.payload.get("repair_setup")), repair)

    def test_the_post_merge_attempt_is_never_a_repair(self):
        self.op.payload["repair_setup"] = True
        prepare._begin_post_merge_attempt(self.op, setup_run())
        self.assertNotIn("repair_setup", self.op.payload)


class PostMergeSetupSurfaceTests(SimpleTestCase):
    """PawShare (23 Oct): setup ccd3ea22 merged without its kit. The follow-up setup asked
    the owner to choose the articles page again from /dist/blog, /articles/detail and tag pages."""

    def setUp(self):
        self.replay = replay.PrepareJourneyReplayTests()
        self.replay.setUp()
        self.replay.facts["resolution"] = {"existence_state": "ambiguous", "ranked_candidates": {"listing_surface_candidates": [
            {"route": "/dist/blog"}, {"route": "/articles/detail"}, {"route": "/dist/blog/tag/Melbourne"}]}}
        self.op = self.replay.op
        self.op.payload.update(attempt=15, retry_epoch="post-merge", source_reverify=True)
        self.op.receipt = {"merged_setup_run_id": "setup-3", "scan_run_id": "scan-9"}

    def merged(self, *, mode="existing", hint="/dist/blog", built="/blog", merged=True):
        run = setup_run(merged=merged)
        run.run_request.update(article_surface_mode=mode,
            article_surface_hint={"source": "user_input", "route_path": hint, "listing_url": hint} if hint else None)
        run.result["route_path"] = built
        return run

    def request(self, run):
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(run)) as rows:
            data = prepare._owner_request(self.op, self.replay.context, self.replay.config, "setup",
                user=self.replay.user, journey=self.replay.journey()).data
        return data, rows

    def test_the_kit_follow_up_keeps_the_merged_setups_page(self):
        data, rows = self.request(self.merged())
        self.assertEqual((data["article_surface_mode"], data["article_surface_url"]), ("existing", "/dist/blog"))
        self.assertEqual(data["source_scan_run_id"], "scan-9")
        self.assertEqual(rows.call_args.kwargs, {"run_id": "setup-3", "organization_id": 7, "workflow": "article_system_setup",
            "run_request__website_connection_id": "website", "run_request__connection_generation": 3})

    def test_a_page_the_merged_setup_created_is_now_existing(self):
        data, _ = self.request(self.merged(mode="none", hint="", built="/articles/"))
        self.assertEqual((data["article_surface_mode"], data["article_surface_url"]), ("existing", "/articles"))

    def test_an_unmerged_or_foreign_setup_leaves_the_choice_to_the_owner(self):
        for run in (self.merged(merged=False), None):
            with self.subTest(run=run), self.assertRaises(WebsiteAuthorityError) as raised:
                self.request(run)
            self.assertEqual(raised.exception.code, "articles_page_choice_required")

    def test_without_its_own_merge_the_rescan_still_decides(self):
        self.op.receipt.pop("merged_setup_run_id")
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as rows, \
                self.assertRaises(WebsiteAuthorityError) as raised:
            prepare._owner_request(self.op, self.replay.context, self.replay.config, "setup",
                user=self.replay.user, journey=self.replay.journey())
        self.assertEqual(raised.exception.code, "articles_page_choice_required")
        rows.assert_not_called()


class ClosedSetupReplacementTests(SimpleTestCase):
    """La Vie (23 Oct): its setup PR #30 was closed so a fresh setup could replace it. Try
    again on the waiting review kept attempt 30 and its retry epoch, so the replacement
    reused setup c12e1329's request key. Content Factory replayed c12e1329, which the
    backend then marked queued, and Prepare waited on it indefinitely."""

    def setUp(self):
        self.replay = replay.PrepareJourneyReplayTests()
        self.replay.setUp()
        self.op = self.replay.op
        self.op.payload.update(attempt=30, retry_epoch="review-wait")
        self.closed = setup_run("setup-30", merged=False, kit=False, status="queued")
        self.closed.result.update(merge_status="pr_closed", status="pr_closed")
        self.closed.result["article_system_setup"].update(merge_status="pr_closed")
        self.closed.run_request.update(article_surface_mode="existing",
            article_surface_hint={"source": "user_input", "route_path": "/articles", "listing_url": "/articles"})
        self.op.receipt = {"child_run_id": "setup-30", "setup_run_id": "setup-30", "current_step": "setup"}
        self.setup = Mock(return_value={"run_id": "setup-31"})

    def journey(self, *args, **kwargs):
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": ""},
            capabilities={"repositoryAccessVerified": True, "repositorySourceSha": HEAD},
            discovery={"complete": True, "sourceSha": HEAD, "resolution": self.replay.facts["resolution"]})

    def advance(self):
        with patch.object(prepare, "_claim_prepare", return_value=self.op), \
                patch.object(prepare, "_prepare_context", return_value=(self.replay.context, self.replay.config, self.replay.user)), \
                patch.object(prepare, "journey_for_context", side_effect=self.journey), \
                patch.object(prepare, "_cleanup_retry_journey", side_effect=lambda op, context, config, journey: journey), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kw: op), \
                patch.object(prepare, "_setup_owner_action", return_value=None), \
                patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(self.closed)):
            prepare.advance_prepare(self.op.pk, handlers={"setup": self.setup})

    def test_a_closed_setup_is_replaced_under_a_new_request_identity(self):
        self.advance()
        self.setup.assert_called_once()
        self.assertEqual(self.op.payload["attempt"], 31)
        self.assertNotEqual(self.op.payload["retry_epoch"], "review-wait")
        self.assertEqual(self.op.receipt["superseded_setup_run_id"], "setup-30")
        self.assertEqual(self.op.receipt["child_run_id"], "setup-31")
        self.assertEqual(self.op.state, "pending")

    def test_the_identity_rotates_once_per_closed_setup(self):
        self.op.receipt["superseded_setup_run_id"] = "setup-30"
        self.advance()
        self.assertEqual((self.op.payload["attempt"], self.op.payload["retry_epoch"]), (30, "review-wait"))

    def test_the_replacement_keeps_the_owners_existing_page(self):
        self.replay.facts["resolution"] = {"existence_state": "ambiguous", "ranked_candidates": {"listing_surface_candidates": [
            {"route": "/articles"}, {"route": "/journal"}]}}
        self.op.receipt = {"superseded_setup_run_id": "setup-30"}
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=newest_first(self.closed)):
            data = prepare._owner_request(self.op, self.replay.context, self.replay.config, "setup",
                user=self.replay.user, journey=self.journey()).data
        self.assertEqual((data["article_surface_mode"], data["article_surface_url"]), ("existing", "/articles"))


class OwnedSquashMergeAttestationTests(SimpleTestCase):
    """A squash merge never contains the reviewed head; the platform's own merge record is the lineage."""

    def attest(self, *, reviewed_heads, owned, lineage=None):
        target = SimpleNamespace(contract={"contract_digest": "digest"})
        website = SimpleNamespace(pk="website", generation=3, repository_id=123, github_repo="fixture/site", app_root="",
            branch="main", organization=SimpleNamespace(domain="fixture.test"), verified_sha="",
            targets=Mock(), operations=Mock(), repository_mutations=Mock())
        website.targets.filter.return_value.first.return_value = target
        website.operations.filter.return_value.first.return_value = None
        website.repository_mutations.filter.return_value.exclude.return_value.values_list.return_value = reviewed_heads
        reached = WebsiteAuthorityError("reached_ci_proof", "Lineage accepted.")
        with patch.object(verification, "authority_guard", return_value=nullcontext(website)), \
                patch.object(verification, "contract_for", return_value={}), \
                patch.object(authority, "verify_repository_head", return_value=HEAD), \
                patch.object(verification, "_setup_merged_at", return_value=owned) as merged, \
                patch.object(verification, "verify_reviewed_source_lineage", side_effect=lineage) as compare, \
                patch.object(verification, "read_ci_proof", side_effect=reached):
            with self.assertRaises(WebsiteAuthorityError) as raised:
                verification.record_ci_attestation({"source_sha": HEAD, "run_id": "setup-3", "target_id": "native",
                    "contract_digest": "digest"})
        return raised.exception.code, merged, compare

    def test_the_platforms_own_squash_merge_is_accepted(self):
        code, merged, compare = self.attest(reviewed_heads=[], owned=True)
        self.assertEqual(code, "reached_ci_proof")
        self.assertEqual(merged.call_args.args[1:], ("setup-3", HEAD))
        compare.assert_not_called()

    def test_a_diverged_reviewed_head_falls_back_to_the_owned_merge(self):
        refused = WebsiteAuthorityError("website_source_review_required", "Diverged.")
        self.assertEqual(self.attest(reviewed_heads=[OTHER], owned=True, lineage=refused)[0], "reached_ci_proof")

    def test_an_unowned_change_is_still_refused(self):
        self.assertEqual(self.attest(reviewed_heads=[], owned=False)[0], "website_source_review_required")
        refused = WebsiteAuthorityError("website_source_review_required", "Diverged.")
        self.assertEqual(self.attest(reviewed_heads=[OTHER], owned=False, lineage=refused)[0], "website_source_review_required")

    def test_a_reviewed_lineage_needs_no_merge_lookup(self):
        code, merged, compare = self.attest(reviewed_heads=[OTHER], owned=False)
        self.assertEqual(code, "reached_ci_proof")
        merged.assert_not_called()
