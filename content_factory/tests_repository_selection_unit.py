"""Plan E8: repository suggestion, permission and merge-ability checks at selection, and the reviewer ask.

Every GitHub payload here is a recorded fixture under
``testdata/repository-selection``; nothing calls GitHub.
"""
import json
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from integrations.services import github_app
from integrations.services.github_installations import _normalize_repo

from . import founder_transitions
from . import website_connections
from . import website_repository_selection as selection
from .founder_journey_testing import make_new_journey_org
from .tests_founder_journey_unit import JourneyFixture, make_run
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection
from .website_setup_merge_policy import REVIEW_REQUIRED_REASON, setup_merge_policy

FIXTURES = Path(__file__).resolve().parent / "testdata" / "repository-selection"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=dt_timezone.utc)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def normalized(name, installation_id="1000001"):
    """The recorded raw repositories through the picker's own normaliser."""
    return [_normalize_repo(repo, installation_id) for repo in fixture(name)["repositories"]]


def response(status, payload=None):
    item = MagicMock(status_code=status, headers={}, content=b"{}")
    item.json.return_value = payload
    return item


class RankingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_normalised_repositories_carry_the_ranking_signals(self):
        repo = normalized("installation-repositories.json")[1]
        self.assertEqual((repo["fullName"], repo["homepage"], repo["pushedAt"], repo["archived"], repo["fork"],
                          repo["language"], repo["topics"]),
                         ("fixture-studio/marketing-site", "https://www.tigertutor.example", "2026-10-01T09:00:00Z",
                          False, False, "TypeScript", ["nextjs"]))

    def test_the_homepage_match_ranks_first_and_archived_and_forks_rank_last(self):
        ranked = selection.rank_repositories(normalized("installation-repositories.json"),
                                             domain="tigertutor.example", brand="Tiger Tutor", now=NOW)
        self.assertEqual([row["fullName"].split("/")[1] for row in ranked],
                         ["marketing-site", "tigertutor", "tiger-tutor-n8n", "tigertutor-old-site", "infra",
                          "nextjs-starter"])
        self.assertIn("homepage_matches_domain", ranked[0]["reasons"])
        self.assertIn("archived", ranked[3]["reasons"])
        self.assertIn("fork", ranked[-1]["reasons"])

    def test_the_best_match_is_only_preselected(self):
        result = selection.suggestion(normalized("installation-repositories.json"), domain="https://tigertutor.example/",
                                      brand="Tiger Tutor", now=NOW)
        self.assertEqual(result["repo"], "fixture-studio/marketing-site")
        self.assertTrue(result["looksLikeWebsite"])
        self.assertIsNone(result["warning"])
        self.assertFalse(result["nothingVisible"])

    def test_an_examon_shaped_repository_is_preselected_with_a_soft_warning(self):
        repos = [repo for repo in normalized("installation-repositories.json") if repo["name"] == "tiger-tutor-n8n"]
        result = selection.suggestion(repos, domain="tigertutor.example", brand="Tiger Tutor", now=NOW)
        self.assertEqual(result["repo"], "fixture-studio/tiger-tutor-n8n")
        self.assertEqual(result["warning"]["reason"], "not_a_website")
        self.assertEqual(result["warning"]["message"], selection.NOT_A_WEBSITE)

    def test_no_clear_winner_preselects_nothing(self):
        repos = [{**repo, "homepage": None, "pushedAt": "2026-11-10T09:00:00Z"}
                 for repo in normalized("host-config-repositories.json")]
        result = selection.suggestion(repos, domain="blueharbour.example", brand="Blue Harbour", now=NOW)
        self.assertIsNone(result["repo"])
        self.assertFalse(result["nothingVisible"])
        self.assertEqual(result["ranked"][0]["score"], result["ranked"][1]["score"])

    def test_nothing_plausible_says_we_cannot_see_the_repository_with_the_access_link(self):
        repos = [repo for repo in normalized("installation-repositories.json") if repo["name"] in {"infra"}]
        repos = [{**repos[0], "pushedAt": "2020-01-01T00:00:00Z"}, {**repos[0], "fullName": "fixture-studio/notes",
                                                                        "name": "notes", "pushedAt": None}]
        url = selection.installation_access_url("1000001", account_login="fixture-studio", account_type="Organization")
        result = selection.suggestion(repos, domain="tigertutor.example", brand="Tiger Tutor", now=NOW, access_url=url)
        self.assertTrue(result["nothingVisible"])
        self.assertIsNone(result["repo"])
        self.assertEqual(result["nothingVisibleCopy"]["headline"], "We can't see your website's repository yet")
        self.assertEqual(result["nothingVisibleCopy"]["url"],
                         "https://github.com/organizations/fixture-studio/settings/installations/1000001")

    def test_the_domain_in_host_config_lifts_a_repository_and_probes_stay_bounded(self):
        recorded = fixture("host-config-repositories.json")
        repos = normalized("host-config-repositories.json")
        calls = []

        def reader(repo, path):
            calls.append((repo["fullName"], path))
            return recorded["contents"][repo["fullName"]].get(path)
        ranked = selection.rank_repositories(repos, domain="blueharbour.example", brand="Blue Harbour", now=NOW)
        hints = selection.host_config_hints(repos, ranked, domain="blueharbour.example", reader=reader)
        result = selection.suggestion(repos, domain="blueharbour.example", brand="Blue Harbour", hints=hints, now=NOW)
        self.assertEqual(result["repo"], "fixture-agency/web")
        self.assertIn("domain_in_host_config", result["reasons"])
        self.assertEqual(hints["fixture-agency/web"]["configFile"], "wrangler.jsonc")
        landing = [path for name, path in calls if name == "fixture-agency/landing"]
        self.assertEqual(landing, ["", "CNAME", "vercel.json", "netlify.toml"])
        # Cached per repository and push: a second picker load reads nothing.
        calls.clear()
        selection.host_config_hints(repos, ranked, domain="blueharbour.example", reader=reader)
        self.assertEqual(calls, [])

    def test_probes_read_only_the_top_five_and_never_archived_repositories(self):
        repos = [{**repo, "fullName": f"fixture-studio/site-{index}", "name": f"site-{index}"}
                 for index, repo in enumerate(normalized("installation-repositories.json") * 2)]
        ranked = selection.rank_repositories(repos, domain="tigertutor.example", brand="Tiger Tutor", now=NOW)
        seen = set()
        selection.host_config_hints(repos, ranked, domain="tigertutor.example",
                                    reader=lambda repo, path: seen.add(repo["fullName"]) or [])
        self.assertEqual(len(seen), selection.PROBE_LIMIT)
        archived = {repo["fullName"] for repo in repos if repo["archived"]}
        self.assertFalse(seen & archived)

    def test_a_failing_probe_is_only_a_missing_hint(self):
        repos = normalized("host-config-repositories.json")
        ranked = selection.rank_repositories(repos, domain="blueharbour.example", now=NOW)

        def reader(repo, path):
            raise RuntimeError("provider down")
        self.assertEqual(selection.host_config_hints(repos, ranked, domain="blueharbour.example", reader=reader), {})


