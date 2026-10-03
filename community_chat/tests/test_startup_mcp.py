"""Database-free MCP protocol, OAuth boundary, and narrative-save regressions."""
import base64
import hashlib
import json
import time
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory

from community_chat.startups import agent_connections
from startup_updates.mcp import config, oauth, tools, views
from startup_updates.revisions import RevisionConflict

SETTINGS = {
    "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED": True, "VALLEY_MCP_ENABLED": True,
    "VALLEY_MCP_PUBLIC_BASE_URL": "https://api.example.test", "VALLEY_MCP_ALLOWED_ORIGINS": [],
    "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.example.test", "VALLEY_MCP_OAUTH_CLIENTS": {},
    "VALLEY_MCP_CLIENT_INSTALL_URLS": {}, "DEBUG": True,
}


COMPANY_ID = "04d89807-5884-4b0e-83cc-311abf13b648"
OTHER_COMPANY_ID = "7e399ce7-8d85-4970-ad8e-aa13bc3b028c"


def grant(**changes):
    return {"id": "grant", "user_id": 7, "auth_version": 3, "session_id": "session", "company_id": COMPANY_ID,
        "client_id": "client", "clientName": "Claude", "scopes": sorted(config.SCOPES), "resource": config.mcp_url(),
        "redirectOrigin": "https://claude.ai", "createdAt": timezone.now().isoformat(), "expiresAt": int(time.time()) + 3600, "revoked": False, **changes}


def principal():
    return oauth.Principal(user=Obj(pk=7, auth_version=3, is_active=True), grant=grant())


class DomainVerificationTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = views.DomainVerificationView.as_view(throttle_classes=())

    def test_exact_plaintext_token_is_public_without_enabling_agent_access(self):
        token = "synthetic_openai_domain_token_0123456789"
        with override_settings(VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN=token,
                              VALLEY_MCP_ENABLED=False, COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False):
            response = self.view(self.factory.get("/.well-known/openai-apps-challenge"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, token.encode())
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_unconfigured_or_combined_challenges_fail_closed(self):
        for token in ("", None, "short", "a" * 257, "first_token_0123456789\nsecond_token_0123456789",
                      '["synthetic_token_0123456789"]', {"token": "synthetic_token_0123456789"}):
            with self.subTest(token=token), override_settings(VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN=token):
                response = self.view(self.factory.get("/.well-known/openai-apps-challenge", HTTP_ACCEPT="text/plain"))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.content, b"")

    def test_challenge_cannot_be_updated_by_http(self):
        response = self.view(self.factory.post("/.well-known/openai-apps-challenge", {}, format="json", HTTP_ACCEPT="text/plain"))
        self.assertEqual(response.status_code, 405)

    def test_accept_headers_do_not_change_plaintext_ownership_proof(self):
        token = "synthetic_openai_domain_token_0123456789"
        for accept in ("text/plain", "*/*", "text/html", "application/json", "application/octet-stream"):
            with self.subTest(accept=accept), override_settings(VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN=token,
                    VALLEY_MCP_ENABLED=False, COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False):
                response = self.view(self.factory.get("/.well-known/openai-apps-challenge", HTTP_ACCEPT=accept))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, token.encode())
                self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
                self.assertEqual(response["Cache-Control"], "no-store")
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_plaintext_proof_still_enforces_throttle(self):
        view = views.DomainVerificationView.as_view()
        with patch.object(views.McpRateThrottle, "allow_request", return_value=False), patch.object(views.McpRateThrottle, "wait", return_value=1):
            response = view(self.factory.get("/.well-known/openai-apps-challenge", HTTP_ACCEPT="text/plain"))
        self.assertEqual(response.status_code, 429)

    def test_origin_root_challenge_route_is_registered(self):
        from django.urls import resolve
        match = resolve("/.well-known/openai-apps-challenge")
        self.assertIs(match.func.view_class, views.DomainVerificationView)


