"""The owner merging a verified setup pull request on GitHub, not with Publish.

Stopping preparation keeps an approved setup's pull request open to be merged.
Merged on GitHub, it left no merge intent, so the push counted as an external
change: publishing turned off, a "repository changed" blocker and a forced
rescan were added, and a stopped preparation stayed stopped. GitHub's record of
that exact pull request, merged at its previewed head with its previewed tree,
now proves the push. These run without a database against a read-only GitHub
stub.
"""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_connections as authority
from . import website_prepare as prepare
from . import website_reconciliation as reconciliation
from . import website_verification as verification
from .website_contract import WebsiteAuthorityError

BASE, MERGED, HEAD, TREE = "a" * 40, "b" * 40, "c" * 40, "d" * 40
API = "https://api.github.com/repos/synthetic/site/"


def verified_setup_result(run_id="owner-setup", number=40, head=HEAD):
    """An approved setup whose open pull request previewed head exactly on BASE."""
    return {"status": "setup_pr_created", "pr_number": number, "pr_url": f"https://github.com/synthetic/site/pull/{number}",
        "source_sha": BASE, "branch_commit_sha": head, "resume_generation": 1,
        "article_system_setup": {"status": "pr_created", "setup_run_id": run_id, "source_sha": BASE,
            "branch_commit_sha": head, "resume_generation": 1},
        "live_preview": {"exactRender": True, "commitSha": head},
        "directory_quality_gates": {"status": "passed", "local_build_skipped": False, **{key: True for key in (
            "dependency_validation", "static_policy", "local_build", "dom_slot_compliance", "publish_surface",
            "browser", "visual_style")}},
        "directory_quality_verification": {"status": "passed", "commit_sha": head, "preview_identity": f"commit:{head}",
            "resume_generation": 1, "task_id": "verification-1", "completed_at": "2026-10-13T07:35:00+00:00"}}


def lookup(run, key):
    """Resolve a Django lookup such as result__merge_status or github_repo__iexact."""
    *path, last = key.split("__")
    folded = last == "iexact"
    if not folded:
        path.append(last)
    value = getattr(run, path[0], None)
    for part in path[1:]:
        value = value.get(part) if isinstance(value, dict) else None
    return value, folded


class Runs(list):
    """The ContentFactoryRun queries _find_owned_merge makes, over in-memory rows."""

    def filter(self, **lookups):
        def matches(run, key, expected):
            value, folded = lookup(run, key)
            return str(value).casefold() == str(expected).casefold() if folded else value == expected
        return Runs(run for run in self if all(matches(run, key, value) for key, value in lookups.items()))

    def order_by(self, *fields):
        return self

    def first(self):
        return self[0] if self else None


