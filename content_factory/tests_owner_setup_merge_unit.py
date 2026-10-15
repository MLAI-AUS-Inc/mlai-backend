"""The owner merging a verified setup pull request on GitHub, not with Publish.

Stopping preparation keeps an approved setup's pull request open to be merged.
Merged on GitHub, it left no merge intent, so the push counted as an external
change: publishing turned off, a "repository changed" blocker and a forced
rescan were added, and a stopped preparation stayed stopped. GitHub's record of
that exact pull request, merged at its previewed head with its previewed tree,
now proves the push, and is recorded as that setup's own merge so the settle
and CI-lineage checks accept it like MLAI's Publish merge. These run without a
database against a read-only GitHub stub.
"""
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
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
        self.build()

    def build(self):
        self.website = SimpleNamespace(pk=uuid4(), organization_id=4, generation=3, repository_id=42,
            github_repo="synthetic/site", branch="main", app_root="", state="connected", installation_id="45",
            verified_sha=BASE, configuration_version=5, organization=SimpleNamespace(domain="synthetic.test"),
            targets=Mock(), capabilities={"publishingReady": True, "previewSupported": True}, blockers=[], save=Mock())
        # The saved pending setup names this run's open pull request.
        self.config = SimpleNamespace(website_connection=self.website, organization_id=4, default_publish_target_id="featured",
            articles_scaffold_pr_url="https://github.com/synthetic/site/pull/40", save=Mock(),
            article_system={"pending_article_system_setup": {"setupRunId": "owner-setup", "setup_run_id": "owner-setup",
                "status": "pr_created", "mergeStatus": "not_merged", "merge_status": "not_merged", "prNumber": 40}})
        self.setup = SimpleNamespace(pk=1, run_id="owner-setup", organization_id=4, domain="synthetic.test",
            github_repo="synthetic/site", workflow="article_system_setup", status="completed", approval_state="approved",
            run_request={**authority.contract_for(self.website), "expected_source_sha": BASE},
            result=verified_setup_result(), save=Mock())
        self.runs = [self.setup]
        repo = {"id": 42, "full_name": "synthetic/site"}
        # GitHub's "Squash and merge": one new commit on BASE whose tree is HEAD's.
        self.pull = {"number": 40, "state": "closed", "merged": True, "merge_commit_sha": MERGED,
            "merged_at": "2026-10-14T13:17:02Z",
            "head": {"sha": HEAD, "ref": "mlai/articles-setup", "repo": dict(repo)},
            "base": {"sha": BASE, "ref": "main", "repo": dict(repo)}}
        self.commits = {MERGED: {"sha": MERGED, "tree": {"sha": TREE}, "parents": [{"sha": BASE}]},
            HEAD: {"sha": HEAD, "tree": {"sha": TREE}, "parents": [{"sha": BASE}]}}
        self.other_pulls, self.unavailable, self.reads = {}, set(), []
        # Row locks in the order taken; before_lock changes the run between the match and its lock.
        self.locks, self.before_lock = [], None

    def lock_run(self):
        self.locks.append("run")
        if self.before_lock:
            self.before_lock(self.setup)
        return SimpleNamespace(filter=lambda **lookups: Runs(self.runs).filter(**lookups))

    def lock_config(self):
        self.locks.append("config")
        return SimpleNamespace(filter=lambda **lookups: Runs([self.config] if lookups == {"website_connection": self.website} else []))

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
        stack.enter_context(patch.object(ContentFactoryRun.objects, "select_for_update", side_effect=self.lock_run))
        stack.enter_context(patch.object(reconciliation.OrganizationContentConfig.objects, "select_for_update", side_effect=self.lock_config))
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

    def push(self, source_sha=MERGED, atomic=nullcontext):
        def lock(name, row):
            self.locks.append(name)
            return SimpleNamespace(get=lambda **lookups: row)
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=atomic))
            stack.enter_context(patch("organizations.models.Organization.objects.select_for_update",
                side_effect=lambda: lock("organization", object())))
            stack.enter_context(patch.object(reconciliation.WebsiteConnection.objects, "select_for_update",
                side_effect=lambda: lock("connection", self.website)))
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
        self.assertEqual(self.logged("info"), ["website_owner_setup_merge_matched", "website_owner_setup_merge_recorded"])
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
                # The match carries the commit GitHub merged as, never another one.
                self.assertEqual((found["owner_merge"]["merge_commit_sha"], found["owner_merge"]["head_sha"],
                    found["owner_merge"]["pr_number"]), (source_sha, HEAD, 40))
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

        @contextmanager
        def guard(*args, **kwargs):
            self.locks.append("authority")
            yield self.website
            self.locks.append("released")
        with ExitStack() as stack:
            self.github(stack)
            stack.enter_context(patch.object(authority, "authority_guard", side_effect=guard))
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
        # The setup records its merge inside the recheck's authority lock, run before saved setup.
        self.assertEqual(self.locks[-4:], ["authority", "run", "config", "released"])
        self.assertEqual((self.setup.result["merge_sha"], self.setup.result["merge_response"]["source"]),
            (MERGED, "github_owner_merge"))
        self.assertTrue(authority.setup_settled_at(self.setup, MERGED))


