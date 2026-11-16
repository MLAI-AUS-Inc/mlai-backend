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
        qs.exclude.return_value = qs
        qs.values_list.return_value = [13, 14]
        qs.filter.return_value.exists.return_value = True
        with patch.object(connections.ExternalServiceConnection.objects, "filter", return_value=qs) as lookup, \
             patch.object(connections, "disconnect_external_connection") as disconnect, \
             patch.object(connections, "set_source_preference") as preference:
            response = self.call(connections.DisconnectView, "delete", {"companyId": COMPANY}, "slack")
            self.assertEqual(response.status_code, 400)
            disconnect.assert_not_called()
            lookup.assert_called_once_with(user=self.user, organization=self.org, provider="slack")
            response = self.call(connections.DisconnectView, "delete", {"companyId": COMPANY, "connectionId": 13}, "slack")
            self.assertEqual(response.status_code, 200)
            qs.filter.assert_called_once_with(pk=13)
        disconnect.assert_called_once_with(self.user, 13)
        preference.assert_not_called()
        qs.values_list.return_value = [21]
        with patch.object(connections.GoogleConnection.objects, "filter", return_value=qs) as lookup, \
             patch.object(connections, "disconnect_gmail_for_user", return_value={"status": "disconnected"}) as gmail:
            response = self.call(connections.DisconnectView, "delete", {}, "gmail")
            self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(user=self.user, organization=self.org)
        gmail.assert_called_once_with(self.user, organization=self.org, connection_id=21, delete_derived_data=False)

    def test_api_key_connect_confirms_only_fresh_provider_status(self):
        for provider in ("luma", "humanitix"):
            for status_value in ("connected", "needs_reauth"):
                with self.subTest(provider=provider, status=status_value), ExitStack() as stack:
                    stack.enter_context(patch.object(connectors, "_org_scope_or_response", return_value=(self.org, None)))
                    stack.enter_context(patch.object(connectors, "_company_for_scope", return_value=self.company))
                    connect = stack.enter_context(patch.object(connectors, "connect_" + provider + "_connection"))
                    stack.enter_context(patch.object(connectors, "serialize_source_status", return_value={"sources": [{"key": provider, "status": status_value}]}))
                    label = {"accountLabel": "Sydney team"} if provider == "humanitix" else {}
                    response = self.call(connections.ConnectView, "post", {"companyId": COMPANY, "apiKey": "synthetic-test-key", **label}, provider)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data["connected"], status_value == "connected")
                    connect.assert_called_once_with(self.user, "synthetic-test-key", company=self.company,
                        **({"account_label": "Sydney team"} if provider == "humanitix" else {}))
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
        for provider, status_value, selected in (("slack", "syncing", True), ("google_drive", "connected", True), ("github", "connected", True)):
            self.assertFalse(prefs.source_capabilities({"key": provider, "status": status_value, "selected": selected})["usableForUpdates"])
        self.assertTrue(prefs.source_capabilities({"key": "slack", "status": "connected", "selected": True}, preferences={"slack": False})["usableForUpdates"])

    def test_default_inclusion_requires_working_connection_but_explicit_choice_survives_revoke(self):
        for status_value in ("not_connected", "unavailable", "needs_reauth", "expired", "error"):
            source = prefs.source_capabilities({"key": "slack", "status": status_value, "connectionId": 4})
            self.assertFalse(source["enabled"])
            self.assertFalse(source["usableForUpdates"])
        for status_value in ("connected", "syncing"):
            self.assertTrue(prefs.source_capabilities({"key": "slack", "status": status_value})["enabled"])
        self.assertTrue(prefs.source_capabilities({"key": "slack", "status": "not_connected"}, preferences={"slack": True})["enabled"])
        revoked = prefs.source_capabilities({"key": "slack", "status": "needs_reauth"}, preferences={"slack": True})
        self.assertTrue(revoked["enabled"])
        self.assertFalse(revoked["usableForUpdates"])

    def test_broken_connection_reads_as_reconnect_in_chat_vocabulary(self):
        for provider in ("gmail", "xero", "bank_feed"):
            source = prefs.source_capabilities({"key": provider, "status": "error", "connectionId": 4, "configured": True})
            self.assertEqual(source["status"], "needs_reauth")
            self.assertEqual(source["connectionStatus"], "error")
            self.assertTrue(source["needsReconnect"])
            self.assertTrue(source["canConnect"])
            self.assertTrue(source["canDisconnect"])
        healthy = prefs.source_capabilities({"key": "xero", "status": "connected", "connectionId": 4})
        self.assertFalse(healthy["needsReconnect"])
        self.assertNotIn("connectionStatus", healthy)

    def test_xero_with_several_organisations_needs_a_choice_before_use(self):
        source = prefs.source_capabilities({"key": "xero", "status": "connected", "connectionId": 4, "accountChoiceRequired": True})
        self.assertFalse(source["usableForUpdates"])
        self.assertEqual(source["status"], "connected")


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