class MergeAbilityTests(SimpleTestCase):
    def ability(self, *, rules, protection, permission, founder="fixture-founder"):
        recorded = fixture("branch-policy.json")

        def get(path):
            if path.startswith("/rules/branches/"):
                return (200, recorded[rules]) if isinstance(rules, str) else rules
            if path.endswith("/protection"):
                return (403, recorded[protection]) if protection == "protection_forbidden" else (200, recorded[protection])
            return (200, recorded[permission])
        return selection.merge_ability(repo="fixture-agency/site", branch="main", owner_login="fixture-agency",
                                       founder_login=founder, get=get)

    def test_a_required_review_means_the_founder_cannot_merge_alone(self):
        result = self.ability(rules="rules_required_review", protection="protection_forbidden", permission="permission_write")
        self.assertEqual((result["reviewRequired"], result["founderCanMerge"]), (True, False))

    def test_classic_protection_with_required_reviews_counts(self):
        result = self.ability(rules="rules_none", protection="protection_required_review", permission="permission_admin")
        self.assertEqual((result["reviewRequired"], result["founderCanMerge"]), (True, False))

    def test_code_owner_review_counts(self):
        result = self.ability(rules="rules_code_owners", protection="protection_forbidden", permission="permission_admin")
        self.assertEqual((result["codeOwnerReview"], result["founderCanMerge"]), (True, False))

    def test_read_only_access_to_a_developers_repository_names_the_owner(self):
        result = self.ability(rules="rules_none", protection="protection_forbidden", permission="permission_read")
        self.assertEqual((result["founderPermission"], result["founderCanMerge"], result["reviewer"]),
                         ("read", False, "fixture-agency"))

    def test_an_unprotected_branch_and_an_admin_founder_can_merge(self):
        result = self.ability(rules="rules_none", protection="protection_open", permission="permission_admin")
        self.assertEqual((result["reviewRequired"], result["founderCanMerge"], result["policyUnknown"]), (False, True, False))

    def test_unknown_policy_is_never_can_merge(self):
        result = self.ability(rules="rules_none", protection="protection_forbidden", permission="permission_write")
        self.assertIsNone(result["founderCanMerge"])
        self.assertTrue(result["policyUnknown"])
        outage = self.ability(rules=(502, None), protection="protection_open", permission="permission_admin")
        self.assertIsNone(outage["founderCanMerge"])

    def test_the_merge_policy_still_refuses_to_merge_past_a_required_review(self):
        recorded = fixture("branch-policy.json")
        responses = iter([response(200, recorded["rules_required_review"])])
        website = SimpleNamespace(branch="main", github_repo="fixture-agency/site")
        with patch("content_factory.website_setup_merge_policy.http_client.get", side_effect=lambda *a, **k: next(responses)):
            policy = setup_merge_policy(website, {"state": "open"}, token="t", checks={"state": "success"})
        self.assertEqual(policy, {"decision": "needs_user", "reason": REVIEW_REQUIRED_REASON})