INTENT = {"connection_generation": 3, "repository_id": 42, "run_id": "owner-setup", "github_repo": "synthetic/site",
    "source_sha": BASE, "base_branch": "main", "pr_number": 40, "head_sha": HEAD, "head_branch": "mlai/articles-setup"}


class OwnerSetupMergeRecordTests(OwnerSetupMergeFixture):
    """The authenticated merge becomes that setup's own recorded merge."""

    def test_owner_merge_is_recorded_as_the_setups_own_merge(self):
        payload, _ = self.push()
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("owner-setup", False))
        result = self.setup.result
        self.assertEqual(result["merge_response"], {"source": "github_owner_merge", "sha": MERGED,
            "pull": {"number": 40, "merge_commit_sha": MERGED, "head_sha": HEAD}})
        for item in (result, result["article_system_setup"]):
            self.assertEqual((item["status"], item["merge_status"], item["mergeStatus"], item["merge_sha"], item["merged_at"]),
                ("merged", "merged", "merged", MERGED, "2026-10-14T13:17:02+00:00"))
        self.assertEqual((self.setup.status, self.setup.approval_state, self.setup.current_step), ("completed", "approved", "merged"))
        self.setup.save.assert_called_once_with(update_fields=["status", "current_step", "approval_state", "result", "updated_at"])
        # The preview proof that vouched for the merge is kept as it was.
        self.assertEqual({key: result[key] for key in ("branch_commit_sha", "source_sha", "live_preview")},
            {"branch_commit_sha": HEAD, "source_sha": BASE, "live_preview": {"exactRender": True, "commitSha": HEAD}})
        pending = self.config.article_system["pending_article_system_setup"]
        self.assertEqual((pending["setupRunId"], pending["status"], pending["mergeStatus"], pending["merge_status"]),
            ("owner-setup", "merged", "merged", "merged"))
        # A scoped merge receipt is history: it grants no generation readiness.
        self.assertNotIn("generationReady", self.config.article_system)
        self.assertNotIn("generationReady", pending)
        self.config.save.assert_called_once_with(update_fields=["article_system", "updated_at"])
        # Inside the webhook's organization and connection locks, the run is locked before the saved setup.
        self.assertEqual(self.locks, ["organization", "connection", "run", "config"])
        self.assertEqual(self.logged("info"), ["website_owner_setup_merge_matched", "website_owner_setup_merge_recorded"])
        self.assertEqual(self.logged("warning"), [])

    def test_later_checks_accept_the_recorded_merge_like_mlais_own(self):
        self.setup.result["adoption"] = {"kit_paths": [".github/workflows/mlai-articles-verification.yml"]}
        with ExitStack() as stack:
            self.github(stack)
            # Unrecorded, a kit-carrying setup was not settled: preparation built another setup.
            self.assertIsNone(authority.settled_setup_run(self.website, source_sha=MERGED))
            self.assertFalse(verification._setup_merged_at(self.website, "owner-setup", MERGED))
        self.push()
        with ExitStack() as stack:
            self.github(stack)
            # The branch's own CI proof is next (ci-attestation), not another setup.
            self.assertIs(authority.settled_setup_run(self.website, source_sha=MERGED), self.setup)
            self.assertFalse(authority.setup_pr_open(self.setup))
            # CI lineage: this setup's own merge produced the source, and no other.
            self.assertTrue(verification._setup_merged_at(self.website, "owner-setup", MERGED))
            self.assertFalse(verification._setup_merged_at(self.website, "owner-setup", "e" * 40))
            # A redelivered push is owned by the recorded merge without reading GitHub again.
            self.reads = []
            self.assertEqual(reconciliation._find_owned_merge(self.website, 4, MERGED),
                {"run_id": "owner-setup", "connection_identity": reconciliation._merge_connection_identity(self.website)})
            self.assertEqual(self.reads, [])
            self.mint.assert_not_called()

    def test_content_factory_snapshots_keep_the_recorded_merge(self):
        from . import service_views
        self.push()
        # The worker's PR-created checkpoint predates the merge.
        checkpoint = {**verified_setup_result(), "merge_status": "not_merged"}
        callback = service_views._merge_django_owned_run_result(self.setup.result, checkpoint)
        self.assertEqual((callback["merge_status"], callback["merge_response"]["source"], callback["merged_at"]),
            ("merged", "github_owner_merge", "2026-10-14T13:17:02+00:00"))
        self.assertEqual(callback["article_system_setup"]["merge_status"], "merged")
        poll = vibe_marketing_views._merge_django_owned_article_result(self.setup.result, checkpoint)
        self.assertEqual((poll["merge_status"], poll["merge_sha"], poll["merge_response"]["sha"]), ("merged", MERGED, MERGED))
        for result in (callback, poll):
            with self.subTest(snapshot="callback" if result is callback else "poll"):
                self.setup.result = result
                self.assertTrue(authority.setup_settled_at(self.setup, MERGED))
                self.assertFalse(authority.setup_settled_at(self.setup, "e" * 40))

    def test_goal_evidence_never_calls_the_owner_merge_a_backend_merge(self):
        from . import website_goal_evidence as goal
        self.push()
        self.website.targets.filter.return_value.first.return_value = None
        self.website.scan_snapshots = Mock()
        self.website.scan_snapshots.filter.return_value.exclude.return_value.order_by.return_value.first.return_value = None

        def proofs(run):
            with patch.object(goal, "_child", return_value=run), patch.object(goal, "scope_for", return_value={}):
                found, source = goal.collect_proofs(SimpleNamespace(receipt={}), SimpleNamespace(company=SimpleNamespace(pk=7)),
                    self.config, {"repository": {"observedSourceSha": MERGED}}, now=timezone.now())
            self.assertEqual(source, MERGED)
            return found
        self.assertFalse({"approved", "merged"} & set(proofs(self.setup)))
        # Not even beside a publish intent saved by an earlier refused Publish.
        intent = {**INTENT, "website_connection_id": str(self.website.pk)}
        self.setup.result["publish_merge_intent"] = intent
        self.assertFalse({"approved", "merged"} & set(proofs(self.setup)))
        # MLAI's own Publish of that pull request is the backend merge.
        published = SimpleNamespace(**{**vars(self.setup), "result": {**self.setup.result,
            "merge_response": {"merged": True, "sha": MERGED}}})
        self.assertEqual(proofs(published)["merged"]["kind"], "backend_merge")

    def test_merge_time_is_githubs_or_else_when_it_was_observed(self):
        observed = datetime(2026, 10, 15, 1, 2, 3, tzinfo=dt_timezone.utc)
        for value in (None, "", "yesterday", "2026-10-14T13:17:02", "2026-13-40T00:00:00Z", 1760447822):
            with self.subTest(merged_at=value):
                self.build()
                self.pull["merged_at"] = value
                with patch("django.utils.timezone.now", return_value=observed):
                    self.push()
                self.assertEqual(self.setup.result["merged_at"], observed.isoformat())
                self.assertEqual(self.setup.result["merge_sha"], MERGED)