class OwnerSetupMergeFixture(SimpleTestCase):
    def setUp(self):
        self.website = SimpleNamespace(pk=uuid4(), organization_id=4, generation=3, repository_id=42,
            github_repo="synthetic/site", branch="main", app_root="", state="connected", installation_id="45",
            verified_sha=BASE, configuration_version=5, organization=SimpleNamespace(domain="synthetic.test"),
            targets=Mock(), capabilities={"publishingReady": True, "previewSupported": True}, blockers=[], save=Mock())
        self.config = SimpleNamespace(website_connection=self.website, organization_id=4, default_publish_target_id="featured")
        self.setup = SimpleNamespace(pk=1, run_id="owner-setup", organization_id=4, domain="synthetic.test",
            github_repo="synthetic/site", workflow="article_system_setup", status="completed", approval_state="approved",
            run_request={**authority.contract_for(self.website), "expected_source_sha": BASE},
            result=verified_setup_result())
        self.runs = [self.setup]
        repo = {"id": 42, "full_name": "synthetic/site"}
        # GitHub's "Squash and merge": one new commit on BASE whose tree is HEAD's.
        self.pull = {"number": 40, "state": "closed", "merged": True, "merge_commit_sha": MERGED,
            "head": {"sha": HEAD, "ref": "mlai/articles-setup", "repo": dict(repo)},
            "base": {"sha": BASE, "ref": "main", "repo": dict(repo)}}
        self.commits = {MERGED: {"sha": MERGED, "tree": {"sha": TREE}, "parents": [{"sha": BASE}]},
            HEAD: {"sha": HEAD, "tree": {"sha": TREE}, "parents": [{"sha": BASE}]}}
        self.other_pulls, self.unavailable, self.reads = {}, set(), []

    def github_get(self, url, **kwargs):
        path = url.removeprefix(API)
        self.reads.append(path)
        if path.startswith("pulls/"):
            payload = self.pull if path == "pulls/40" else self.other_pulls.get(path)
        else:
            payload = self.commits.get(path.removeprefix("git/commits/"))
        if path in self.unavailable or payload is None:
            def refuse():
                raise reconciliation_http_error(path)
            return SimpleNamespace(raise_for_status=refuse, json=lambda: {"message": "Not Found"})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: deepcopy(payload))

    def github(self, stack):
        """Read-only synthetic GitHub; any repository write fails the test."""
        from integrations.services import github_app
        from workflow_runs.models import ContentFactoryRun
        stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", side_effect=lambda **kw: Runs(self.runs).filter(**kw)))
        stack.enter_context(patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext(self.website)))
        self.mint = stack.enter_context(patch.object(github_app, "create_installation_access_token",
            return_value=SimpleNamespace(token="synthetic-read-token")))
        stack.enter_context(patch("integrations.http_client.get", side_effect=self.github_get))
        self.revoke = stack.enter_context(patch("integrations.http_client.delete"))
        for method in ("post", "put", "patch", "request"):
            stack.enter_context(patch(f"integrations.http_client.{method}",
                side_effect=AssertionError("Recognizing a merge only reads GitHub.")))
        self.log = stack.enter_context(patch.object(reconciliation, "logger"))

    def find(self, source_sha=MERGED):
        with ExitStack() as stack:
            self.github(stack)
            return reconciliation._find_owned_merge(self.website, 4, source_sha)

    def push(self, source_sha=MERGED):
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()))
            stack.enter_context(patch("organizations.models.Organization.objects.select_for_update"))
            connections = stack.enter_context(patch.object(reconciliation.WebsiteConnection.objects, "select_for_update"))
            connections.return_value.get.return_value = self.website
            configs = stack.enter_context(patch.object(reconciliation.OrganizationContentConfig.objects, "filter"))
            configs.return_value.select_related.return_value = [self.config]
            stack.enter_context(patch.object(reconciliation.WebsiteScanSnapshot.objects, "get_or_create", return_value=(object(), True)))
            queued = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create"))
            wake = stack.enter_context(patch.object(prepare, "wake_prepare_for_source"))
            reconciliation.handle_website_github_event("push", {"repository": {"id": 42}, "ref": "refs/heads/main", "after": source_sha})
        return queued.call_args.kwargs["defaults"]["payload"], wake

    def assertExternal(self, found):
        self.assertIsNone(found)
        if self.mint.called:
            self.revoke.assert_called_once()
        self.assertNotIn("synthetic-read-token", repr(self.log.mock_calls))

    def logged(self, level):
        return [entry.args[0].split()[0] for entry in getattr(self.log, level).call_args_list]


def reconciliation_http_error(path):
    from integrations import http_client
    return http_client.HTTPError(f"GitHub refused {path}")


