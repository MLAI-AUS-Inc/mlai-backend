"""Migration-free unified OAuth audience, consent and tool-boundary regressions."""
import json
import time
from types import SimpleNamespace as Obj
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory

from startup_updates.mcp import config, oauth, registry, views
from .test_startup_mcp import COMPANY_ID, SETTINGS, grant


@override_settings(**{**SETTINGS, "MLAI_MCP_ENABLED": True})
class UnifiedOAuthTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = oauth.register_client({"redirect_uris": ["https://example.test/callback"]})
        self.query = {"client_id": self.client["client_id"], "redirect_uri": self.client["redirect_uris"][0],
            "response_type": "code", "resource": config.mlai_url(), "code_challenge": "x" * 43,
            "code_challenge_method": "S256", "state": "synthetic-client-state"}

    def test_omitted_host_scope_offers_explicit_consent_and_legacy_stays_narrow(self):
        self.assertEqual(set(oauth.create_intent(self.query)["scopes"]), config.ALL_SCOPES)
        self.assertEqual(oauth.create_intent({**self.query, "scope": "startup:brief:read"})["scopes"], ["startup:brief:read"])
        with self.assertRaises(oauth.OAuthError):
            oauth.create_intent({**self.query, "resource": config.mcp_url(), "scope": "article:publish"})
        self.assertEqual(set(oauth.create_intent({**self.query, "scope": "article:read community:read"})["scopes"]),
                         {"article:read", "community:read"})

    def test_downscoping_consent_cannot_expand_the_requested_permissions(self):
        intent = oauth.create_intent({**self.query, "scope": "startup:brief:read article:read"})
        user, session = Obj(pk=7, auth_version=3), Obj(pk="session", user_id=7)
        company = Obj(pk=COMPANY_ID)
        with patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "assert_device_owner"), patch.object(oauth, "company_for", return_value=company):
            for scopes in (["article:publish"], [], "article:read", [{}]):
                with self.subTest(scopes=scopes), self.assertRaises(ValidationError):
                    oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=COMPANY_ID, approve=True, scopes=scopes)
            url = oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=COMPANY_ID,
                                       approve=True, scopes=["article:read"])
        code = parse_qs(urlsplit(url).query)["code"][0]
        record = cache.get(oauth.key("code", code))
        approved = cache.get(oauth.key("grant", record["grant_id"]))
        self.assertEqual(approved["scopes"], ["article:read"])
        self.assertEqual(approved["resource"], config.mlai_url())
        self.assertEqual(approved["community_channels"], [])

    def test_community_only_consent_requires_exact_validated_channels_without_startup(self):
        from startup_updates.mcp import community
        intent = oauth.create_intent({**self.query, "scope": "community:read community:write"})
        user, session = Obj(pk=7, auth_version=3), Obj(pk="session", user_id=7)
        resources = [{"id": COMPANY_ID, "name": "Engineering", "relayUrl": "https://relay.example.test", "ownerPubkey": "a" * 64}]
        with patch.object(oauth, "_valid_session", return_value=True), patch.object(oauth, "assert_device_owner"), patch.object(oauth, "company_for") as company, patch.object(community, "validate_channel_selection", return_value=resources) as validate:
            with self.assertRaises(ValidationError):
                oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=None, approve=True)
            url = oauth.approve_intent(intent["requestId"], user=user, session=session, company_id=None,
                approve=True, scopes=["community:read"], community_channel_ids=[COMPANY_ID])
        company.assert_not_called()
        validate.assert_called_once_with(user, session, [COMPANY_ID])
        code = parse_qs(urlsplit(url).query)["code"][0]
        approved = cache.get(oauth.key("grant", cache.get(oauth.key("code", code))["grant_id"]))
        self.assertIsNone(approved["company_id"])
        self.assertEqual(approved["scopes"], ["community:read"])
        self.assertEqual(approved["community_channels"], resources)
        oauth.disconnect_company(user, None)
        self.assertTrue(cache.get(oauth.key("grant", approved["id"]))["revoked"])

    def test_access_and_refresh_are_bound_to_the_exact_resource(self):
        value = oauth.Principal(Obj(pk=7), grant(resource=config.mcp_url()))
        cache.set(oauth.key("access", "valley_access_test"), {"grant_id": "grant"})
        with patch.object(oauth, "valid_grant", return_value=value):
            with self.assertRaises(AuthenticationFailed):
                oauth.authenticate_token("valley_access_test", resource=config.mlai_url())
            self.assertIs(oauth.authenticate_token("valley_access_test", resource=config.mcp_url()), value)
            cache.set(oauth.key("refresh", "refresh"), {"grant_id": "grant", "client_id": self.client["client_id"]})
            with self.assertRaises(oauth.OAuthError) as error:
                oauth.token_exchange({"client_id": self.client["client_id"], "grant_type": "refresh_token",
                                      "refresh_token": "refresh", "resource": config.mlai_url()})
            self.assertEqual(error.exception.error, "invalid_target")
            self.assertNotIn("spent", cache.get(oauth.key("refresh", "refresh")))

    def test_community_outage_keeps_other_consent_capabilities_available(self):
        from community_chat.startups.agent_connections import AgentAuthorizationView, VibeRaisingCompany
        from startup_updates.mcp import community
        intent = oauth.create_intent(self.query)
        request = Obj(user=Obj(pk=7), auth=Obj(pk="session", user_id=7))
        company = Obj(pk=COMPANY_ID, name="Synthetic startup")
        with patch.object(VibeRaisingCompany.objects, "filter") as companies:
            companies.return_value.order_by.return_value = [company]
            for error in (PermissionDenied("Native device unavailable."), ValidationError("Native relay unavailable.")):
                with self.subTest(error=type(error).__name__), patch.object(community, "consent_channels", side_effect=error):
                    response = AgentAuthorizationView().get(request, intent["requestId"])
                    self.assertEqual(response.data["communityChannels"], [])
                    self.assertEqual(response.data["communityReason"], str(error.detail))
                    self.assertIn("article:read", response.data["scopes"])
                    self.assertEqual(response.data["companies"], [{"id": COMPANY_ID, "name": company.name}])
            with patch.object(community, "consent_channels", side_effect=AuthenticationFailed("Session expired.")):
                with self.assertRaises(AuthenticationFailed):
                    AgentAuthorizationView().get(request, intent["requestId"])

    def test_legacy_refresh_cannot_gain_new_permissions_even_when_unified_is_enabled(self):
        self.assertEqual(config.resource_scopes(config.mcp_url()), config.SCOPES)
        self.assertNotIn("community:write", config.resource_scopes(config.mcp_url()))
        self.assertIn("community:write", config.resource_scopes(config.mlai_url()))

    def test_metadata_and_catalogue_separate_new_and_legacy_audiences(self):
        factory = APIRequestFactory()
        legacy = views.ResourceMetadataView.as_view(throttle_classes=())(factory.get("/.well-known/oauth-protected-resource/mcp/valley"))
        unified = views.MlaiResourceMetadataView.as_view(throttle_classes=())(factory.get("/.well-known/oauth-protected-resource/mcp/mlai"))
        self.assertEqual(json.loads(legacy.content)["scopes_supported"], sorted(config.SCOPES))
        self.assertEqual(json.loads(unified.content)["resource"], config.mlai_url())
        self.assertEqual(json.loads(unified.content)["scopes_supported"], sorted(config.ALL_SCOPES))
        rows = config.client_catalog()
        self.assertEqual([item["id"] for item in rows], ["chatgpt", "claude", "codex", "cursor"])
        self.assertTrue(all(item["config"]["url"] == config.mlai_url() for item in rows))
        self.assertNotIn("Valley", json.dumps(rows))

    def test_canonical_endpoint_remains_independently_disabled(self):
        with override_settings(MLAI_MCP_ENABLED=False):
            with self.assertRaises(oauth.OAuthError):
                oauth.create_intent(self.query)
            response = views.MlaiMcpView.as_view(throttle_classes=())(APIRequestFactory().get("/mcp/mlai"))
            self.assertEqual(response.status_code, 503)
        self.assertEqual(views.McpView.as_view(throttle_classes=())(APIRequestFactory().get("/mcp/valley")).status_code, 401)

    def test_article_only_grant_can_discover_its_startup_without_pulse_permission(self):
        from startup_updates.mcp import tools
        value = oauth.Principal(Obj(pk=7), grant(resource=config.mlai_url(), scopes=["article:read"]))
        definitions = registry.catalogue(value)
        names = {item["name"] for item in definitions}
        self.assertIn("list_startups", names)
        self.assertNotIn("get_monthly_update_brief", names)
        company = Obj(pk=COMPANY_ID, name="Synthetic startup")
        with patch.object(tools, "valid_grant", return_value=value) as revalidate, patch.object(tools, "company_for", return_value=company):
            self.assertEqual(registry.call(value, "list_startups", {}), {"startups": [{"id": COMPANY_ID, "name": company.name}]})
        revalidate.assert_called_once_with(value.grant["id"], scope="article:read")
        value = oauth.Principal(Obj(pk=7), grant(resource=config.mlai_url(), scopes=["community:read"], company_id=None))
        self.assertNotIn("list_startups", {item["name"] for item in registry.catalogue(value)})

    def test_real_unified_catalogue_has_explicit_permissions_and_annotations(self):
        value = oauth.Principal(Obj(pk=7), grant(resource=config.mlai_url(), scopes=sorted(config.ALL_SCOPES)))
        definitions = registry.catalogue(value)
        self.assertEqual(len(definitions), 20)
        self.assertEqual(len({item["name"] for item in definitions}), 20)
        for item in definitions:
            with self.subTest(tool=item["name"]):
                self.assertTrue(set(item["securitySchemes"][0]["scopes"]) <= config.ALL_SCOPES)
                for flag in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                    self.assertIsInstance(item["annotations"][flag], bool)
        value = oauth.Principal(Obj(pk=7), grant())
        self.assertEqual(len(registry.catalogue(value)), 4)
        self.assertTrue(all(not item["name"].startswith(("generate_", "publish_", "post_")) for item in registry.catalogue(value)))

    def test_registry_enforces_permission_before_dispatch_and_advertises_oauth(self):
        module = Obj(TOOLS=[{"name": "synthetic_write", "inputSchema": {"type": "object"},
                            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}}],
                     TOOL_SCOPES={"synthetic_write": "community:write"})
        value = oauth.Principal(Obj(pk=7), grant(resource=config.mlai_url(), scopes=["community:read"]))
        with patch.object(registry, "modules", return_value=(module,)):
            self.assertEqual(registry.catalogue(value), [])
            with self.assertRaises(PermissionDenied):
                registry.call(value, "synthetic_write", {})
            value.grant["scopes"] = ["community:write"]
            definition = registry.catalogue(value)[0]
            self.assertEqual(definition["securitySchemes"], [{"type": "oauth2", "scopes": ["community:write"]}])
            self.assertEqual(definition["_meta"]["securitySchemes"], definition["securitySchemes"])