class OwnerSetupMergeRecordGuardTests(OwnerSetupMergeFixture):
    """Only the tree-verified match records, and never over a newer receipt."""

    def assertOwnedButUnrecorded(self, payload, result):
        # The authenticated match still owns the push; the run keeps its own receipt.
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("owner-setup", False))
        self.assertEqual(self.setup.result, result)
        self.setup.save.assert_not_called()
        self.config.save.assert_not_called()
        self.assertEqual(self.config.article_system["pending_article_system_setup"]["mergeStatus"], "not_merged")

    def test_setup_changed_since_the_match_keeps_its_own_receipt(self):
        def merged(response):
            def change(run):
                run.result.update(merge_status="merged", merge_response=response)
                run.result["article_system_setup"].update(status="merged", merge_status="merged")
            return change
        changes = {
            "merge already recorded by a GitHub observation": merged({"source": "github_pr_status", "sha": MERGED,
                "pull": {"number": 40, "merge_commit_sha": MERGED}}),
            "merge recorded at another commit": merged({"sha": "e" * 40}),
            "newer preview at another head": lambda run: [item.update(branch_commit_sha="e" * 40)
                for item in (run.result, run.result["article_system_setup"])],
            "preview proof withdrawn": lambda run: run.result["directory_quality_gates"].update(browser=False),
            "pull request closed": lambda run: run.result.update(status="pr_closed"),
            "run cancelled": lambda run: setattr(run, "status", "cancelled"),
            "bound to another generation": lambda run: run.run_request.update(connection_generation=4),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.build()
                self.before_lock = change
                expected = deepcopy(self.setup.result)
                change(SimpleNamespace(result=expected, run_request={}, status=""))
                payload, _ = self.push()
                self.assertOwnedButUnrecorded(payload, expected)
                self.assertEqual(self.locks, ["organization", "connection", "run"])
                self.assertEqual(self.logged("info"), ["website_owner_setup_merge_matched", "website_owner_setup_merge_unrecorded"])

    def test_merge_marked_without_its_commit_since_the_match_gets_that_commit(self):
        def checkpoint(run):
            # Content Factory's completion checkpoint: nested merged, no commit, top level not Django's merge.
            run.result["status"] = "completed"
            run.result["article_system_setup"].update(status="merged_verifying", merge_status="merged")
        self.before_lock = checkpoint
        self.push()
        result = self.setup.result
        self.assertEqual((result["merge_status"], result["merge_sha"], result["merge_response"]["source"]),
            ("merged", MERGED, "github_owner_merge"))
        self.assertTrue(authority.setup_settled_at(self.setup, MERGED))
        changes = {
            "request replaced": lambda run: run.run_request.update(operation_attempt=2),
            "another pull request": lambda run: run.result.update(pr_number=41,
                pr_url="https://github.com/synthetic/site/pull/41"),
            "another head": lambda run: [item.update(branch_commit_sha="e" * 40)
                for item in (run.result, run.result["article_system_setup"])],
            "approval withdrawn": lambda run: setattr(run, "approval_state", "approval_required"),
            "another setup's checkpoint": lambda run: run.result["article_system_setup"].update(setup_run_id="other-setup"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.build()
                self.before_lock = lambda run, change=change: (checkpoint(run), change(run))
                expected = deepcopy(self.setup.result)
                probe = SimpleNamespace(result=expected, run_request={}, approval_state="")
                checkpoint(probe)
                change(probe)
                payload, _ = self.push()
                self.assertOwnedButUnrecorded(payload, expected)

    def test_recorded_and_intent_matches_record_nothing_new(self):
        # MLAI's Publish already recorded this merge: owned without GitHub, nothing rewritten.
        self.setup.result.update(merge_status="merged", merge_sha=MERGED, merge_response={"merged": True, "sha": MERGED})
        self.setup.result["article_system_setup"].update(status="merged", merge_status="merged")
        expected = deepcopy(self.setup.result)
        payload, _ = self.push()
        self.assertOwnedButUnrecorded(payload, expected)
        self.assertEqual((self.reads, self.locks), ([], ["organization", "connection"]))
        self.assertEqual(self.log.mock_calls, [])
        # A saved publish intent matching the merged pull request owns it as MLAI's merge in flight.
        self.build()
        self.setup.result["publish_merge_intent"] = {**INTENT, "website_connection_id": str(self.website.pk)}
        expected = deepcopy(self.setup.result)
        payload, _ = self.push()
        self.assertOwnedButUnrecorded(payload, expected)
        self.assertEqual((self.reads, self.locks), (["pulls/40"], ["organization", "connection"]))
        self.assertEqual(self.log.mock_calls, [])

    def test_unverified_merge_records_nothing(self):
        # The merged tree differs from the previewed one.
        self.commits[MERGED]["tree"]["sha"] = "f" * 40
        expected = deepcopy(self.setup.result)
        payload, _ = self.push()
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("", True))
        self.assertEqual(self.setup.result, expected)
        self.setup.save.assert_not_called()
        self.config.save.assert_not_called()
        self.assertEqual(self.locks, ["organization", "connection"])

    def test_a_recording_failure_never_fails_the_push(self):
        self.config.save.side_effect = RuntimeError("database unavailable at synthetic-host")
        blocks = []

        @contextmanager
        def atomic():
            try:
                yield
            except BaseException as exc:
                blocks.append(type(exc).__name__)
                raise
            blocks.append("committed")
        payload, wake = self.push(atomic=atomic)
        self.assertEqual((payload["owned_merge_run_id"], payload["scan_required"]), ("owner-setup", False))
        self.assertTrue(self.website.capabilities["publishingReady"])
        self.assertEqual(self.website.blockers, [])
        wake.assert_called_once_with(self.website.pk, organization_id=4, owned_merge_run_id="owner-setup")
        # Only the recording's savepoint rolled back; the webhook's transaction committed.
        self.assertEqual(blocks, ["RuntimeError", "committed"])
        self.assertEqual(self.logged("warning"), ["website_owner_setup_merge_unrecorded"])
        self.assertEqual(self.log.warning.call_args.args[-1], "RuntimeError")
        self.assertNotIn("synthetic-host", repr(self.log.mock_calls))
