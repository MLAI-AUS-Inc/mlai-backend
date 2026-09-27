"""Provider contracts with synthetic tokens: no database, migrations, or network."""
from contextlib import ExitStack
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
import time

from django.core import signing
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.response import Response

from community_chat.startups import connections, website_connections
from integrations import views
from integrations.services import external_connectors as external
from integrations.services import github_connections as github
from integrations.services.chat_oauth_return import native_chat_connection_return_url, oauth_return_redirect

COMPANY = "00000000-0000-0000-0000-000000000001"
NATIVE = "mlaichat://connections?" + urlencode({"company_id": COMPANY, "provider": "google_search_console"})


class Session(dict):
    modified = False


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    ROOT_URLCONF="community_chat.startups.urls",
    COMMUNITY_CHAT_FRONTEND_URL="https://chat.example", DEFAULT_FRONTEND_URL="https://website.example")
class OAuthHandoffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.user = Obj(pk=7, id=7, is_authenticated=True)
        self.organization = Obj(pk=9, id=9, domain="startup.example")
        self.company = Obj(pk=COMPANY, organization=self.organization)

    def request(self, query=None, *, authenticated=True):
        request = RequestFactory().get("/oauth/callback", query or {})
        request.session = Session()
        request.user = self.user if authenticated else Obj(is_authenticated=False)
        return request

    def state(self, provider, *, native=NATIVE, chat=False):
        request = self.request()
        if chat:
            request.chat_oauth_context = {"chat_session_id": "session", "chat_company_id": COMPANY}
        return external._save_state(request, provider, native, {"organization_id": 9})

    def test_native_return_is_fixed_and_contains_no_credentials(self):
        self.assertEqual(native_chat_connection_return_url(NATIVE), NATIVE)
        response = oauth_return_redirect(NATIVE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], NATIVE)
        invalid = [NATIVE + "&token=secret", NATIVE + "&provider=gmail", NATIVE + "#fragment",
            NATIVE.replace("connections?", "other?"), NATIVE.replace("connections?", "connections/path?"),
            NATIVE.replace(COMPANY, "other"), NATIVE.replace("mlaichat:", "evil:")]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(native_chat_connection_return_url(value))
        dev = NATIVE.replace("mlaichat:", "mlaichat-dev:")
        self.assertEqual(oauth_return_redirect(dev)["Location"], dev)

    def test_all_return_validators_agree_and_reject_external_origins(self):
        self.assertEqual(views._normalize_google_next(NATIVE), NATIVE)
        self.assertEqual(external.normalize_connector_next(NATIVE), NATIVE)
        self.assertEqual(github.build_post_install_redirect_url(NATIVE, repo="private/repo"), NATIVE)
        self.assertIsNone(views._normalize_google_next("https://evil.example/my-startup/connections"))
        self.assertNotIn("evil.example", external.normalize_connector_next("https://evil.example/my-startup/connections"))
        self.assertFalse(github.is_allowed_return_url("https://evil.example"))

    def test_native_provider_ticket_preserves_owned_company_and_fixed_return_intent(self):
        view = connections.ConnectView()
        view.company = self.company
        request = Obj(user=self.user, auth=Obj(pk="session"), data={"returnTo": "mobile", "returnUrl": "https://evil.example"},
            build_absolute_uri=lambda path: "https://api.example" + path)
        for provider in connections.PROVIDERS:
            with self.subTest(provider=provider):
                response = view.post(request, provider)
                ticket = parse_qs(urlsplit(response.data["authorizationUrl"]).query)["ticket"][0]
                payload = connections.consume_ticket(ticket)
                self.assertEqual(payload["company"], COMPANY)
                self.assertEqual(payload["provider"], provider)
                self.assertTrue(payload["return_to"])
                self.assertNotIn("returnUrl", payload)
                with self.assertRaises(signing.BadSignature):
                    connections.consume_ticket(ticket)

    def test_google_drive_and_analytics_hyphen_aliases_are_accepted(self):
        view = connections.ConnectView()
        view.company = self.company
        request = Obj(user=self.user, auth=Obj(pk="session"), data={}, build_absolute_uri=lambda path: "https://api.example" + path)
        for alias, canonical in connections.PROVIDER_ALIASES.items():
            response = view.post(request, alias)
            ticket = parse_qs(urlsplit(response.data["authorizationUrl"]).query)["ticket"][0]
            self.assertEqual(connections.consume_ticket(ticket)["provider"], canonical)

    def test_browser_gsc_handoff_requests_website_scope_and_native_return(self):
        session = Obj(pk="session", user=self.user)
        payload = {"session": "session", "uid": 7, "company": COMPANY, "provider": "google_search_console", "return_to": True}
        request = self.request({"ticket": "synthetic"})
        with patch.object(connections, "enabled", return_value=True), patch.object(connections, "consume_ticket", return_value=payload), \
                patch.object(connections.CommunityChatAccountSession, "objects") as sessions, \
                patch.object(connections, "_valid_session", return_value=True), \
                patch.object(connections.VibeRaisingCompany, "objects") as companies, \
                patch.object(connections, "connector_connect", return_value=Response(status=302)) as connect:
            sessions.select_related.return_value.filter.return_value.first.return_value = session
            companies.get.return_value = self.company
            response = connections.connect_browser(request)
        self.assertEqual(response.status_code, 302)
        connect.assert_called_once_with(request, "google")
        self.assertEqual(request.GET["scope"], "website_baseline")
        self.assertEqual(request.GET["next"], NATIVE)
        self.assertEqual(request.chat_oauth_context["chat_company_id"], COMPANY)

    def test_provider_state_is_one_use_without_browser_cookie(self):
        for provider in connections.OAUTH_PROVIDERS:
            with self.subTest(provider=provider):
                state = self.state(provider)
                self.assertEqual(external.connector_oauth_state_user_id(provider=provider, state=state), 7)
                request = self.request()
                result = external._consume_state(request, provider, state)
                self.assertEqual(result["organization_id"], 9)
                with self.assertRaises(external.ConnectorOAuthError):
                    external._consume_state(request, provider, state)

    def test_provider_state_rejects_tampering_cross_user_and_revoked_chat_session(self):
        state = self.state("google_drive")
        request = self.request()
        for provider, value in (("slack", state), ("google_drive", state + "tampered")):
            with self.assertRaises(external.ConnectorOAuthError):
                external._consume_state(request, provider, value)
        request.user = Obj(id=8)
        with self.assertRaises(external.ConnectorOAuthError):
            external._consume_state(request, "google_drive", state)
        state = self.state("google_drive", chat=True)
        with patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=False):
            with self.assertRaises(external.ConnectorOAuthError):
                external._consume_state(self.request(), "google_drive", state)

    def test_every_external_oauth_provider_stores_tokens_for_selected_organization(self):
        methods = {"stripe": "stripe", "xero": "xero", "notion": "notion", "google_drive": "google_drive",
            "google_analytics": "google_analytics", "slack": "slack", "linear": "linear"}
        for provider, suffix in methods.items():
            with self.subTest(provider=provider), ExitStack() as stack:
                state = self.state(provider)
                request = self.request({"state": state, "code": "synthetic-code"})
                orgs = stack.enter_context(patch.object(external.Organization, "objects"))
                orgs.filter.return_value.first.return_value = self.organization
                exchange = stack.enter_context(patch.object(external, "_exchange_" + suffix + "_code", return_value={"access_token": "synthetic-token"}))
                store = stack.enter_context(patch.object(external, "_store_" + suffix + "_connection", return_value=Obj(id=11)))
                if provider == "slack":
                    stack.enter_context(patch("integrations.services.slack_dm_mirror.activate_connection"))
                result = external.complete_oauth_callback(request, provider)
                self.assertEqual(result, NATIVE)
                exchange.assert_called_once_with("synthetic-code")
                self.assertEqual(store.call_args.args[:2], (self.user, self.organization))
                self.assertEqual(store.call_args.args[2]["access_token"], "synthetic-token")

    def test_missing_startup_fails_before_token_exchange(self):
        request = self.request({"state": self.state("google_drive"), "code": "synthetic"})
        with patch.object(external.Organization, "objects") as orgs, patch.object(external, "_exchange_google_drive_code") as exchange:
            orgs.filter.return_value.first.return_value = None
            with self.assertRaises(external.ConnectorOAuthError):
                external.complete_oauth_callback(request, "google_drive")
            exchange.assert_not_called()

    def test_all_external_callback_views_recover_user_from_signed_state(self):
        for provider in connections.OAUTH_PROVIDERS - {"gmail"}:
            request = self.request({"state": self.state(provider), "code": "synthetic"}, authenticated=False)
            with self.subTest(provider=provider), patch.object(views, "_resolve_google_oauth_user", return_value=None), \
                    patch("django.contrib.auth.get_user_model") as model, \
                    patch.object(views, "complete_oauth_callback", return_value=NATIVE):
                model.return_value.objects.filter.return_value.first.return_value = self.user
                response = views.connector_callback(request, provider)
                self.assertEqual(response["Location"], NATIVE)
                self.assertIs(request.user, self.user)

    def test_google_callback_without_cookie_persists_tokens_and_returns_to_native_app(self):
        request = self.request({"state": self.state("gmail"), "code": "synthetic"}, authenticated=False)
        with patch.object(views, "_resolve_google_oauth_user", return_value=None), patch("django.contrib.auth.get_user_model") as model, \
                patch("organizations.models.Organization.objects") as orgs, patch.object(views.GoogleConnection, "objects") as connections_manager, \
                patch.object(views.requests, "post") as post, patch.object(views.requests, "get") as get:
            model.return_value.objects.filter.return_value.first.return_value = self.user
            orgs.filter.return_value.first.return_value = self.organization
            connections_manager.filter.return_value.first.return_value = None
            post.return_value.json.return_value = {"access_token": "synthetic", "refresh_token": "synthetic-refresh", "scope": "openid https://www.googleapis.com/auth/webmasters.readonly"}
            get.return_value.json.return_value = {"email": "founder@example.test"}
            response = views.google_callback(request)
        self.assertEqual(response["Location"], NATIVE)
        self.assertEqual(connections_manager.update_or_create.call_args.kwargs["organization"], self.organization)
        self.assertEqual(connections_manager.update_or_create.call_args.kwargs["defaults"]["refresh_token"], "synthetic-refresh")

    def test_logged_in_browser_cannot_bypass_expired_or_replayed_google_state(self):
        with patch("django.core.signing.time.time", return_value=time.time() - 10000):
            expired = self.state("gmail", chat=True)
        replayed = self.state("gmail")
        external._consume_state(self.request(), "gmail", replayed)
        for state in (expired, replayed):
            request = self.request({"state": state, "code": "synthetic"})
            request.session[views.GOOGLE_OAUTH_STATE_SESSION_KEY] = state
            with self.subTest(state=state[:10]), patch.object(views, "_resolve_google_oauth_user", return_value=self.user), patch.object(views.requests, "post") as exchange:
                response = views.google_callback(request)
                self.assertEqual(response.status_code, 400)
                exchange.assert_not_called()

    def test_signed_initiator_overrides_unrelated_browser_account(self):
        request = self.request({"state": self.state("google_drive"), "code": "synthetic"})
        other = Obj(pk=8, id=8, is_authenticated=True)
        with patch.object(views, "_resolve_google_oauth_user", return_value=other), patch("django.contrib.auth.get_user_model") as users, \
                patch.object(views, "complete_oauth_callback", return_value=NATIVE) as complete:
            users.return_value.objects.filter.return_value.first.return_value = self.user
            response = views.connector_callback(request, "google_drive")
        self.assertEqual(response["Location"], NATIVE)
        self.assertIs(complete.call_args.args[0].user, self.user)

    def test_every_external_oauth_authorization_url_uses_signed_owned_intent(self):
        hosts = {"stripe": "connect.stripe.com", "xero": "login.xero.com", "notion": "api.notion.com",
            "google_drive": "accounts.google.com", "google_analytics": "accounts.google.com",
            "slack": "slack.com", "linear": "linear.app"}
        for provider, host in hosts.items():
            request = self.request({"next": NATIVE, "company_id": COMPANY})
            with self.subTest(provider=provider), patch.object(external, "_provider_configuration_error", return_value=None), \
                    patch.object(external, "company_for_user_from_request", return_value=(self.company, True)), \
                    patch.object(external, "resolve_connector_organization", return_value=self.organization), \
                    patch.object(external, "current_slack_oauth_generation", return_value=1):
                url = external.build_authorization_url(request, provider)
            parsed = urlsplit(url)
            self.assertEqual(parsed.netloc, host)
            state = parse_qs(parsed.query)["state"][0]
            payload = external._consume_state(self.request(), provider, state)
            self.assertEqual(payload["organization_id"], self.organization.pk)
            self.assertEqual(payload["next"], NATIVE)

    @override_settings(GOOGLE_WEBSITE_BASELINE_SCOPES=["https://www.googleapis.com/auth/webmasters.readonly"])
    def test_google_search_console_start_uses_website_scopes_not_gmail(self):
        request = self.request({"scope": "website_baseline", "company_id": COMPANY, "next": NATIVE})
        with patch.object(views, "_resolve_google_oauth_user", return_value=self.user), patch.object(views, "_ensure_django_session_for_user"), \
                patch.object(external, "company_for_user_from_request", return_value=(self.company, True)), \
                patch.object(external, "resolve_connector_organization", return_value=self.organization):
            response = views.google_connect(request)
        query = parse_qs(urlsplit(response["Location"]).query)
        self.assertIn("webmasters.readonly", query["scope"][0])
        self.assertNotIn("gmail", query["scope"][0])
        self.assertEqual(external._consume_state(self.request(), "gmail", query["state"][0])["next"], NATIVE)

    def test_bank_feed_callback_updates_only_selected_startup(self):
        request = self.request({"jobIds": "job-1", "state": self.state("bank_feed")})
        with patch.object(external.Organization, "objects") as orgs, patch.object(external.ExternalServiceConnection, "objects") as records:
            orgs.filter.return_value.first.return_value = self.organization
            record = records.filter.return_value.first.return_value
            record.provider_metadata = {}
            self.assertEqual(external.complete_oauth_callback(request, "bank_feed"), NATIVE)
            self.assertEqual(records.filter.call_args.kwargs["organization"], self.organization)
            self.assertEqual(record.status, "syncing")

    def test_github_state_recovers_chat_identity_and_rejects_replay(self):
        state = github.build_github_oauth_state(domain="startup.example", slack_user_id="user:7", return_url=NATIVE,
            chat_context={"user_id": 7, "chat_company_id": COMPANY, "chat_session_id": "session"})
        with patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=True):
            actual = github.validate_github_oauth_state(state.raw, request=self.request(authenticated=False))
            self.assertEqual(actual.chat_context["user_id"], 7)
            self.assertEqual(actual.return_url, NATIVE)
            with self.assertRaises(ValueError):
                github.validate_github_oauth_state(state.raw, request=self.request())

    def test_github_callback_persists_selected_installation_and_returns_to_dev_app(self):
        native = NATIVE.replace("mlaichat:", "mlaichat-dev:").replace("google_search_console", "github")
        state = github.build_github_oauth_state(domain="startup.example", slack_user_id="user:7", return_url=native,
            chat_context={"user_id": 7, "chat_company_id": COMPANY, "chat_session_id": "session"})
        request = self.request({"state": state.raw, "code": "synthetic", "installation_id": "123"}, authenticated=False)
        config = Obj(connected_slack_user_id="", github_repo="", save=MagicMock())
        with patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=True), \
                patch("founder_tools.models.VibeRaisingCompany.objects") as companies, \
                patch("content_factory.models.OrganizationContentConfig.objects") as configs, \
                patch.object(views.UserIntegration, "objects"), \
                patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=self.user), \
                patch("integrations.services.github_installations.upsert_github_installation") as registry, \
                patch("integrations.services.slack.SlackService.send_dm") as dm, \
                patch.object(views.requests, "post") as post, patch.object(views.requests, "get") as get:
            companies.select_related.return_value.filter.return_value.first.return_value = self.company
            configs.get_or_create.return_value = (config, False)
            post.return_value.json.return_value = {"access_token": "synthetic-token", "refresh_token": "synthetic-refresh"}
            identity = MagicMock()
            identity.json.return_value = {"login": "founder"}
            repos = MagicMock()
            repos.json.return_value = {"repositories": [{"full_name": "founder/site", "owner": {"login": "founder"}}]}
            get.side_effect = [identity, repos]
            response = views.github_callback(request)
        self.assertEqual(response["Location"], native)
        self.assertEqual(config.github_repo, "founder/site")
        self.assertEqual(config.github_installation_id, "123")
        self.assertEqual(config.github_token_encrypted, "synthetic-token")
        self.assertIs(registry.call_args.kwargs["user"], self.user)
        self.assertEqual(configs.get_or_create.call_args.kwargs["organization"], self.organization)
        dm.assert_not_called()

    def test_github_unverified_installation_cannot_write_credentials(self):
        for error in (views.requests.HTTPError("403"), views.requests.HTTPError("404"), views.requests.RequestException("offline")):
            state = github.build_github_oauth_state(domain="startup.example", slack_user_id="user:7", return_url=NATIVE)
            request = self.request({"state": state.raw, "code": "synthetic", "installation_id": "unverified"})
            with self.subTest(error=str(error)), patch("content_factory.models.OrganizationContentConfig.objects") as configs, \
                    patch("integrations.services.github_installations.upsert_github_installation") as registry, \
                    patch.object(views.requests, "post") as post, patch.object(views.requests, "get") as get:
                post.return_value.json.return_value = {"access_token": "synthetic-token"}
                identity = MagicMock()
                identity.json.return_value = {"login": "founder"}
                get.side_effect = [identity, error]
                response = views.github_callback(request)
            self.assertEqual(response.status_code, 400)
            registry.assert_not_called()
            configs.get_or_create.assert_not_called()

    def test_api_key_success_is_verified_from_catalog(self):
        view = connections.ConnectView()
        for provider, handler in (("luma", "LumaConnectView"), ("humanitix", "HumanitixConnectView")):
            for status in ("connected", "error"):
                with self.subTest(provider=provider, status=status), patch.object(connections, handler) as service:
                    service.return_value.post.return_value = Response({"sources": [{"provider": provider, "status": status}]})
                    result = view.post(Obj(data={"apiKey": "synthetic"}), provider)
                    self.assertEqual(result.data["connected"], status == "connected")

    def test_website_sources_are_company_scoped_and_never_update_inputs(self):
        with patch.object(website_connections, "google_connection_for_org", return_value=None) as google, \
                patch.object(website_connections, "is_provider_configured", return_value=True), \
                patch.object(website_connections, "actor_ids_for_user", return_value=["user:7"]), \
                patch.object(website_connections.OrganizationContentConfig, "objects") as configs:
            configs.filter.return_value.first.return_value = None
            sources = website_connections.website_connection_sources(self.user, self.company)
        self.assertEqual([row["provider"] for row in sources], ["google_search_console", "github"])
        self.assertTrue(all(not row["usableForUpdates"] and not row["enabled"] for row in sources))
        google.assert_called_once_with(self.user, self.organization)
        self.assertEqual(configs.filter.call_args.kwargs["organization"], self.organization)