@override_settings(**SETTINGS)
class McpProtocolTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.view = views.McpView.as_view(throttle_classes=())

    def post(self, value, **headers):
        request = self.factory.post("/mcp/valley", value, format="json", HTTP_AUTHORIZATION="Bearer valley_access_test",
            HTTP_ACCEPT="application/json, text/event-stream", **headers)
        with patch.object(oauth, "authenticate_token", return_value=principal()):
            return self.view(request)

    def test_initialize_and_list_tools_are_streamable_http_json(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        value = json.loads(response.content)
        self.assertEqual(value["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("Mcp-Session-Id", response)
        response = self.post({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        names = [item["name"] for item in json.loads(response.content)["result"]["tools"]]
        self.assertEqual(names, ["list_startups", "get_monthly_update_brief", "save_narrative_draft", "get_draft_status"])
        self.assertFalse(any("publish" in name or "finance" in name for name in names))

    def test_notification_and_unknown_method(self):
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "publish"})
        self.assertEqual(json.loads(response.content)["error"]["code"], -32601)

    def test_batches_and_bool_ids_rejected(self):
        for payload in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], {"jsonrpc": "2.0", "id": True, "method": "ping"}):
            self.assertEqual(self.post(payload).status_code, 400)

    def test_tools_are_filtered_by_actual_grant_scope(self):
        value = principal()
        value.grant["scopes"] = ["startup:brief:read"]
        request = self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, format="json",
            HTTP_AUTHORIZATION="Bearer valley_access_test", HTTP_ACCEPT="application/json, text/event-stream")
        with patch.object(oauth, "authenticate_token", return_value=value):
            response = self.view(request)
        self.assertNotIn("save_narrative_draft", [item["name"] for item in json.loads(response.content)["result"]["tools"]])

    def test_unauthenticated_request_discovers_protected_resource(self):
        response = self.view(self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("/.well-known/oauth-protected-resource/mcp/valley", response["WWW-Authenticate"])

    def test_read_scope_rechecked_before_tool_call(self):
        with patch.object(tools, "valid_grant", side_effect=PermissionDenied("revoked scope")), patch.object(tools, "company_for") as company:
            with self.assertRaises(PermissionDenied):
                tools.call_tool(principal(), "get_monthly_update_brief", {"companyId": COMPANY_ID, "month": "2026-09"})
        company.assert_not_called()

    def test_tool_errors_are_mcp_results(self):
        with patch.object(tools, "call_tool", side_effect=ValidationError("financial fields forbidden")):
            response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "save_narrative_draft", "arguments": {}}})
        self.assertTrue(json.loads(response.content)["result"]["isError"])

    def test_missing_accept_or_wrong_origin_rejected(self):
        request = self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json",
            HTTP_AUTHORIZATION="Bearer valley_access_test")
        with patch.object(oauth, "authenticate_token", return_value=principal()):
            self.assertEqual(self.view(request).status_code, 406)
        request = self.factory.post("/mcp/valley", {}, format="json", HTTP_ORIGIN="https://attacker.test")
        self.assertEqual(self.view(request).status_code, 403)


