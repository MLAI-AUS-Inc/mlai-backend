"""P1.14: binding the domain's repository for new-journey Chat GitHub connections.

The callback binds only a repository the founder can push to, whose name
matches the domain (or the only such repository), with an empty folder and
the default branch, then starts setup and records the sticky owner click.
A legacy organisation's callback binds nothing and keeps today's behaviour.
"""
import uuid
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from content_factory.founder_journey_cohort import read_marker
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from content_factory.website_contract import WebsiteAuthorityError
from content_factory.website_models import WebsiteConnection
from integrations import http_client
from integrations.services import github_callback_reasons as reasons
from integrations.services.github_connections import build_github_oauth_state

FRONTEND = "https://chat.example"
COMMON = {
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    "COMMUNITY_CHAT_FRONTEND_URL": FRONTEND,
    "GITHUB_OAUTH_CLIENT_ID": "synthetic-client",
    "GITHUB_OAUTH_CLIENT_SECRET": "synthetic-secret",
}


def _json(payload):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _repo(name, *, push=True, admin=False):
    return {"full_name": name, "owner": {"login": name.split("/")[0]},
            "permissions": {"push": push, "admin": admin, "pull": True}}


def _metadata(repo, branch="trunk"):
    return {"repository_id": abs(hash(repo)) % 10_000_000 + 1, "github_repo": repo, "installation_id": "77", "branch": branch}


class MatchingTests(TestCase):
    def test_exact_and_hyphenated_names_match_the_full_domain(self):
        for name in ("acme/banksia.studio", "acme/banksia-studio", "acme/BanksiaStudio"):
            with self.subTest(name):
                self.assertEqual(reasons.match_repository("banksia.studio", [_repo(name), _repo("acme/other")]),
                                 (reasons.CONNECTED, _repo(name)))

    def test_first_label_only_for_two_label_domains(self):
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("acme/banksia"), _repo("acme/other")])[0],
                         reasons.CONNECTED)
        self.assertEqual(reasons.match_repository("banksia.com.au", [_repo("acme/banksia"), _repo("acme/other")]),
                         (reasons.MULTIPLE_REPOS, None))
        self.assertEqual(reasons.match_repository("banksia.com.au", [_repo("acme/banksia-com-au"), _repo("acme/other")])[0],
                         reasons.CONNECTED)

    def test_read_only_repositories_are_never_candidates(self):
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("acme/banksia-studio", push=False)]),
                         (reasons.NO_REPO, None))
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("acme/banksia-studio", push=False),
                                                                      _repo("acme/site", admin=True, push=False)]),
                         (reasons.CONNECTED, _repo("acme/site", admin=True, push=False)))

    def test_candidates_without_a_single_match(self):
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("acme/one"), _repo("acme/two")]),
                         (reasons.MULTIPLE_REPOS, None))
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("a/banksia"), _repo("b/banksia")]),
                         (reasons.MULTIPLE_REPOS, None))
        self.assertEqual(reasons.match_repository("banksia.studio", []), (reasons.NO_REPO, None))
        self.assertEqual(reasons.match_repository("banksia.studio", [_repo("acme/anything")]),
                         (reasons.CONNECTED, _repo("acme/anything")))


