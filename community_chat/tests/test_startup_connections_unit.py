"""Real DRF parsing/auth contracts with mocked persistence and provider boundaries."""
from contextlib import ExitStack
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.core import signing
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from django.http import HttpResponseRedirect
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.startups import connections, views, source_preferences as prefs
from community_chat.startups.oauth_context import valid_chat_oauth_context
from content_factory.editorial_catalog import merge_strategy, public_strategy
from integrations import api_views_connectors as connectors
from integrations.services.chat_oauth_return import native_chat_connection_return_url, oauth_return_redirect

COMPANY = "11111111-1111-4111-8111-111111111111"


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True)
class StartupConnectionBridgeTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = Obj(id=7, pk=7, is_authenticated=True)
        self.org = Obj(pk=9, id=9, domain="fixture.test")
        self.company = Obj(pk=COMPANY, organization_id=9, organization=self.org, profile=Obj(user_id=7))

    def call(self, cls, method, body=None, provider=None, authenticated=True):
        request = getattr(self.factory, method)("/?company_id=" + COMPANY, body or {}, format="json")
        if authenticated:
            force_authenticate(request, self.user, token=Obj(pk="session-1"))
        kwargs = {"provider": provider} if provider else {}
        with patch.object(views, "get_object_or_404", return_value=self.company):
            return cls.as_view(throttle_classes=())(request, **kwargs)

    def test_default_preference_bridge_auth_scope_and_failed_write(self):
        with patch.object(connections, "set_source_preference", return_value={"provider": "slack", "enabled": False}) as save:
            response = self.call(connections.DisconnectView, "post", {"companyId": COMPANY, "enabled": False}, "slack")
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with(self.company, "slack", False)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        with patch.object(connections, "set_source_preference", side_effect=ValidationError({"enabled": "Choose on or off."})):
            self.assertEqual(self.call(connections.DisconnectView, "post", {"enabled": "false"}, "slack").status_code, 400)
        self.assertEqual(self.call(connections.DisconnectView, "post", {"enabled": False}, "slack", authenticated=False).status_code, 401)

    def test_readback_preserves_data_selection_when_default_off(self):
        row = {"key": "slack", "provider": "slack", "status": "connected", "selected": True, "configured": True, "connectionId": 13}
        with patch.object(connectors.ConnectorSourcesStatusView, "get", return_value=Response({"sources": [row]})), \
             patch.object(views, "source_preferences", return_value={"slack": False}), \
             patch("community_chat.startups.website_connections.website_connection_sources", return_value=[]):
            response = self.call(views.SourcesView, "get")
        result = response.data["sources"][0]
        self.assertFalse(result["enabled"])
        self.assertTrue(result["selected"])
        self.assertTrue(result["usableForUpdates"])
        self.assertEqual(response.data["connections"], response.data["sources"])

    def test_disconnect_is_user_company_provider_scoped_and_keeps_defaults(self):
        qs = MagicMock()
        qs.exclude.return_value.values_list.return_value = [13, 14]
        with patch.object(connections.ExternalServiceConnection.objects, "filter", return_value=qs) as lookup, \
             patch.object(connections, "disconnect_external_connection") as disconnect, \
             patch.object(connections, "set_source_preference") as preference:
            response = self.call(connections.DisconnectView, "delete", {"companyId": COMPANY}, "slack")
        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(user=self.user, organization=self.org, provider="slack")
        self.assertEqual([call.args for call in disconnect.call_args_list], [(self.user, 13), (self.user, 14)])
        preference.assert_not_called()
        with patch.object(connections, "disconnect_gmail_for_user", return_value={"status": "disconnected"}) as gmail:
            self.call(connections.DisconnectView, "delete", {}, "gmail")
        gmail.assert_called_once_with(self.user, organization=self.org, delete_derived_data=False)

    def test_api_key_connect_confirms_only_fresh_provider_status(self):
        for provider in ("luma", "humanitix"):
            for status_value in ("connected", "needs_reauth"):
                with self.subTest(provider=provider, status=status_value), ExitStack() as stack:
                    stack.enter_context(patch.object(connectors, "_org_scope_or_response", return_value=(self.org, None)))
                    stack.enter_context(patch.object(connectors, "_company_for_scope", return_value=self.company))
                    connect = stack.enter_context(patch.object(connectors, "connect_" + provider + "_connection"))
                    stack.enter_context(patch.object(connectors, "serialize_source_status", return_value={"sources": [{"key": provider, "status": status_value}]}))
                    response = self.call(connections.ConnectView, "post", {"companyId": COMPANY, "apiKey": "synthetic-test-key"}, provider)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data["connected"], status_value == "connected")
                    connect.assert_called_once_with(self.user, "synthetic-test-key", company=self.company)
                    self.assertNotIn("synthetic-test-key", str(response.data))
        with patch.object(connectors, "connect_luma_connection") as connect:
            response = self.call(connections.ConnectView, "post", {"companyId": COMPANY}, "luma")
        self.assertEqual(response.status_code, 400)
        connect.assert_not_called()

    def test_native_return_choice_is_signed_and_arbitrary_url_discarded(self):
        response = self.call(connections.ConnectView, "post", {"returnTo": "mobile"}, "slack")
        ticket = parse_qs(urlsplit(response.data["authorizationUrl"]).query)["ticket"][0]
        payload = signing.loads(ticket, salt=connections.SALT)
        self.assertEqual((payload["return_to"], payload["company"], payload["uid"], payload["session"]), ("mobile", COMPANY, 7, "session-1"))
        response = self.call(connections.ConnectView, "post", {"returnTo": "https://malicious.test"}, "slack")
        ticket = parse_qs(urlsplit(response.data["authorizationUrl"]).query)["ticket"][0]
        self.assertIsNone(signing.loads(ticket, salt=connections.SALT)["return_to"])

    def test_browser_handoff_uses_only_signed_native_return_and_company(self):
        request = self.factory.get('/?ticket=synthetic-ticket')
        session = Obj(pk="session-1", user=self.user)
        with patch.object(connections, 'consume_ticket', return_value={"uid": 7, "company": COMPANY, "session": "session-1", "provider": "slack", "return_to": "mobile"}), \
             patch.object(connections.CommunityChatAccountSession.objects, 'select_related') as sessions, \
             patch.object(connections, '_valid_session', return_value=True), \
             patch.object(connections.VibeRaisingCompany.objects, 'get', return_value=self.company), \
             patch.object(connections, 'connector_connect', return_value=HttpResponseRedirect('https://provider.example.test/oauth')) as start:
            sessions.return_value.filter.return_value.first.return_value = session
            response = connections.connect_browser(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(request.GET['next'], 'mlaichat://connections?company_id=' + COMPANY + '&provider=slack')
        self.assertEqual(request.chat_oauth_context, {"chat_session_id": "session-1", "chat_company_id": COMPANY, "user_id": 7, "organization_id": 9})
        start.assert_called_once_with(request, 'slack')

    def test_bridge_routes_resolve_without_migrations(self):
        self.assertIs(resolve("/api/v1/community-chat/startups/sources/slack/").func.view_class, connections.DisconnectView)
        self.assertIs(resolve("/api/v1/community-chat/startups/connect/luma/").func.view_class, connections.ConnectView)


class SourcePreferenceStorageTests(SimpleTestCase):
    def setUp(self):
        self.company = Obj(pk=COMPANY, organization_id=9, profile=Obj(user_id=7))
        self.strategy = {"pillars": ["culture"], "editorial_catalog": {"version": 4},
                         prefs.PREFERENCE_KEY: {COMPANY + ":7": {"slack": True}, COMPANY + ":8": {"luma": False}, "other:7": {"stripe": False}}}

    def test_locked_save_retains_catalog_other_accounts_and_preferences(self):
        config = Obj(progress_configuration=self.strategy, save=MagicMock())
        with patch.object(prefs.Organization.objects, "select_for_update") as lock_org, \
             patch.object(prefs.StartupProfile.objects, "select_for_update") as lock_config:
            lock_config.return_value.get_or_create.return_value = (config, False)
            result = prefs.set_source_preference.__wrapped__(self.company, "slack", False)
        lock_org.return_value.get.assert_called_once_with(pk=9)
        lock_config.return_value.get_or_create.assert_called_once_with(organization_id=9)
        self.assertFalse(result["enabled"])
        self.assertEqual(config.progress_configuration["editorial_catalog"], {"version": 4})
        self.assertEqual(config.progress_configuration[prefs.PREFERENCE_KEY][COMPANY + ":8"], {"luma": False})
        self.assertFalse(config.progress_configuration[prefs.PREFERENCE_KEY][COMPANY + ":7"]["slack"])
        config.save.assert_called_once_with(update_fields=["progress_configuration", "updated_at"])
        self.assertTrue(self.strategy[prefs.PREFERENCE_KEY][COMPANY + ":7"]["slack"])

    def test_read_is_owner_company_scoped(self):
        with patch.object(prefs.StartupProfile.objects, "filter") as configs:
            configs.return_value.first.return_value = Obj(progress_configuration=self.strategy)
            self.assertEqual(prefs.source_preferences(self.company), {"slack": True})
            self.company.profile.user_id = 8
            self.assertEqual(prefs.source_preferences(self.company), {"luma": False})

    def test_generated_strategy_cannot_overwrite_or_expose_private_defaults(self):
        merged = merge_strategy(self.strategy, {prefs.PREFERENCE_KEY: {"attacker": True}, "pillars": ["new"]})
        self.assertEqual(merged[prefs.PREFERENCE_KEY], self.strategy[prefs.PREFERENCE_KEY])
        self.assertNotIn(prefs.PREFERENCE_KEY, public_strategy(merged))
        self.assertEqual(public_strategy(merged)["pillars"], ["new"])
        self.assertNotIn(prefs.PREFERENCE_KEY, merge_strategy({}, {prefs.PREFERENCE_KEY: {"attacker": True}}))

    def test_syncing_missing_selection_and_website_sources_cannot_be_update_inputs(self):
        for provider, status_value, selected in (("slack", "syncing", True), ("google_analytics", "connected", False), ("google_drive", "connected", True), ("github", "connected", True)):
            self.assertFalse(prefs.source_capabilities({"key": provider, "status": status_value, "selected": selected})["usableForUpdates"])
        self.assertTrue(prefs.source_capabilities({"key": "slack", "status": "connected", "selected": True}, preferences={"slack": False})["usableForUpdates"])

    def test_default_inclusion_requires_saved_connection_but_explicit_choice_survives_revoke(self):
        for status_value in ("not_connected", "unavailable"):
            self.assertFalse(prefs.source_capabilities({"key": "slack", "status": status_value})["enabled"])
        for status_value in ("connected", "syncing", "needs_reauth", "expired"):
            self.assertTrue(prefs.source_capabilities({"key": "slack", "status": status_value})["enabled"])
        self.assertTrue(prefs.source_capabilities({"key": "slack", "status": "not_connected"}, preferences={"slack": True})["enabled"])


class NativeOAuthBoundaryTests(SimpleTestCase):
    def test_only_exact_callback_route_and_routing_fields_are_accepted(self):
        uri = "mlaichat://connections?company_id=" + COMPANY + "&provider=slack"
        self.assertEqual(native_chat_connection_return_url(uri), uri)
        response = oauth_return_redirect(uri)
        self.assertEqual(response["Location"], uri)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        for bad in (uri + "&token=secret", uri + "&provider=github", uri + "#secret", uri.replace("connections?", "evil?"), uri.replace(COMPANY, "invalid"), uri.replace("slack", "unknown")):
            self.assertIsNone(native_chat_connection_return_url(bad))

    def test_revoked_session_or_changed_company_cannot_complete_callback(self):
        payload = {"chat_session_id": "session-1", "chat_company_id": COMPANY, "organization_id": 9}
        with patch("community_chat.startups.oauth_context.CommunityChatAccountSession.objects.select_related") as sessions, \
             patch("community_chat.startups.oauth_context.VibeRaisingCompany.objects.filter") as companies, \
             patch("community_chat.startups.oauth_context._valid_session", return_value=True):
            sessions.return_value.filter.return_value.first.return_value = Obj()
            companies.return_value.exists.return_value = True
            self.assertTrue(valid_chat_oauth_context(payload, 7))
            companies.assert_called_with(pk=COMPANY, profile__user_id=7, organization_id=9)
            companies.return_value.exists.return_value = False
            self.assertFalse(valid_chat_oauth_context(payload, 7))
        with patch("community_chat.startups.oauth_context.CommunityChatAccountSession.objects.select_related"), \
             patch("community_chat.startups.oauth_context._valid_session", return_value=False):
            self.assertFalse(valid_chat_oauth_context(payload, 7))

    def test_signed_connector_and_github_states_preserve_and_revalidate_chat_context(self):
        from integrations.services import external_connectors as external, github_connections as github
        class Session(dict):
            modified = False
        context = {"chat_session_id": "session-1", "chat_company_id": COMPANY, "user_id": 7}
        request = Obj(user=Obj(id=7), session=Session(), chat_oauth_context=context)
        state = external._save_state(request, "slack", "mlaichat://connections?company_id=" + COMPANY + "&provider=slack")
        with patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=False):
            with self.assertRaises(external.ConnectorOAuthError):
                external._consume_state(request, "slack", state)
        state = github.build_github_oauth_state(domain="fixture.test", slack_user_id="mlai_user:7", chat_context=context)
        with patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=False):
            with self.assertRaises(ValueError):
                github.validate_github_oauth_state(state.raw)