@override_settings(**SETTINGS)
class OAuthTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = oauth.register_client({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude"})
        self.verifier = "x" * 43
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        self.query = {"client_id": self.client["client_id"], "redirect_uri": self.client["redirect_uris"][0], "response_type": "code",
            "resource": config.mcp_url(), "code_challenge": self.challenge, "code_challenge_method": "S256", "state": "opaque-client-state"}

    def test_pkce_resource_scope_and_exact_callback_required(self):
        for changes in ({"code_challenge_method": "plain"}, {"resource": "https://attacker.test/mcp"}, {"scope": "finance:write"}, {"redirect_uri": "https://claude.ai/api/mcp/auth_callback/"}, {"state": ""}):
            with self.assertRaises(oauth.OAuthError):
                oauth.create_intent({**self.query, **changes})
        self.assertIn("requestId", oauth.create_intent(self.query))

    def test_registration_rejects_unsafe_callbacks(self):
        for callback in ("https://user:secret@example.test/callback", "https://example.test/callback#token", "http://example.test/callback", "javascript:alert(1)", "https://[broken/callback"):
            with self.assertRaises(oauth.OAuthError):
                oauth.register_client({"redirect_uris": [callback]})
        oauth.register_client({"redirect_uris": ["http://localhost:3210/callback"]})

    def test_registration_rejects_malformed_grant_types_and_exposes_callback_origin(self):
        for grants in (3, [{}], "authorization_code"):
            with self.assertRaises(oauth.OAuthError):
                oauth.register_client({"redirect_uris": ["https://example.test/callback"], "grant_types": grants})
        value = oauth.create_intent(self.query)
        self.assertEqual(value["redirectOrigin"], "https://claude.ai")

    def test_registered_client_outlives_old_ninety_day_limit_without_extending_credentials(self):
        now = int(time.time())
        user = Obj(pk=7, auth_version=3)
        session = Obj(pk="session", user_id=7)
        with patch("time.time", return_value=now) as clock:
            client = oauth.register_client({"redirect_uris": self.client["redirect_uris"]})
            query = {**self.query, "client_id": client["client_id"]}
            intent = oauth.create_intent(query)
            with patch.object(oauth, "company_for", return_value=Obj(pk=UUID(COMPANY_ID))), patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"):
                callback = oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=COMPANY_ID, approve=True)
            code = parse_qs(urlsplit(callback).query)["code"][0]
            grant_id = cache.get(oauth.key("code", code))["grant_id"]
            value = cache.get(oauth.key("grant", grant_id))
            self.assertEqual(value["expiresAt"], now + 30 * 86400)
            with patch.object(oauth, "valid_grant", return_value=oauth.Principal(user=user, grant=value)):
                tokens = oauth.issue_tokens(grant_id)
            self.assertEqual(tokens["expires_in"], 3600)

            clock.return_value = now + 3601
            self.assertIsNone(cache.get(oauth.key("access", tokens["access_token"])))
            self.assertIsNotNone(cache.get(oauth.key("refresh", tokens["refresh_token"])))
            clock.return_value = now + 30 * 86400 + 1
            self.assertIsNone(cache.get(oauth.key("refresh", tokens["refresh_token"])))
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant(grant_id)

            clock.return_value = now + 91 * 86400
            self.assertEqual(oauth.client_for(client["client_id"]), client)
            resumed = oauth.create_intent(query)
            self.assertEqual(resumed["client_id"], client["client_id"])
            self.assertEqual(resumed["expiresAt"], clock.return_value + 600)

    def test_authorize_redirect_preserves_opaque_request_without_tokens(self):
        view = views.AuthorizeView.as_view(throttle_classes=())
        response = view(APIRequestFactory().get("/mcp/oauth/authorize", self.query))
        parsed = urlsplit(response["Location"])
        self.assertEqual(parsed.path, "/my-startup/connections")
        request_id = parse_qs(parsed.query)["mcpAuthorization"][0]
        self.assertNotIn("token", response["Location"])
        self.assertEqual(oauth.intent_for(request_id)["state"], "opaque-client-state")

    def test_token_and_revocation_reject_nonobject_json(self):
        for value in ([], 3, "token"):
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange(value)
        factory = APIRequestFactory()
        view = views.RevokeView.as_view(throttle_classes=())
        self.assertEqual(view(factory.post("/mcp/oauth/revoke", ["token"], format="json")).status_code, 400)

    def test_approval_code_pkce_exchange_and_replay(self):
        intent = oauth.create_intent(self.query)
        user = Obj(pk=7, auth_version=3)
        session = Obj(pk="session", user_id=7)
        with patch.object(oauth, "company_for", return_value=Obj(pk=UUID(COMPANY_ID))), patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"):
            url = oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=COMPANY_ID, approve=True)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["iss"], [config.public_base()])
        self.assertEqual(query["state"], ["opaque-client-state"])
        code = query["code"][0]
        exchange = {"grant_type": "authorization_code", "code": code, "client_id": self.client["client_id"], "redirect_uri": self.query["redirect_uri"],
            "resource": config.mcp_url(), "code_verifier": self.verifier}
        with patch.object(oauth, "valid_grant", return_value=principal()):
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange({**exchange, "code_verifier": "z" * 43})
            tokens = oauth.token_exchange(exchange)
            self.assertEqual(tokens["token_type"], "Bearer")
            self.assertIsNotNone(cache.get(oauth.key("access", tokens["access_token"])))
            self.assertIsNone(cache.get("valley-mcp:access:" + tokens["access_token"]))
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange(exchange)
        with self.assertRaises(ValidationError):
            oauth.intent_for(intent["requestId"])

    def test_deny_does_not_require_a_company(self):
        intent = oauth.create_intent(self.query)
        with patch.object(oauth, "company_for") as company, patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"):
            result = oauth.approve_intent(intent["requestId"], user=Obj(pk=7), session=Obj(user_id=7), company_id=None, approve=False)
        company.assert_not_called()
        self.assertEqual(parse_qs(urlsplit(result).query)["error"], ["access_denied"])
        with self.assertRaises(ValidationError):
            oauth.intent_for(intent["requestId"])

    def test_revoked_device_returns_authentication_failure(self):
        with patch.object(oauth, "_validate_device_owner", side_effect=oauth.InvalidAccountSession("revoked")):
            with self.assertRaises(AuthenticationFailed):
                oauth.assert_device_owner(Obj())

    def test_refresh_rotates_and_cannot_escalate(self):
        with patch.object(oauth, "valid_grant", return_value=principal()):
            value = oauth.issue_tokens("grant")
            request = {"grant_type": "refresh_token", "refresh_token": value["refresh_token"], "client_id": self.client["client_id"], "resource": config.mcp_url()}
            # Match the principal's registered client for this test grant.
            cache.set(oauth.key("refresh", value["refresh_token"]), {"grant_id": "grant", "client_id": self.client["client_id"]})
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange({**request, "scope": "finance:write"})
            self.assertNotEqual(oauth.token_exchange(request)["refresh_token"], value["refresh_token"])
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange(request)

    def test_refresh_reuse_revokes_rotated_family_only_for_the_bound_client(self):
        user = Obj(pk=7, auth_version=3, is_active=True)
        with patch.object(oauth, "get_user_model") as users, patch.object(oauth.CommunityChatAccountSession, "objects") as sessions, patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"), patch.object(oauth, "require_community_access"), patch.object(oauth, "company_for"):
            users.return_value.objects.filter.return_value.first.return_value = user
            sessions.select_related.return_value.filter.return_value.first.return_value = Obj()
            cache.set(oauth.key("grant", "grant"), grant(client_id=self.client["client_id"]))
            original = oauth.issue_tokens("grant")
            request = {"grant_type": "refresh_token", "refresh_token": original["refresh_token"],
                "client_id": self.client["client_id"], "resource": config.mcp_url()}
            rotated = oauth.token_exchange(request)
            self.assertTrue(cache.get(oauth.key("refresh", original["refresh_token"]))["spent"])
            other = oauth.register_client({"redirect_uris": ["https://other.example.test/callback"]})
            for rejected in ({**request, "client_id": other["client_id"]},
                             {**request, "refresh_token": "valley_refresh_unknown"}):
                with self.assertRaises(oauth.OAuthError):
                    oauth.token_exchange(rejected)
                self.assertFalse(cache.get(oauth.key("grant", "grant"))["revoked"])
                self.assertEqual(oauth.authenticate_token(rotated["access_token"]).user, user)
            with self.assertRaises(oauth.OAuthError):
                oauth.token_exchange(request)
            self.assertTrue(cache.get(oauth.key("grant", "grant"))["revoked"])
            for token in (original["access_token"], rotated["access_token"]):
                with self.assertRaises(AuthenticationFailed):
                    oauth.authenticate_token(token)
            with self.assertRaises(AuthenticationFailed):
                oauth.token_exchange({**request, "refresh_token": rotated["refresh_token"]})

    def test_revoked_grant_auth_version_and_session_deny_access(self):
        user = Obj(pk=7, auth_version=3, is_active=True)
        with patch.object(oauth, "get_user_model") as users, patch.object(oauth.CommunityChatAccountSession, "objects") as sessions, patch.object(oauth, "_valid_session", return_value=True) as valid, patch.object(oauth, "_validate_device_owner"), patch.object(oauth, "require_community_access"), patch.object(oauth, "company_for"):
            users.return_value.objects.filter.return_value.first.return_value = user
            sessions.select_related.return_value.filter.return_value.first.return_value = Obj()
            cache.set(oauth.key("grant", "grant"), grant())
            self.assertEqual(oauth.valid_grant("grant").user, user)
            user.auth_version = 4
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant("grant")
            user.auth_version = 3
            valid.return_value = False
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant("grant")
            valid.return_value = True
            oauth.revoke_grant("grant")
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant("grant")

    def test_wrong_owner_and_cross_startup_rejected_before_tools(self):
        with patch.object(oauth.VibeRaisingCompany, "objects") as companies:
            companies.select_related.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                oauth.company_for(principal().user, COMPANY_ID)
            self.assertEqual(companies.select_related.return_value.filter.call_args.kwargs["profile__user"].pk, 7)
            companies.reset_mock()
            with self.assertRaises(PermissionDenied):
                oauth.company_for(principal().user, OTHER_COMPANY_ID, grant())
            companies.select_related.assert_not_called()

    def test_model_uuid_company_approval_and_tool_round_trip(self):
        from organizations.models import Organization
        company = oauth.VibeRaisingCompany(id=UUID(COMPANY_ID), name="Synthetic reviewer startup",
            organization=Organization(pk=8, name="Synthetic reviewer startup", domain="review-fixture.invalid"))
        intent = oauth.create_intent(self.query)
        user = Obj(pk=7, auth_version=3, is_active=True)
        session = Obj(pk="session", user_id=7)
        with patch.object(oauth.VibeRaisingCompany, "objects") as companies, patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"):
            companies.select_related.return_value.filter.return_value.first.return_value = company
            callback = oauth.approve_intent(intent["requestId"], user=user, session=session,
                company_id=COMPANY_ID.upper(), approve=True)
            companies.select_related.return_value.filter.assert_called_with(pk=UUID(COMPANY_ID), profile__user=user)
            code = parse_qs(urlsplit(callback).query)["code"][0]
            grant_id = cache.get(oauth.key("code", code))["grant_id"]
            saved_grant = cache.get(oauth.key("grant", grant_id))
            self.assertEqual(saved_grant["company_id"], COMPANY_ID)
            self.assertEqual(cache.get(oauth.key("index", f"{user.pk}:{COMPANY_ID}")), [grant_id])
            with patch.object(oauth, "get_user_model") as users, patch.object(oauth.CommunityChatAccountSession, "objects") as sessions, patch.object(oauth, "require_community_access"):
                users.return_value.objects.filter.return_value.first.return_value = user
                sessions.select_related.return_value.filter.return_value.first.return_value = session
                value = oauth.valid_grant(grant_id)
                self.assertEqual(oauth.grants_for(user, COMPANY_ID.upper())[0]["id"], grant_id)
                self.assertEqual(tools.call_tool(value, "list_startups", {}),
                    {"startups": [{"id": COMPANY_ID, "name": company.name}]})
                with patch.object(tools.StartupProfile, "objects") as profiles:
                    profiles.filter.return_value.first.return_value = Obj(reporting_timezone="Australia/Melbourne", short_description="Fictional test material", stage="Idea")
                    brief = tools.call_tool(value, "get_monthly_update_brief", {"companyId": COMPANY_ID.upper(), "month": "2026-09"})
                self.assertEqual(brief["companyId"], COMPANY_ID)
                self.assertEqual(brief["reportingPeriod"]["timezone"], "Australia/Melbourne")
                with patch.object(tools.MonthlyUpdateDraft, "objects") as drafts:
                    draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision=Obj(pk=5, content_hash="revision", structured_memo={}), published_revision_id=None)
                    drafts.select_related.return_value.filter.return_value.first.return_value = draft
                    status = tools.call_tool(value, "get_draft_status", {"companyId": COMPANY_ID, "updateId": 10})
                self.assertEqual(status["companyId"], COMPANY_ID)
                self.assertEqual(parse_qs(urlsplit(status["reviewUrl"]).query)["company_id"], [COMPANY_ID])

    def test_invalid_company_ids_are_denied_before_ownership_query(self):
        with patch.object(oauth.VibeRaisingCompany, "objects") as companies:
            for company_id in (None, True, False, 9, "9", "not-a-uuid", {}, [], "04d89807-5884-4b0e-83cc-311abf13b648/other"):
                with self.subTest(company_id=company_id), self.assertRaises(PermissionDenied):
                    oauth.company_for(principal().user, company_id)
            companies.select_related.assert_not_called()

    def test_revoked_scope_rejected(self):
        user = Obj(pk=7, auth_version=3, is_active=True)
        cache.set(oauth.key("grant", "grant"), grant(scopes=["startup:brief:read"]))
        with patch.object(oauth, "get_user_model") as users, patch.object(oauth.CommunityChatAccountSession, "objects"), patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "_validate_device_owner"), patch.object(oauth, "require_community_access"), patch.object(oauth, "company_for"):
            users.return_value.objects.filter.return_value.first.return_value = user
            with self.assertRaises(PermissionDenied):
                oauth.valid_grant("grant", scope="startup:draft:write")