def _token_response(status, payload):
    import json
    from requests import Response as HttpResponse
    value = HttpResponse()
    value.status_code = status
    value.url = "https://identity.example/token"
    value._content = json.dumps(payload).encode()
    return value


@override_settings(XERO_CLIENT_ID="synthetic", XERO_CLIENT_SECRET="synthetic",
    GOOGLE_OAUTH_CLIENT_ID="synthetic", GOOGLE_OAUTH_CLIENT_SECRET="synthetic",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ExpiredCredentialTests(SimpleTestCase):
    """Expired or revoked Xero and Google grants read as Reconnect, never "try again shortly"."""

    def connection(self, provider, **extra):
        from django.utils import timezone
        return Obj(pk=8, id=8, user_id=2, organization_id=4, provider=provider, refresh_token="synthetic-refresh",
            updated_at=timezone.now(), provider_metadata={}, status="connected", last_error="", **extra)

    def refresh(self, provider, response):
        from integrations.services import external_connectors as external
        refresh = (external._refresh_xero_connection_unlocked if provider == "xero"
            else external._refresh_google_analytics_connection)
        connection = self.connection(provider)
        with patch.object(external.requests, "post", return_value=response), \
                patch.object(external.ExternalServiceConnection, "objects") as rows:
            try:
                refresh(connection)
            except Exception as exc:  # noqa: BLE001 - the test inspects the raised contract
                return exc, connection, rows
        self.fail("refresh should not succeed")

    def test_invalid_grant_marks_xero_and_analytics_for_reconnect(self):
        from integrations.services import external_connectors as external
        from startup_updates.preflight import source_failure_check
        for provider, message in (("xero", external.XERO_RECONNECT_MESSAGE),
                                  ("google_analytics", external.GOOGLE_ANALYTICS_RECONNECT_MESSAGE)):
            with self.subTest(provider=provider):
                exc, connection, rows = self.refresh(provider, _token_response(400, {"error": "invalid_grant"}))
                self.assertIsInstance(exc, external.ConnectorReconnectRequired)
                self.assertEqual(str(exc), message)
                self.assertEqual(connection.status, "error")
                rows.filter.return_value.update.assert_called_once()
                self.assertEqual(rows.filter.return_value.update.call_args.kwargs["last_error"], message)
                self.assertIn(message, external.RECONNECT_MESSAGES[provider])
                self.assertEqual(source_failure_check(provider, exc)["status"], "needs_reconnect")

    def test_missing_refresh_token_also_needs_reconnect(self):
        from integrations.services import external_connectors as external
        connection = self.connection("xero")
        connection.refresh_token = ""
        with patch.object(external.ExternalServiceConnection, "objects"), \
                self.assertRaises(external.ConnectorReconnectRequired):
            external._refresh_xero_connection_unlocked(connection)

    def test_app_credential_or_outage_failures_stay_temporary(self):
        import requests
        from integrations.services import external_connectors as external
        for status, payload in ((400, {"error": "invalid_client"}), (503, {})):
            with self.subTest(status=status):
                exc, connection, rows = self.refresh("xero", _token_response(status, payload))
                self.assertIsInstance(exc, requests.HTTPError)
                self.assertEqual(connection.status, "connected")
                rows.filter.return_value.update.assert_not_called()

    def test_gmail_revoked_grant_reads_as_reconnect_until_reconnected(self):
        from google.auth.exceptions import RefreshError
        from integrations.services import gmail
        from startup_updates.preflight import source_failure_check
        connection = Obj(pk=5, user_id=2, refresh_token="synthetic-old")
        credentials = MagicMock()
        credentials.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked.", {})
        with patch.object(gmail, "get_refreshed_credentials", return_value=credentials):
            with self.assertRaises(gmail.GmailReconnectRequired) as raised:
                gmail.build_gmail_service(connection)
        self.assertEqual(source_failure_check("gmail", raised.exception)["status"], "needs_reconnect")
        self.assertTrue(gmail.gmail_needs_reconnect(connection))
        self.assertFalse(gmail.gmail_needs_reconnect(Obj(pk=5, user_id=2, refresh_token="synthetic-new")))
        credentials.refresh.side_effect = RefreshError("temporarily unavailable", {})
        with patch.object(gmail, "get_refreshed_credentials", return_value=credentials), self.assertRaises(RefreshError):
            gmail.build_gmail_service(Obj(pk=6, user_id=2, refresh_token="other"))
        self.assertFalse(gmail.gmail_needs_reconnect(Obj(pk=6, user_id=2, refresh_token="other")))

    def test_gmail_status_reports_needs_reauth_for_a_refused_grant(self):
        from integrations.services import external_connectors as external, gmail
        connection = Obj(pk=5, id=5, user_id=2, refresh_token="synthetic-old", google_email="founder@example.test")
        with patch.object(external, "google_connection_for_org", return_value=connection), \
                patch.object(external, "is_provider_configured", return_value=True), \
                patch.object(external, "gmail_scope_status_payload", return_value={"hasGmailScope": True}), \
                patch.object(external, "has_gmail_read_scope", return_value=True), \
                patch.object(external, "_scope_org", return_value=Obj(pk=4)), \
                patch.object(external.GoogleConnection, "objects"), \
                patch("integrations.services.source_accounts.serialize_accounts", return_value={}), \
                patch.object(gmail, "gmail_needs_reconnect", return_value=True):
            source = external._serialize_google_source(Obj(pk=2), Obj(pk=4))
        self.assertEqual(source["status"], "needs_reauth")
        self.assertEqual(source["warning"], gmail.GMAIL_REVOKED_WARNING)
        self.assertEqual(prefs.source_capabilities(source)["enabled"], False)


@override_settings(BASIQ_API_BASE_URL="https://basiq.example", BASIQ_CONSENT_UI_URL="https://consent.basiq.example")
class BankFeedConsentTests(SimpleTestCase):
    """A bank feed is not shown as connected until the founder consents at Basiq."""

    def start(self, rows):
        from integrations.services import external_connectors as external
        request = Obj(user=Obj(pk=7, id=7, email="founder@example.test"))
        organization = Obj(pk=9, id=9)
        with patch.object(external, "_basiq_token", return_value={"access_token": "synthetic"}), \
                patch.object(external.requests, "post") as post, \
                patch.object(external, "_save_state", return_value="state"), \
                patch.object(external.ExternalServiceConnection, "objects") as manager, \
                patch.object(external, "_upsert_connection") as upsert:
            post.return_value.json.return_value = {"id": "basiq-user"}
            manager.filter.return_value.exclude.return_value.order_by.return_value.__getitem__.return_value = rows
            manager.filter.return_value.order_by.return_value.first.return_value = rows[0] if rows else None
            url = external._build_basiq_consent_url(request, next_url="https://chat.example", organization=organization)
        return url, upsert.call_args.kwargs

    def test_first_consent_waits_disconnected(self):
        url, saved = self.start([])
        self.assertTrue(url.startswith("https://consent.basiq.example?"))
        self.assertEqual(saved["status"], "disconnected")
        self.assertTrue(saved["provider_metadata"]["consent_pending"])

    def test_abandoned_consent_reuses_basiq_user_and_stays_pending(self):
        pending = Obj(status="disconnected", external_account_id="basiq-user", provider_metadata={"consent_pending": True, "basiq_user": {"id": "basiq-user"}})
        url, saved = self.start([pending])
        self.assertIn("action=connect", url)
        self.assertEqual(saved["external_account_id"], "basiq-user")
        self.assertEqual(saved["status"], "disconnected")

    def test_reconsent_keeps_a_working_feed_connected(self):
        working = Obj(status="connected", external_account_id="basiq-user", provider_metadata={})
        _url, saved = self.start([working])
        self.assertEqual(saved["status"], "connected")
        self.assertFalse(saved["provider_metadata"]["consent_pending"])

    def test_consent_callback_clears_the_pending_marker(self):
        from integrations.services import external_connectors as external
        class Session(dict):
            modified = False
        request = Obj(user=Obj(pk=7, id=7), session=Session(), GET={})
        state = external._save_state(request, "bank_feed", "https://chat.example/my-startup/connections?company_id=" + COMPANY,
            {"organization_id": 9, "basiq_user_id": "basiq-user"})
        from django.test import RequestFactory
        callback = RequestFactory().get("/cb", {"state": state})
        callback.session, callback.user = Session(), request.user
        record = Obj(provider_metadata={"consent_pending": True}, status="disconnected", last_error="", save=MagicMock())
        with patch.object(external.Organization, "objects") as orgs, patch.object(external.ExternalServiceConnection, "objects") as rows:
            orgs.filter.return_value.first.return_value = Obj(pk=9, id=9)
            rows.filter.return_value.first.return_value = record
            external.complete_oauth_callback(callback, "bank_feed")
        self.assertEqual(record.status, "connected")
        self.assertFalse(record.provider_metadata["consent_pending"])
