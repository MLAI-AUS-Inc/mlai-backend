"""One source decision for every run action (build plan workstream 4).

No database: the resolver is a table over (action, branch state), and the
callers' seams (proceed_past_moved_head, followed_repository_name and
source_repin._resolve_repin) are exercised with the GitHub read stubbed.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import website_connections as wc
from .website_connections import (
    SOURCE_ACTIONS, SOURCE_DECISIONS, SOURCE_STATES, SourceObservation, classify_source_comparison,
    decide_run_source, followed_repository_name, guard_source_action, proceed_past_moved_head,
    resolve_run_source,
)
from .website_contract import WebsiteAuthorityError

# The plan's four branch states, plus the three the resolver also has to
# answer. Every action needs a row with every state; the expected cells are
# written out here, not derived from SOURCE_DECISIONS.
EXPECTED = {
    #                    unchanged     fast-forward  setup files   renamed   unreachable  rewritten  unverified
    "read":              ("unchanged", "follow",     "refuse",     "follow", "refuse",    "refuse",  "follow"),
    "preview_retry":     ("unchanged", "follow",     "refuse",     "follow", "refuse",    "refuse",  "follow"),
    "repin":             ("unchanged", "follow",     "refuse",     "follow", "refuse",    "refuse",  "follow"),
    "terminal_callback": ("unchanged", "unchanged",  "unchanged",  "follow", "refuse",    "unchanged", "unchanged"),
    "publish":           ("unchanged", "refuse",     "refuse",     "follow", "refuse",    "refuse",  "refuse"),
}
STATE_COLUMNS = ("unchanged", "fast_forward", "setup_files_changed", "repo_renamed",
                 "repo_unreachable", "history_rewritten", "unverified")

SHA_A, SHA_B = "a" * 40, "b" * 40
SETUP_CACHE = {"registry_path": "src/lib/articles.ts", "route_shell_path": "src/app/articles/[slug]/page.tsx"}


def _connection(**overrides):
    values = {"pk": "conn", "github_repo": "founder/site", "repository_id": 7, "branch": "main",
              "verified_sha": SHA_A, "blockers": []}
    values.update(overrides)
    return SimpleNamespace(**values)


def _compare(status, *filenames):
    return {"status": status, "total_commits": 1,
            "files": [{"filename": name, "status": "modified"} for name in filenames]}


@override_settings(ARTICLES_SINGLE_SOURCE_RESOLVER_ENABLED=True, ARTICLES_SOURCE_FOLLOW_ENABLED=True)
class SourceTableTests(SimpleTestCase):
    def test_every_action_has_a_full_row(self):
        self.assertEqual(set(EXPECTED), set(SOURCE_ACTIONS))
        self.assertEqual(set(SOURCE_DECISIONS), set(SOURCE_ACTIONS))
        self.assertEqual(STATE_COLUMNS, SOURCE_STATES)
        for action in SOURCE_ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(set(SOURCE_DECISIONS[action]), set(SOURCE_STATES))
                for outcome, reason in SOURCE_DECISIONS[action].values():
                    self.assertIn(outcome, wc.SOURCE_OUTCOMES)
                    self.assertRegex(reason, r"^[a-z][a-z_]+$")

    def test_table(self):
        for action, row in EXPECTED.items():
            for state, expected in zip(STATE_COLUMNS, row):
                with self.subTest(action=action, state=state):
                    decision = decide_run_source(action, SourceObservation(state, SHA_A))
                    self.assertEqual(decision.outcome, expected)
                    self.assertEqual(decision.proceeds, expected != "refuse")
                    self.assertEqual((decision.action, decision.state), (action, state))

    def test_unknown_action_or_state_refuses(self):
        self.assertEqual(decide_run_source("deploy", SourceObservation("unchanged")).reason, "unknown_source_action")
        self.assertEqual(decide_run_source("read", SourceObservation("teleported")).reason, "unknown_source_state")

    def test_follow_switch_still_gates_reads_and_owner_retry(self):
        with override_settings(ARTICLES_SOURCE_FOLLOW_ENABLED=False):
            for action in ("read", "preview_retry"):
                decision = decide_run_source(action, SourceObservation("fast_forward"))
                self.assertEqual((decision.outcome, decision.reason), ("refuse", "source_follow_disabled"))
                self.assertEqual(decide_run_source(action, SourceObservation("repo_renamed")).outcome, "follow")
            # Re-pin keeps its own cohort gate (ARTICLES_SOURCE_REPIN_ENABLED).
            self.assertEqual(decide_run_source("repin", SourceObservation("fast_forward")).outcome, "follow")


class ClassifyComparisonTests(SimpleTestCase):
    def test_states_from_github_compare(self):
        cases = (
            (_compare("identical"), True, "unchanged"),
            (_compare("ahead", "README.md"), True, "fast_forward"),
            (_compare("ahead", "src/app/articles/[slug]/page.tsx"), True, "setup_files_changed"),
            # A setup run installs those files itself.
            (_compare("ahead", "src/app/articles/[slug]/page.tsx"), False, "fast_forward"),
            # Another article landing in the registry is not a setup change.
            (_compare("ahead", "src/lib/articles.ts"), True, "fast_forward"),
            (_compare("diverged", "README.md"), True, "history_rewritten"),
            (_compare("behind"), True, "history_rewritten"),
            (None, True, "unverified"),
            ({"status": "ahead", "files": None}, True, "unverified"),
        )
        for comparison, article_run, expected in cases:
            with self.subTest(comparison=comparison, article_run=article_run):
                self.assertEqual(classify_source_comparison(comparison, setup_cache=SETUP_CACHE,
                    article_run=article_run), expected)

    def test_no_setup_record_means_fast_forward(self):
        self.assertEqual(classify_source_comparison(_compare("ahead", "src/app/page.tsx"), setup_cache={}), "fast_forward")


class _FakeConfigs:
    def __init__(self, cache):
        self.cache = cache

    def filter(self, **_):
        return self

    def first(self):
        return SimpleNamespace(article_system_setup_cache=self.cache)


@override_settings(ARTICLES_SINGLE_SOURCE_RESOLVER_ENABLED=True, ARTICLES_SOURCE_FOLLOW_ENABLED=True)
class ObserveAndResolveTests(SimpleTestCase):
    def resolve(self, action, comparison, *, run=None, connection=None, **kwargs):
        run = run or SimpleNamespace(run_id="run-1", workflow="direct_generate", github_repo="founder/site",
                                     run_request={"expected_source_sha": SHA_A})
        with patch.object(wc.OrganizationContentConfig, "objects", _FakeConfigs(SETUP_CACHE)), \
                patch("content_factory.website_source_carry.compare_sources", return_value=comparison) as compare:
            decision = resolve_run_source(run, action, connection=connection or _connection(), **kwargs)
        return decision, compare

    def test_reads_the_runs_pin_against_the_branch(self):
        decision, compare = self.resolve("preview_retry", _compare("ahead", "README.md"))
        self.assertEqual((decision.outcome, decision.reason), ("follow", "head_fast_forward"))
        self.assertEqual(compare.call_args.args[1:], (SHA_A, "main"))

    def test_setup_change_refuses_the_owner_retry(self):
        decision, _ = self.resolve("preview_retry", _compare("ahead", "src/app/articles/[slug]/page.tsx"))
        self.assertEqual((decision.outcome, decision.reason), ("refuse", "setup_files_changed"))

    def test_same_head_needs_no_github_read(self):
        decision, compare = self.resolve("repin", None, pinned_sha=SHA_B, head_ref=SHA_B)
        self.assertEqual(decision.outcome, "unchanged")
        compare.assert_not_called()

    def test_rename_follows_unless_the_installation_lost_access(self):
        decision, compare = self.resolve("publish", None, github_repo="founder/old-name")
        self.assertEqual((decision.outcome, decision.reason), ("follow", "repository_renamed"))
        compare.assert_not_called()
        blocked = _connection(blockers=[{"code": "github_access_required"}])
        decision, _ = self.resolve("publish", None, github_repo="founder/old-name", connection=blocked)
        self.assertEqual((decision.outcome, decision.reason), ("refuse", "repository_unreachable"))


def _moved():
    return WebsiteAuthorityError("website_source_changed", "The repository changed since this verification.")


class GuardSeamTests(SimpleTestCase):
    """authority_guard's two decisions, with the resolver switch off and on."""

    def test_guard_actions_map_to_rows_only_when_known(self):
        self.assertEqual(guard_source_action("preview", {}, follow_moved_preview=True), "preview_retry")
        self.assertEqual(guard_source_action("read", {}, follow_moved_preview=True), "preview_retry")
        self.assertEqual(guard_source_action("read", {}), "read")
        self.assertEqual(guard_source_action("merge", {}), "publish")
        self.assertEqual(guard_source_action("publish", {}), "publish")
        self.assertEqual(guard_source_action("config_write", {"event_type": "article_complete"}), "terminal_callback")
        self.assertEqual(guard_source_action("config_write", {"status": "failed"}), "terminal_callback")
        self.assertIsNone(guard_source_action("config_write", {"event_type": "progress"}))
        self.assertIsNone(guard_source_action("setup", {}))
        self.assertIsNone(guard_source_action("preview", {}))

    @override_settings(ARTICLES_SOURCE_FOLLOW_ENABLED=True)
    def test_switch_off_keeps_the_old_rule(self):
        connection = _connection()
        with patch.object(wc, "resolve_run_source") as resolver:
            self.assertTrue(proceed_past_moved_head(_moved(), action="preview", source_action=None, payload={},
                connection=connection, pinned_sha=SHA_A, follow_moved_preview=True))
            for action in ("read", "publish", "config_write"):
                self.assertFalse(proceed_past_moved_head(_moved(), action=action, source_action=None, payload={},
                    connection=connection, pinned_sha=SHA_A))
            self.assertEqual(followed_repository_name(None, {}, {"repository_id": 7}, connection, "founder/old"),
                             "founder/old")
        resolver.assert_not_called()

    @override_settings(ARTICLES_SINGLE_SOURCE_RESOLVER_ENABLED=True, ARTICLES_SOURCE_FOLLOW_ENABLED=True)
    def test_switch_on_asks_the_resolver(self):
        connection = _connection()
        run = SimpleNamespace(run_id="run-1", workflow="direct_generate", github_repo="founder/site", run_request={})
        cases = (
            # A terminal callback on a moved branch was refused before (and the
            # run marked failed); now it is recorded and keeps its pin.
            ("config_write", "terminal_callback", _compare("ahead", "README.md"), True),
            ("read", "read", _compare("ahead", "README.md"), True),
            ("publish", "publish", _compare("ahead", "README.md"), False),
            ("preview", "preview_retry", _compare("ahead", "src/app/articles/[slug]/page.tsx"), False),
        )
        for action, source_action, comparison, expected in cases:
            with self.subTest(action=action), patch.object(wc, "_source_run", return_value=run), \
                    patch.object(wc.OrganizationContentConfig, "objects", _FakeConfigs(SETUP_CACHE)), \
                    patch("content_factory.website_source_carry.compare_sources", return_value=comparison):
                self.assertEqual(proceed_past_moved_head(_moved(), action=action, source_action=source_action,
                    payload={"run_id": "run-1"}, connection=connection, pinned_sha=SHA_A), expected)
        other = WebsiteAuthorityError("github_source_unavailable", "GitHub could not verify the source.", retryable=True)
        self.assertFalse(proceed_past_moved_head(other, action="read", source_action="read", payload={},
            connection=connection, pinned_sha=SHA_A))

    @override_settings(ARTICLES_SINGLE_SOURCE_RESOLVER_ENABLED=True)
    def test_rename_follows_only_the_same_repository_id(self):
        connection = _connection(github_repo="founder/new-name")
        with patch.object(wc, "_source_run", return_value=None):
            self.assertEqual(followed_repository_name("publish", {}, {"repository_id": 7}, connection, "founder/old-name"),
                             "founder/new-name")
            self.assertEqual(followed_repository_name("publish", {}, {"repository_id": 8}, connection, "founder/old-name"),
                             "founder/old-name")
            self.assertEqual(followed_repository_name("publish", {}, {}, connection, "founder/old-name"),
                             "founder/old-name")