class OwnerSetupMergeWebhookTests(OwnerSetupMergeFixture):
    def test_owner_merge_keeps_publishing_and_reopens_preparation_without_a_rescan(self):
        payload, wake = self.push()
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("owner-setup", False))
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertEqual(self.website.blockers, [])
        # The stopped preparation reopens for an owned current-generation setup.
        wake.assert_called_once_with(self.website.pk, organization_id=4, owned_merge_run_id="owner-setup")
        self.assertEqual(self.reads, ["pulls/40", f"git/commits/{MERGED}", f"git/commits/{HEAD}"])
        self.assertEqual(self.mint.call_args.kwargs["permission_mode"], "read")
        self.assertEqual(self.revoke.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-read-token")
        self.assertEqual(self.logged("info"), ["website_owner_setup_merge_matched"])
        self.assertNotIn("synthetic-read-token", repr(self.log.mock_calls))

    def test_customer_push_while_setup_pull_request_is_open_stays_external(self):
        self.pull.update(state="open", merged=False, merge_commit_sha="e" * 40)
        payload, wake = self.push()
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("", True))
        self.assertFalse(self.website.capabilities["publishingReady"])
        self.assertEqual([item["code"] for item in self.website.blockers], ["repository_source_changed"])
        wake.assert_called_once_with(self.website.pk, organization_id=4, owned_merge_run_id="")
        # One pull request read and no commit reads or logs for an ordinary push.
        self.assertEqual(self.reads, ["pulls/40"])
        self.revoke.assert_called_once()
        self.assertEqual(self.log.mock_calls, [])


class OwnerSetupMergeEvidenceTests(OwnerSetupMergeFixture):
    def test_every_github_merge_method_with_the_previewed_tree_is_owned(self):
        cases = {
            "squash": (MERGED, [BASE]),
            "merge commit": (MERGED, [BASE, HEAD]),
            # Rebasing several commits: the last rewritten commit's parent is not BASE.
            "rebase": (MERGED, ["e" * 40]),
            "fast-forward": (HEAD, [BASE]),
        }
        for method, (source_sha, parents) in cases.items():
            with self.subTest(method=method):
                self.pull["merge_commit_sha"] = source_sha
                self.commits[source_sha] = {"sha": source_sha, "tree": {"sha": TREE}, "parents": [{"sha": sha} for sha in parents]}
                found = self.find(source_sha)
                self.assertEqual(found["run_id"], "owner-setup")
                self.assertEqual(found["connection_identity"], reconciliation._merge_connection_identity(self.website))
                self.revoke.assert_called_once()

    def test_merge_with_changes_that_were_never_previewed_stays_external(self):
        # The base moved before the merge, or the merge commit was edited.
        self.commits[MERGED]["tree"]["sha"] = "f" * 40
        self.assertExternal(self.find())
        self.assertEqual(self.reads, ["pulls/40", f"git/commits/{MERGED}", f"git/commits/{HEAD}"])
        self.assertEqual(self.logged("info"), ["website_owner_setup_merge_changed"])

    def test_pull_request_that_is_not_this_exact_merge_stays_external(self):
        changes = {
            "not merged": lambda pull: pull.update(merged=False),
            "another merge commit": lambda pull: pull.update(merge_commit_sha="e" * 40),
            "another pull request": lambda pull: pull.update(number=41),
            "head pushed after preview": lambda pull: pull["head"].update(sha="e" * 40),
            "another base branch": lambda pull: pull["base"].update(ref="staging"),
            "fork head": lambda pull: pull["head"]["repo"].update(id=999),
            "renamed head repository": lambda pull: pull["head"]["repo"].update(full_name="someone/site"),
            "another base repository": lambda pull: pull["base"]["repo"].update(id=999),
        }
        valid = deepcopy(self.pull)
        for name, change in changes.items():
            with self.subTest(change=name):
                self.pull, self.reads = deepcopy(valid), []
                change(self.pull)
                self.assertExternal(self.find())
                self.assertEqual(self.reads, ["pulls/40"])
                self.assertEqual(self.log.mock_calls, [])
        self.pull = deepcopy(valid)
        self.assertEqual(self.find()["run_id"], "owner-setup")

    def test_unavailable_or_inconsistent_github_evidence_fails_closed(self):
        failures = {
            "pull request unavailable": lambda: self.unavailable.add("pulls/40"),
            "merge commit unavailable": lambda: self.unavailable.add(f"git/commits/{MERGED}"),
            "previewed commit unavailable": lambda: self.unavailable.add(f"git/commits/{HEAD}"),
            "another commit returned": lambda: self.commits[MERGED].update(sha="e" * 40),
            "malformed tree": lambda: self.commits[HEAD].update(tree={"sha": "not-a-tree"}),
            "missing tree": lambda: self.commits[MERGED].pop("tree"),
            "pull request not an object": lambda: setattr(self, "pull", ["merged"]),
        }
        commits, pull = deepcopy(self.commits), deepcopy(self.pull)
        for name, fail in failures.items():
            with self.subTest(failure=name):
                self.commits, self.pull, self.unavailable = deepcopy(commits), deepcopy(pull), set()
                fail()
                self.assertExternal(self.find())
                expected = [] if name == "pull request not an object" else ["website_owner_setup_merge_unverified"]
                self.assertEqual(self.logged("warning"), expected)
        self.commits, self.pull, self.unavailable = deepcopy(commits), deepcopy(pull), set()
        self.assertEqual(self.find()["run_id"], "owner-setup")

    def test_token_failure_and_revoked_authority_fail_closed(self):
        from integrations.services import github_app
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(github_app, "create_installation_access_token", side_effect=RuntimeError("GitHub App unavailable")))
            self.assertIsNone(reconciliation._find_owned_merge(self.website, 4, MERGED))
        self.assertEqual(self.reads, [])
        self.assertEqual(self.logged("warning"), ["website_owner_setup_merge_unavailable"])
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(authority, "authority_guard", side_effect=WebsiteAuthorityError("website_disconnected", "Disconnected.")))
            self.assertIsNone(reconciliation._find_owned_merge(self.website, 4, MERGED))
        self.mint.assert_not_called()
        self.assertEqual(self.logged("warning"), ["website_owner_setup_merge_unavailable"])

    def test_rebind_after_github_read_cannot_preserve_readiness(self):
        guards = 0

        def guard(*args, **kwargs):
            nonlocal guards
            guards += 1
            if guards == 2:
                self.website.configuration_version += 1
            return nullcontext(self.website)
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(authority, "authority_guard", side_effect=guard))
            self.assertIsNone(reconciliation._find_owned_merge(self.website, 4, MERGED))
        self.assertEqual(guards, 2)
        self.revoke.assert_called_once()