@override_settings(**SETTINGS)
class NarrativeTests(SimpleTestCase):
    def arguments(self, **changes):
        return {"companyId": COMPANY_ID, "month": "2026-09", "requestId": "8ec75f46-340a-46fb-9aa2-b99e8974d99b", "narrative": {"summary": "We shipped a launch."}, **changes}

    def test_financial_fields_and_nested_injection_rejected(self):
        for value in (self.arguments(metrics={"revenue": "999"}), self.arguments(financialSnapshot={}), self.arguments(publish=True),
            self.arguments(narrative={"summary": "Launch", "revenue": "999"}), self.arguments(sources=[{"provider": "gmail", "title": "Launch", "financial_snapshot": {}}])):
            with self.assertRaises(ValidationError):
                tools.validate_save(value)

    def test_sources_dates_and_request_identity_validated(self):
        for changes in ({"requestId": "retry"}, {"month": "2026-13"}, {"updateId": True}, {"narrative": {"summary": {"metric": "revenue"}}},
            {"sources": [{"provider": "gmail", "title": "Launch", "url": "https://user:token@example.test/"}]},
            {"sources": [{"provider": "gmail", "title": "Launch", "occurredAt": "2026-09-01T12:00:00"}]}):
            with self.assertRaises(ValidationError):
                tools.validate_save(self.arguments(**changes))
        with self.assertRaises(ValidationError):
            tools.validate_save(self.arguments(sources=[{"provider": "gmail", "title": "Launch", "url": "https://[broken/"}]))
        tools.validate_save(self.arguments(sources=[{"provider": "gmail", "title": "Launch", "url": "https://mail.google.com/mail/u/0/#inbox/1", "occurredAt": "2026-09-01T12:00:00Z"}]))

    def save_with_mocks(self, draft, arguments):
        company = Obj(pk=UUID(COMPANY_ID), name="Example", organization=Obj(pk=8))
        with ExitStack() as stack:
            stack.enter_context(patch("django.contrib.auth.get_user_model"))
            stack.enter_context(patch.object(tools, "valid_grant", return_value=principal()))
            stack.enter_context(patch.object(tools, "company_for", return_value=company))
            stack.enter_context(patch.object(tools, "_brief", return_value={}))
            stack.enter_context(patch.object(tools, "resolve_update", return_value=(draft, False)))
            save = stack.enter_context(patch.object(tools, "save_revision"))
            capture = stack.enter_context(patch.object(tools, "capture_snapshot"))
            result = tools.save_draft.__wrapped__(principal(), arguments)
            return result, save, capture

    def test_replay_is_content_sensitive_and_preserves_financial_snapshot(self):
        arguments = self.arguments()
        draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision_id=5,
            current_revision=Obj(pk=5, content_hash="revision", validation={}, structured_memo={"_agent_requests": {arguments["requestId"]: {"hash": tools.content_hash(arguments), "user_id": 7}}}),
            refresh_from_db=MagicMock(), title="title", save=MagicMock(), revisions=MagicMock())
        result, saved, captured = self.save_with_mocks(draft, arguments)
        self.assertTrue(result["replayed"])
        saved.assert_not_called()
        captured.assert_not_called()
        with self.assertRaises(RevisionConflict):
            self.save_with_mocks(draft, self.arguments(narrative={"summary": "different"}))

    def test_receipt_survives_human_edit_via_historical_revision(self):
        arguments = self.arguments()
        draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision_id=6,
            current_revision=Obj(pk=6, content_hash="human", validation={}, structured_memo={"summary": "Human edited"}), revisions=MagicMock())
        draft.revisions.filter.return_value.order_by.return_value.first.return_value = Obj(structured_memo={"_agent_requests": {
            arguments["requestId"]: {"hash": tools.content_hash(arguments), "user_id": 7}}})
        result, saved, _ = self.save_with_mocks(draft, arguments)
        self.assertTrue(result["replayed"])
        saved.assert_not_called()
        self.assertIn("view=compose", result["reviewUrl"])
        self.assertIn("/my-startup/updates?", result["reviewUrl"])

    def test_reused_creation_key_cannot_switch_reporting_month(self):
        draft = Obj(month=date(2026, 8, 1))
        with self.assertRaises(ValidationError):
            self.save_with_mocks(draft, self.arguments())

    def test_existing_revision_requires_match_and_saves_private_unverified_sources(self):
        snapshot = Obj(payload={"metrics": [{"key": "revenue", "value": "123"}]})
        draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision_id=5,
            current_revision=Obj(pk=5, content_hash="revision", validation={"groundedness_status": "passed"}, snapshot=snapshot, structured_memo={"financial_snapshot": {"income": 123}}),
            refresh_from_db=MagicMock(), title="title", save=MagicMock(), revisions=MagicMock())
        draft.revisions.filter.return_value.order_by.return_value.first.return_value = None
        with self.assertRaises(RevisionConflict):
            self.save_with_mocks(draft, self.arguments())
        _, saved, captured = self.save_with_mocks(draft, self.arguments(expectedRevision=5))
        captured.assert_not_called()
        self.assertIs(saved.call_args.kwargs["snapshot"], snapshot)
        self.assertEqual(saved.call_args.kwargs["audience"], "private")
        self.assertEqual(saved.call_args.kwargs["validation"]["groundedness_status"], "needs_review")
        self.assertEqual(saved.call_args.args[1]["financial_snapshot"], {"income": 123})
        self.assertEqual(saved.call_args.args[1]["_agent_provenance"]["kind"], "agent_supplied")

    def test_partial_legacy_edit_preserves_stored_narrative_without_mutating_it(self):
        legacy = {"summary": "Previous summary", "highlights": ["Existing launch"],
            "lowlights": ["Existing risk"], "asks": ["Founder introduction"],
            "learnings": ["Existing learning"], "next_30_days": ["Existing plan"],
            "display_config": {"full_metric_keys": ["revenue"]}}
        draft = tools.MonthlyUpdateDraft(pk=10, organization_id=8, month=date(2026, 9, 1),
            structured_memo=legacy)
        draft.refresh_from_db, draft.save = MagicMock(), MagicMock()
        with patch.object(tools.MonthlyUpdateDraft, "objects") as drafts:
            drafts.select_for_update.return_value.filter.return_value.first.return_value = draft
            _, saved, captured = self.save_with_mocks(draft, self.arguments(updateId=10))
        memo = saved.call_args.args[1]
        self.assertEqual(memo["summary"], "We shipped a launch.")
        for field in ("highlights", "lowlights", "asks", "learnings", "next_30_days", "display_config"):
            self.assertEqual(memo[field], legacy[field])
            self.assertIsNot(memo[field], legacy[field])
        self.assertEqual(legacy["summary"], "Previous summary")
        self.assertNotIn("_agent_requests", legacy)
        self.assertEqual(saved.call_args.kwargs["audience"], "private")
        captured.assert_called_once()
        self.assertIs(saved.call_args.kwargs["snapshot"], captured.return_value)

    def test_legacy_carried_claims_cannot_bypass_unresolved_review(self):
        from startup_updates import revisions
        for status in ("failed", "pending", "needs_review"):
            for field in ("highlights", "topline", "operations", "financial_performance",
                          "title", "concise_analysis", "conciseAnalysis"):
                claim = ["Unresolved carried claim"] if field in {"highlights", "operations", "financial_performance"} else "Unresolved carried claim"
                legacy = {field: claim, "_month_sequence": 1}
                draft = tools.MonthlyUpdateDraft(pk=10, organization_id=8, month=date(2026, 9, 1),
                    structured_memo=legacy, groundedness_status=status)
                draft.refresh_from_db, draft.save = MagicMock(), MagicMock()
                with self.subTest(status=status, field=field):
                    _, saved, _ = self.save_with_mocks(draft, self.arguments())
                    self.assertEqual(saved.call_args.kwargs["validation"], {"groundedness_status": status})
                    memo = saved.call_args.args[1]
                    self.assertEqual(memo[field], legacy[field])
                    self.assertEqual(memo["_agent_provenance"]["kind"], "agent_supplied")
                    imported = revisions.MonthlyUpdateRevision(pk=6, draft=draft, content_hash="new-hash", validation=saved.call_args.kwargs["validation"],
                        structured_memo={**memo, "_audience_visibility": ["just_me"]})
                    draft.current_revision = imported
                    with patch.object(revisions.MonthlyUpdateDraft, "objects") as drafts:
                        drafts.select_for_update.return_value.get.return_value = draft
                        with self.assertRaises(ValidationError):
                            revisions.approve_and_publish.__wrapped__(draft, actor=Obj(pk=7), revision_id=6,
                                revision_hash="new-hash", audience_visibility=["just_me"], reviewed_agent_claims=True)

    def test_new_and_resolved_legacy_drafts_require_normal_agent_review(self):
        for memo, status in (({"_month_sequence": 1}, "pending"),
                             ({"highlights": ["Verified launch"]}, "passed"),
                             ({"highlights": ["Founder supplied launch"]}, "founder_asserted")):
            draft = tools.MonthlyUpdateDraft(pk=10, organization_id=8, month=date(2026, 9, 1),
                structured_memo=memo, groundedness_status=status)
            draft.refresh_from_db, draft.save = MagicMock(), MagicMock()
            with self.subTest(memo=memo, status=status):
                _, saved, _ = self.save_with_mocks(draft, self.arguments())
                self.assertEqual(saved.call_args.kwargs["validation"], {"groundedness_status": "needs_review",
                    "provenance": "agent_supplied", "source_verification": "unverified_external_agent"})
                self.assertEqual(saved.call_args.kwargs["audience"], "private")

    def test_import_cannot_retag_unresolved_carried_claims_for_agent_only_review(self):
        from startup_updates import revisions
        for status in ("failed", "pending", "needs_review"):
            prior_validation = {"groundedness_status": status, "provenance": "valley_verifier",
                "source_verification": "valley_claims", "failed_claims": ["carried-highlight"]}
            draft = Obj(pk=10, month=date(2026, 9, 1), status="draft", current_revision_id=5,
                current_revision=Obj(pk=5, content_hash="revision", validation=prior_validation,
                    snapshot=Obj(payload={}), structured_memo={"highlights": ["Unresolved carried claim"]}),
                refresh_from_db=MagicMock(), title="title", save=MagicMock(), revisions=MagicMock())
            draft.revisions.filter.return_value.order_by.return_value.first.return_value = None
            with self.subTest(status=status):
                _, saved, _ = self.save_with_mocks(draft, self.arguments(expectedRevision=5))
                self.assertEqual(saved.call_args.kwargs["validation"], prior_validation)
                memo = saved.call_args.args[1]
                self.assertEqual(memo["highlights"], ["Unresolved carried claim"])
                self.assertEqual(memo["_agent_provenance"]["kind"], "agent_supplied")
                imported = Obj(pk=6, content_hash="new-hash", validation=saved.call_args.kwargs["validation"],
                    structured_memo={**memo, "_audience_visibility": ["just_me"]})
                draft.current_revision = imported
                with patch.object(revisions.MonthlyUpdateDraft, "objects") as drafts:
                    drafts.select_for_update.return_value.get.return_value = draft
                    with self.assertRaises(ValidationError):
                        revisions.approve_and_publish.__wrapped__(draft, actor=Obj(pk=7), revision_id=6,
                            revision_hash="new-hash", audience_visibility=["just_me"], reviewed_agent_claims=True)