@override_settings(ARTICLES_SOURCE_FOLLOW_ENABLED=True)
class RepinSeamTests(SimpleTestCase):
    def run_obj(self):
        return SimpleNamespace(run_id="run-1", workflow="direct_generate", github_repo="founder/site",
            run_request={"website_connection_id": "00000000-0000-0000-0000-000000000001", "connection_generation": 1,
                         "repository_id": 7, "expected_source_sha": SHA_A})

    def call(self, comparison):
        from . import source_repin

        class Connections:
            def filter(self, **_):
                return self

            def first(self):
                return _connection()

        with patch.object(source_repin.WebsiteConnection, "objects", Connections()), \
                patch.object(wc.OrganizationContentConfig, "objects", _FakeConfigs(SETUP_CACHE)), \
                patch("content_factory.website_source_carry.compare_sources", return_value=comparison) as compare:
            result = source_repin._resolve_repin(self.run_obj(), SHA_A, SHA_B)
        return result, compare

    def test_switch_off_reads_nothing(self):
        result, compare = self.call(_compare("ahead", "src/app/articles/[slug]/page.tsx"))
        self.assertIsNone(result)
        compare.assert_not_called()

    @override_settings(ARTICLES_SINGLE_SOURCE_RESOLVER_ENABLED=True)
    def test_switch_on_refuses_a_setup_change_and_follows_a_push(self):
        with self.assertRaises(WebsiteAuthorityError) as error:
            self.call(_compare("ahead", "src/app/articles/[slug]/page.tsx"))
        self.assertEqual(error.exception.authority_code, "source_repin_refused")
        result, compare = self.call(_compare("ahead", "README.md"))
        self.assertEqual(result.outcome, "follow")
        self.assertEqual(compare.call_args.args[1:3], (SHA_A, SHA_B))