class OwnerSetupMergeEligibilityTests(OwnerSetupMergeFixture):
    """Only a current, approved, exactly previewed setup with an open PR is read."""

    def test_setups_that_cannot_vouch_for_a_merge_make_no_github_reads(self):
        def result(**changes):
            return lambda run: run.result.update(changes)

        def setup(**changes):
            return lambda run: run.result["article_system_setup"].update(changes)
        changes = {
            "not approved": lambda run: setattr(run, "approval_state", "approval_required"),
            "still running": lambda run: setattr(run, "status", "running"),
            "not a setup": lambda run: setattr(run, "workflow", "publish_article"),
            "previous generation": lambda run: run.run_request.update(connection_generation=2),
            "another connection": lambda run: run.run_request.update(website_connection_id=str(uuid4())),
            "another repository": lambda run: run.run_request.update(repository_id=43),
            "repository identity missing": lambda run: run.run_request.pop("repository_id"),
            "another repository name": lambda run: setattr(run, "github_repo", "someone/site"),
            "no pull request": lambda run: [item.pop(key, None) for item in (run.result, run.result["article_system_setup"])
                for key in ("pr_number", "pr_url")],
            "pull request closed": result(status="pr_closed"),
            "merge recorded without its commit": result(merge_status="merged"),
            "preview not exact": lambda run: run.result["live_preview"].update(exactRender=False),
            "quality gate failed": lambda run: run.result["directory_quality_gates"].update(browser=False),
            "proof for another head": setup(branch_commit_sha="e" * 40),
            "head not a commit": result(branch_commit_sha="main"),
            "base no longer current": lambda run: setattr(self.website, "verified_sha", "e" * 40),
        }
        valid_request, valid_result = deepcopy(self.setup.run_request), deepcopy(self.setup.result)

        def reset():
            self.setup.run_request, self.setup.result = deepcopy(valid_request), deepcopy(valid_result)
            self.setup.workflow, self.setup.status, self.setup.approval_state = "article_system_setup", "completed", "approved"
            self.setup.github_repo, self.website.verified_sha, self.reads = "synthetic/site", BASE, []
        for name, change in changes.items():
            with self.subTest(change=name):
                reset()
                change(self.setup)
                with patch.object(authority, "_source_observed", return_value=False):
                    self.assertIsNone(self.find())
                self.mint.assert_not_called()
                self.assertEqual(self.reads, [])
        reset()
        self.assertEqual(self.find()["run_id"], "owner-setup")

    def test_newer_branch_head_observed_this_generation_still_vouches(self):
        # A setup approved on a pushed head that was observed but not yet verified.
        self.website.verified_sha = "e" * 40
        with patch.object(authority, "_source_observed", return_value=True) as observed:
            self.assertEqual(self.find()["run_id"], "owner-setup")
        observed.assert_called_once_with(self.website, BASE)

    def test_unreadable_saved_setup_is_skipped_without_failing_the_push(self):
        broken = SimpleNamespace(**{**vars(self.setup), "run_id": "broken-setup",
            "run_request": deepcopy(self.setup.run_request), "result": {**verified_setup_result(), "status": {"unexpected": True}}})
        self.runs = [broken, self.setup]
        self.assertEqual(self.find()["run_id"], "owner-setup")
        self.assertEqual(self.logged("warning"), ["website_owner_setup_merge_unreadable"])
        # Alone, it leaves an ordinary external push: handled, not failed.
        self.runs, self.reads = [broken], []
        payload, _ = self.push()
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("", True))
        self.assertEqual(self.reads, [])
        self.mint.assert_not_called()

    def test_another_open_setup_pull_request_does_not_hide_the_merged_one(self):
        other = SimpleNamespace(**{**vars(self.setup), "run_id": "other-setup",
            "run_request": deepcopy(self.setup.run_request), "result": verified_setup_result("other-setup", 39, "e" * 40)})
        self.runs = [other, self.setup]
        self.other_pulls["pulls/39"] = {**deepcopy(self.pull), "number": 39, "state": "open", "merged": False,
            "merge_commit_sha": "f" * 40, "head": {**deepcopy(self.pull["head"]), "sha": "e" * 40}}
        for available in (True, False):
            with self.subTest(other_pull_request_readable=available):
                self.unavailable, self.reads = set() if available else {"pulls/39"}, []
                self.assertEqual(self.find()["run_id"], "owner-setup")
                self.assertEqual(self.reads, ["pulls/39", "pulls/40", f"git/commits/{MERGED}", f"git/commits/{HEAD}"])
                self.revoke.assert_called_once()
                self.assertEqual(self.logged("warning"), [] if available else ["website_owner_setup_merge_unverified"])