@override_settings(**SETTINGS)
class ConnectionConfigurationTests(SimpleTestCase):
    def test_metadata_and_install_links_contain_public_urls_only(self):
        self.assertTrue(oauth.metadata()["authorization_response_iss_parameter_supported"])
        rows = config.client_catalog()
        claude = next(item for item in rows if item["id"] == "claude")
        self.assertIn("connectorUrl=", claude["installUrl"])
        cursor = next(item for item in rows if item["id"] == "cursor")
        self.assertIn("/link/mcp/install", cursor["installUrl"])
        encoded = parse_qs(urlsplit(cursor["installUrl"]).query)["config"][0]
        self.assertEqual(json.loads(base64.b64decode(encoded)), {"url": config.mcp_url()})
        self.assertEqual([item["id"] for item in rows], ["claude", "codex", "cursor"])
        codex = next(item for item in rows if item["id"] == "codex")
        self.assertIsNone(codex["installUrl"])
        self.assertEqual(codex["setupUrl"], "https://learn.chatgpt.com/docs/extend/mcp?surface=cli")
        self.assertIn("Streamable HTTP", " ".join(codex["instructions"]))
        self.assertNotIn("token", json.dumps(rows))

    def test_codex_reuses_legacy_chatgpt_listing_without_duplicate_option(self):
        listing = "https://chatgpt.com/plugins/approved-openai-plugin?open_in_app"
        with override_settings(VALLEY_MCP_CLIENT_INSTALL_URLS={"chatgpt": listing}):
            rows = config.client_catalog()
        codex = next(item for item in rows if item["id"] == "codex")
        self.assertEqual(codex["name"], "Codex")
        self.assertEqual(codex["installUrl"], listing)
        self.assertEqual(codex["method"], "directory")
        self.assertIn("choose Install", " ".join(codex["instructions"]))
        self.assertNotIn("chatgpt", [item["id"] for item in rows])

    def test_codex_listing_override_takes_precedence_over_legacy_chatgpt(self):
        current = "https://chatgpt.com/plugins/approved-codex-plugin?open_in_app"
        with override_settings(VALLEY_MCP_CLIENT_INSTALL_URLS={
            "codex": current,
            "chatgpt": "https://chatgpt.com/plugins/previous-plugin",
        }):
            codex = next(item for item in config.client_catalog() if item["id"] == "codex")
        self.assertEqual(codex["installUrl"], current)

    def test_enabled_requires_public_https_and_shared_cache_in_production(self):
        with override_settings(VALLEY_MCP_PUBLIC_BASE_URL="http://api.example.test"):
            self.assertFalse(config.availability()[0])
        with override_settings(DEBUG=False):
            self.assertFalse(config.availability()[0])

    def test_disconnect_epoch_revokes_hidden_grants_with_a_stuck_index_lock(self):
        cache.clear()
        cache.set(oauth.key("grant", "hidden"), grant(id="hidden"))
        cache.set(oauth.key("lock-index", f"7:{COMPANY_ID}"), "crashed-process")
        oauth.disconnect_company(Obj(pk=7), COMPANY_ID.upper())
        with self.assertRaises(AuthenticationFailed):
            oauth.valid_grant("hidden")

    def test_disconnect_revokes_only_selected_company_grants(self):
        cache.clear()
        cache.set(oauth.key("index", f"7:{COMPANY_ID}"), ["own", "foreign"])
        cache.set(oauth.key("grant", "own"), grant(id="own"))
        cache.set(oauth.key("grant", "foreign"), grant(id="foreign", company_id=OTHER_COMPANY_ID))
        view = agent_connections.AgentConnectionView()
        view.company = Obj(pk=UUID(COMPANY_ID))
        response = view.delete(Obj(user=Obj(pk=7)))
        self.assertTrue(response.data["disconnected"])
        self.assertTrue(cache.get(oauth.key("grant", "own"))["revoked"])
        self.assertFalse(cache.get(oauth.key("grant", "foreign"))["revoked"])