@override_settings(**COMMON, **ALL_P1_FLAGS_ON)
class CallbackBindingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    @contextmanager
    def github(self, repositories, *, verify=None):
        with ExitStack() as stack:
            stack.enter_context(patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=True))
            stack.enter_context(patch("integrations.services.github_installations.upsert_github_installation"))
            stack.enter_context(patch("integrations.services.slack.SlackService.send_dm"))
            self.scan = stack.enter_context(patch("integrations.services.github.trigger_scan_async"))
            self.verify = stack.enter_context(patch("content_factory.website_connections.verify_repository_access",
                                                    side_effect=verify or (lambda *, user, repo: _metadata(repo))))
            self.prepare = stack.enter_context(patch("content_factory.website_prepare.start_prepare", return_value=Obj(pk=1)))
            self.advance = stack.enter_context(patch("content_factory.website_prepare.advance_prepare"))
            self.mint = stack.enter_context(patch("integrations.services.github_app.create_installation_access_token"))
            stack.enter_context(patch.object(http_client, "post", return_value=_json(
                {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600})))
            stack.enter_context(patch.object(http_client, "get", side_effect=[
                _json({"login": "founder"}), _json({"repositories": repositories})]))
            yield

    def callback(self, built, repositories, *, verify=None, return_url=None):
        state = build_github_oauth_state(
            domain=built.organization.domain, slack_user_id=f"user:{built.owner.pk}",
            return_url=return_url or f"{FRONTEND}/my-startup/website?company_id={built.company.pk}",
            chat_context={"user_id": built.owner.pk, "chat_company_id": str(built.company.pk),
                          "chat_session_id": "session", "organization_id": built.organization.pk})
        with self.github(repositories, verify=verify):
            return self.client.get(reverse("github_callback"), {
                "code": "synthetic-code", "installation_id": "77", "setup_action": "install", "state": state.raw})

    def reason(self, response):
        self.assertEqual(response.status_code, 302)
        return response["Location"].rsplit("github=", 1)[1]

    def test_exact_and_hyphenated_matches_bind_with_the_default_branch_and_an_empty_folder(self):
        for style in ("dotted", "hyphenated"):
            with self.subTest(style):
                domain = f"b{uuid.uuid4().hex[:6]}.studio"
                built = make_new_journey_org(domain=domain)
                repo = "founder/" + (domain if style == "dotted" else domain.replace(".", "-"))
                response = self.callback(built, [_repo(repo), _repo("founder/unrelated")])
                self.assertEqual(self.reason(response), reasons.CONNECTED)
                built.config.refresh_from_db()
                connection = built.config.website_connection
                self.assertEqual(connection.github_repo, repo)
                self.assertEqual(connection.app_root, "")
                self.assertEqual(connection.branch, "trunk")
                self.assertFalse(any(blocker.get("code") in {"APPLICATION_ROOT_VERIFICATION_REQUIRED", "SETUP_BRANCH_VERIFICATION_REQUIRED"}
                                     for blocker in connection.blockers))
                self.prepare.assert_called_once()
                self.assertEqual(self.prepare.call_args.kwargs["company_id"], built.company.pk)
                self.assertTrue(read_marker(built.organization)["ownerClickRequired"])

    def test_first_label_binds_for_two_labels_but_not_for_three(self):
        studio = make_new_journey_org(domain="banksia.studio")
        self.assertEqual(self.reason(self.callback(studio, [_repo("founder/banksia"), _repo("founder/other")])), reasons.CONNECTED)
        australian = make_new_journey_org(domain="banksia.com.au")
        self.assertEqual(self.reason(self.callback(australian, [_repo("founder/banksia"), _repo("founder/other")])),
                         reasons.MULTIPLE_REPOS)
        australian.config.refresh_from_db()
        self.assertIsNone(australian.config.website_connection)

    def test_read_only_repository_is_never_bound_even_when_it_is_the_only_match(self):
        built = make_new_journey_org(domain="banksia.studio")
        response = self.callback(built, [_repo("founder/banksia-studio", push=False)])
        self.assertEqual(self.reason(response), reasons.NO_REPO)
        self.verify.assert_not_called()
        self.assertFalse(WebsiteConnection.objects.filter(organization=built.organization).exists())
        self.assertIsNone((read_marker(built.organization) or {}).get("ownerClickRequired"))

    def test_several_candidates_none_and_the_only_candidate(self):
        several = make_new_journey_org(domain="banksia.studio")
        self.assertEqual(self.reason(self.callback(several, [_repo("founder/a"), _repo("founder/b")])), reasons.MULTIPLE_REPOS)
        none = make_new_journey_org()
        self.assertEqual(self.reason(self.callback(none, [])), reasons.NO_REPO)
        only = make_new_journey_org()
        self.assertEqual(self.reason(self.callback(only, [_repo("founder/my-site")])), reasons.CONNECTED)
        only.config.refresh_from_db()
        self.assertEqual(only.config.website_connection.github_repo, "founder/my-site")

    def test_installation_token_is_reminted_once_before_asking_again(self):
        built = make_new_journey_org(domain="banksia.studio")
        unavailable = WebsiteAuthorityError("github_repository_unavailable", "GitHub could not verify access.")
        calls = []

        def flaky(*, user, repo):
            calls.append(repo)
            if len(calls) == 1:
                raise unavailable
            return _metadata(repo)

        response = self.callback(built, [_repo("founder/banksia-studio")], verify=flaky)
        self.assertEqual(self.reason(response), reasons.CONNECTED)
        self.assertEqual(len(calls), 2)
        self.mint.assert_called_once_with(installation_id="77", repository="founder/banksia-studio", permission_mode="read")

        again = make_new_journey_org(domain="second-banksia.studio")
        calls.clear()

        def always(*, user, repo):
            calls.append(repo)
            raise unavailable

        response = self.callback(again, [_repo("founder/second-banksia-studio")], verify=always)
        self.assertEqual(self.reason(response), reasons.REPOSITORY_ACCESS)
        self.assertEqual(len(calls), 2)
        self.mint.assert_called_once()
        self.assertIsNone((read_marker(again.organization) or {}).get("ownerClickRequired"))

    def test_repository_already_connected_elsewhere(self):
        other = make_new_journey_org(domain="other-startup.example")
        other.config.github_repo = "founder/banksia-studio"
        other.config.save(update_fields=["github_repo"])
        built = make_new_journey_org(domain="banksia.studio")
        self.assertEqual(self.reason(self.callback(built, [_repo("founder/banksia-studio")])), reasons.OWNED_ELSEWHERE)
        self.verify.assert_not_called()

    def test_successful_bind_writes_owner_click_and_keeps_other_marker_keys(self):
        built = make_new_journey_org(domain="banksia.studio")
        before = read_marker(built.organization)
        self.callback(built, [_repo("founder/banksia-studio")])
        after = read_marker(built.organization)
        self.assertTrue(after["ownerClickRequired"])
        self.assertEqual({key: value for key, value in after.items() if key != "ownerClickRequired"}, before)

    def test_legacy_org_bind_writes_nothing_and_keeps_todays_behaviour(self):
        legacy = make_named_legacy_org("talathrive", domain="banksia-legacy.studio")
        connection_before = legacy.config.website_connection_id
        marker_before = read_marker(legacy.organization)
        return_url = f"{FRONTEND}/my-startup/connections?company_id={legacy.company.pk}&connected=github"
        response = self.callback(legacy, [_repo("founder/banksia-legacy-studio")], return_url=return_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], return_url
                         + "&github=connected&domain=banksia-legacy.studio&repo=founder%2Fbanksia-legacy-studio")
        self.verify.assert_not_called()
        self.prepare.assert_not_called()
        legacy.config.refresh_from_db()
        self.assertEqual(legacy.config.website_connection_id, connection_before)
        self.assertEqual(read_marker(legacy.organization), marker_before)

    def test_flag_off_new_journey_bind_is_todays_behaviour(self):
        built = make_new_journey_org(domain="banksia.studio")
        with override_settings(MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED=False):
            response = self.callback(built, [_repo("founder/banksia-studio")])
        self.assertIn("github=connected&domain=banksia.studio&repo=founder%2Fbanksia-studio", response["Location"])
        self.verify.assert_not_called()
        self.assertIsNone((read_marker(built.organization) or {}).get("ownerClickRequired"))