class OwnerSetupMergeRecheckTests(OwnerSetupMergeFixture):
    def test_late_recheck_skips_the_rescan_for_an_owner_merge(self):
        # GitHub was unavailable at the webhook; the source check proves the merge later.
        self.website.targets.filter.return_value.first.return_value = SimpleNamespace(target_key="featured")
        now = timezone.now()
        op = SimpleNamespace(pk=uuid4(), connection=self.website, generation=3, attempts=0, next_attempt_at=None,
            payload={"source_sha": MERGED, "target_id": "featured", "owned_merge_run_id": "", "scan_required": True},
            receipt={"status": "verification_queued"}, save=Mock())
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()))
            claimed = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update"))
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            stored = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter"))
            stored.return_value.update.return_value = 1
            stack.enter_context(patch.object(verification, "discover_source_attestation",
                side_effect=WebsiteAuthorityError("ci_attestation_required", "Waiting for CI proof.", retryable=True)))
            outcome = reconciliation._process_source_reverification(op.pk, now + timedelta(seconds=1))
        self.assertEqual(outcome, "pending")
        recorded = stored.return_value.update.call_args_list[0].kwargs["payload"]
        self.assertEqual((recorded["owned_merge_run_id"], recorded["scan_required"]), ("owner-setup", False))
        final = stored.return_value.update.call_args.kwargs["receipt"]
        self.assertNotIn("scan_run_id", final)
        self.assertEqual(final["code"], "ci_attestation_required")