@override_settings(GITHUB_APP_ID="1", GITHUB_APP_PRIVATE_KEY="fixture")
class PermissionCheckTests(TestCase):
    def check(self, installation):
        recorded = fixture("grants.json")
        replies = {"https://api.github.com/app": recorded["app"],
                   "https://api.github.com/app/installations/1000001": recorded[installation]}
        with patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                patch.object(github_app.http_requests, "get", side_effect=lambda url, **kw: response(200, replies[url])):
            return selection.permission_check("1000001")

    def test_a_grant_without_contents_fails_at_selection_with_a_clear_code(self):
        with self.assertRaises(WebsiteAuthorityError) as caught:
            self.check("installation_no_contents")
        self.assertEqual(caught.exception.code, "github_access_required")
        self.assertEqual(caught.exception.field_errors,
                         [{"field": "githubRepo", "reason": "contents_permission_missing", "permission": "contents"}])
        self.assertEqual(str(caught.exception), selection.CONTENTS_PERMISSION_DETAIL)

    def test_full_grants_pass_and_read_only_grants_are_reported(self):
        self.assertEqual(self.check("installation_full"),
                         {"checked": True, "contents": "write", "pullRequests": "write", "writable": True})
        self.assertFalse(self.check("installation_read_only")["writable"])

    def test_a_provider_outage_is_not_the_founders_problem(self):
        with patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                patch.object(github_app.http_requests, "get", return_value=response(503)):
            self.assertEqual(selection.permission_check("1000001"), {"checked": False})


@override_settings(GITHUB_APP_ID="1", GITHUB_APP_PRIVATE_KEY="fixture")
class SelectionBindTests(TestCase):
    METADATA = {"repository_id": 930001, "github_repo": "fixture-agency/site", "installation_id": "1000001",
                "branch": "main"}

    def setUp(self):
        built = make_new_journey_org()
        self.config, self.owner = built.config, built.owner

    def grants(self, installation):
        recorded = fixture("grants.json")
        replies = {"https://api.github.com/app": recorded["app"],
                   "https://api.github.com/app/installations/1000001": recorded[installation]}
        return patch.object(github_app.http_requests, "get", side_effect=lambda url, **kw: response(200, replies[url]))

    def test_binding_a_repository_without_contents_access_stops_before_any_connection(self):
        with patch.object(website_connections, "verify_repository_access", return_value=dict(self.METADATA)), \
                patch.object(github_app, "_github_app_jwt", return_value="jwt"), self.grants("installation_no_contents"), \
                self.assertRaises(WebsiteAuthorityError) as caught:
            website_connections.bind_website(self.config, user=self.owner, repo="fixture-agency/site")
        self.assertEqual(caught.exception.field_errors[0]["reason"], "contents_permission_missing")
        self.assertFalse(WebsiteConnection.objects.filter(organization=self.config.organization).exists())

    def test_binding_records_whether_the_founder_can_merge_for_this_generation(self):
        recorded = fixture("branch-policy.json")
        repo_reads = {"/rules/branches/main?per_page=100&page=1": (200, recorded["rules_required_review"]),
                      "/branches/main/protection": (403, recorded["protection_forbidden"])}

        grants = fixture("grants.json")
        grant_reads = {"https://api.github.com/app": grants["app"],
                       "https://api.github.com/app/installations/1000001": grants["installation_full"]}

        def provider_get(url, **kw):
            if url in grant_reads:
                return response(200, grant_reads[url])
            path = url.removeprefix("https://api.github.com/repos/fixture-agency/site")
            status, payload = repo_reads[path]
            return response(status, payload)
        credential = SimpleNamespace(token="read-token")
        # github_app reads through the same http_client module.
        with patch.object(website_connections, "verify_repository_access", return_value=dict(self.METADATA)), \
                patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                patch.object(github_app, "create_installation_access_token", return_value=credential) as mint, \
                patch("integrations.http_client.get", side_effect=provider_get), \
                patch("integrations.http_client.delete") as revoke:
            connection = website_connections.bind_website(self.config, user=self.owner, repo="fixture-agency/site")
        self.assertEqual(mint.call_args.kwargs["permission_mode"], "read")
        revoke.assert_called_once()
        connection.refresh_from_db()
        record = selection.current_record(connection)
        self.assertEqual((record["reviewRequired"], record["founderCanMerge"], record["branch"]), (True, False, "main"))
        self.assertTrue(selection.reviewer_needed(connection))
        # A later generation makes the record stale rather than wrong.
        connection.generation += 1
        self.assertIsNone(selection.current_record(connection))