@override_settings(**SETTINGS)
class FounderAgentReviewTests(SimpleTestCase):
    def revision(self, status="needs_review", **validation):
        return Obj(pk=5, content_hash="exact-hash", structured_memo={
            "_audience_visibility": ["community"], "_agent_provenance": {"kind": "agent_supplied"}},
            snapshot=Obj(payload={"period": {"timezone": "UTC"}}),
            validation={"groundedness_status": status, "provenance": "agent_supplied",
                "source_verification": "unverified_external_agent", **validation})

    def approve(self, revision, *, reviewed=True, revision_id=5, revision_hash="exact-hash"):
        from startup_updates import revisions
        draft = Obj(pk=10, current_revision=revision, published_revision_id=None, published_at=None,
            month=date(2025, 1, 1), ready_at=None, first_published_at=None, save=MagicMock())
        with patch.object(revisions.MonthlyUpdateDraft, "objects") as drafts, patch.object(revisions.MonthlyUpdateApproval, "objects") as approvals:
            drafts.select_for_update.return_value.get.return_value = draft
            approvals.get_or_create.return_value = (Obj(), True)
            result = revisions.approve_and_publish.__wrapped__(draft, actor=Obj(pk=7), revision_id=revision_id,
                revision_hash=revision_hash, audience_visibility=["community"], reviewed_agent_claims=reviewed)
        return result, approvals

    def test_unreviewed_agent_narrative_cannot_publish(self):
        with self.assertRaises(ValidationError):
            self.approve(self.revision(), reviewed=False)

    def test_exact_explicit_review_records_human_approval_of_agent_narrative(self):
        result, approvals = self.approve(self.revision())
        self.assertEqual(result.published_revision.pk, 5)
        self.assertEqual(approvals.get_or_create.call_args.kwargs["defaults"]["actor"].pk, 7)
        # Approval never relabels source references as provider-verified evidence.
        self.assertEqual(result.published_revision.validation["source_verification"], "unverified_external_agent")

    def test_stale_revision_and_other_verifier_failures_remain_blocked(self):
        for kwargs in ({"revision_id": 4}, {"revision_hash": "stale"}):
            with self.assertRaises(RevisionConflict):
                self.approve(self.revision(), **kwargs)
        for status in ("failed", "pending"):
            with self.assertRaises(ValidationError):
                self.approve(self.revision(status))
        with self.assertRaises(ValidationError):
            self.approve(self.revision(source_verification="valley_verifier_failed"))