class ReviewerAskJourneyTests(JourneyFixture, TestCase):
    URL = "https://github.com/example/site/pull/7"

    def blocked_setup(self, **result):
        return self.setup_run("blocked", result={"failure_class": "needs_user", "pr_html_url": self.URL,
                                                 "failure": {"action": {"id": "merge_pr"}}, **result})

    def test_a_protected_branch_yields_the_reviewer_ask_not_a_stuck_merge(self):
        self.blocked_setup(merge_status="manual_merge_required", merge_blocked_reason=REVIEW_REQUIRED_REASON)
        body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="send_pr_to_reviewer")
        headline, copy, label = selection.REVIEWER_COPY
        self.assertEqual((body["headline"], body["body"], body["action"]["label"], body["action"]["url"]),
                         (headline, copy, label, self.URL))

    def test_selection_merge_ability_turns_the_merge_ask_into_the_reviewer_ask(self):
        connection = self.connection()
        selection.store_merge_ability(connection, selection._ability(review=False, code_owners=False,
            permission="read", can_merge=False, reviewer="example"))
        self.blocked_setup()
        body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="send_pr_to_reviewer")
        self.assertEqual(body["headline"], "Send this change to example")

    def test_a_founder_who_can_merge_still_approves_on_github(self):
        connection = self.connection()
        selection.store_merge_ability(connection, selection._ability(review=False, code_owners=False,
            permission="admin", can_merge=True))
        self.blocked_setup()
        self.assertStep(self.journey(), "setup", "your_turn", ask="approve_on_github")

    def test_the_article_merge_workers_protection_code_is_a_reviewer_ask(self):
        run = make_run(self.org, status="blocked", result={"error_code": "BRANCH_PROTECTION_APPROVAL_REQUIRED"})
        self.assertTrue(selection.reviewer_needed(None, run))
        self.assertFalse(selection.reviewer_needed(None, make_run(self.org, status="blocked")))

    def test_reminders_aim_at_the_reviewer_step(self):
        connection = self.connection()
        selection.store_merge_ability(connection, selection._ability(can_merge=False))
        self.assertEqual(founder_transitions._reviewer_ask("approve_on_github", connection), "send_pr_to_reviewer")
        self.assertEqual(founder_transitions._reviewer_ask("approve_setup", connection), "approve_setup")
        self.assertIn("send_pr_to_reviewer", founder_transitions.OWNER_ASKS)
        state = {"ask": "send_pr_to_reviewer", "run": None}
        self.assertEqual(founder_transitions._copy("your_turn", state, self.org)[0], "Your change needs a reviewer on GitHub")

    def test_a_share_link_must_be_a_github_pull_request(self):
        self.assertIsNone(selection.reviewer_ask(url="https://evil.example/pull/1")["url"])
        self.assertIsNone(selection.reviewer_ask(url="https://github.com/example/site")["url"])
        self.assertEqual(selection.reviewer_ask(url=self.URL)["url"], self.URL)


class PickerSuggestionTests(TestCase):
    """The picker read ranks and preselects; it never binds a repository."""

    def test_the_picker_suggestion_preselects_without_binding(self):
        from .vibe_marketing_views import _repository_suggestion
        built = make_new_journey_org("tigertutor.example")
        context = SimpleNamespace(organization=built.organization, company=SimpleNamespace(name="Tiger Tutor"))
        with override_settings(GITHUB_APP_ID="", GITHUB_APP_PRIVATE_KEY=""):
            result = _repository_suggestion(context, built.owner, normalized("installation-repositories.json"))
        self.assertEqual(result["repo"], "fixture-studio/marketing-site")
        self.assertFalse(WebsiteConnection.objects.filter(organization=built.organization).exists())
        built.config.refresh_from_db()
        self.assertFalse(built.config.github_repo)
