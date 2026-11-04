"""Database-free regressions for Dot's fresh opt-in on the unified MCP.

These fixtures exercise real consent, catalogue and authority decisions without
opening a database, contacting a relay or claiming a live OpenAI pilot result.
Run only through ``scripts/test_without_database.py``.
"""

import base64
import hashlib
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from coincurve import PrivateKey
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory

from startup_updates.mcp import config, oauth, registry, views
from .test_startup_mcp import SETTINGS, grant


DOT_SCOPES = frozenset({"agent:mentions:listen", "agent:thread:read", "agent:thread:reply"})
PILOT_SETTINGS = {
    **SETTINGS,
    "MLAI_MCP_ENABLED": True,
    "MLAI_MCP_NATIVE_AGENTS_ENABLED": True,
    "AGENT_RUNTIME_ENABLED": True,
    "AGENT_RUNTIME_NATIVE_ENABLED": True,
    "AGENT_RUNTIME_NATIVE_REPLIES_ENABLED": True,
    "AGENT_RUNTIME_MCP_EVENTS_ENABLED": True,
}


def account_principal(**changes):
    """An account-only unified connection, retaining the actual OAuth client."""
    return oauth.Principal(
        Obj(pk=7, auth_version=3, is_active=True, email="member@example.test", full_name="Synthetic Member"),
        grant(**{"resource": config.mlai_url(), "company_id": None, "scopes": sorted(DOT_SCOPES), **changes}),
    )


def native_authority(value=None):
    """Persisted runtime authority and its private unified credential fixture."""
    from agent_runtime.native_installations import DOT_SCOPES as runtime_scopes
    from agent_runtime.native_crypto import public_key

    value = value or account_principal()
    now = timezone.now()
    owner_key, bot_key = PrivateKey(b"\x01" * 32), PrivateKey(b"\x02" * 32)
    device = Obj(pk=uuid4(), public_key=public_key(owner_key), user_id=value.user.pk)
    conditions = f"created_at<{int((now + timedelta(days=1)).timestamp())}"
    attestation_hash = hashlib.sha256(
        f"nostr:agent-auth:{public_key(bot_key)}:{conditions}".encode()).digest()
    installation = Obj(
        pk=uuid4(), owner=value.user, owner_id=value.user.pk, owner_device=device,
        owner_device_id=device.pk, app_id="openai-dot", runtime="external",
        community_id=uuid4(), is_active=True, state="active", public_key=public_key(bot_key),
        name="My synthetic Dot", sealed_signing_key="synthetic-sealed-pilot-key",
        owner_attestation=["auth", device.public_key, conditions, owner_key.sign_schnorr(attestation_hash).hex()],
    )
    native = Obj(
        pk=uuid4(), installation=installation, installation_id=installation.pk,
        generation=2, scopes=list(runtime_scopes), revoked_at=None,
        expires_at=now + timedelta(hours=1),
        max_concurrent_tasks=1, daily_task_limit=20,
        daily_cost_limit_microunits=1000000, max_task_seconds=300,
        resources=[f"community:{installation.community_id}:channel:{uuid4()}"],
    )
    credential = Obj(
        pk=uuid4(), grant=native, grant_id=native.pk, generation=native.generation,
        scopes=list(runtime_scopes), kind="access", audience=config.mlai_url(),
        revoked_at=None, expires_at=now + timedelta(minutes=15), task_id=None,
        task=None, token_hash="d" * 64,
        save=MagicMock(),
        oauth_binding=Obj(client_id="unified-mcp:" + value.grant["id"]),
    )
    value.grant.update({"native_agent_grant_id": str(native.pk),
                        "native_agent_credential_id": str(credential.pk)})
    return value, native, credential


@override_settings(**PILOT_SETTINGS)
class NativeConsentBoundaryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = oauth.register_client({
            "redirect_uris": ["https://host.example.test/callback"],
            "client_name": "An actual dynamically registered host",
        })
        self.query = {
            "client_id": self.client["client_id"],
            "redirect_uri": self.client["redirect_uris"][0],
            "response_type": "code",
            "resource": config.mlai_url(),
            "scope": " ".join(sorted(DOT_SCOPES)),
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
            "state": "synthetic-client-state",
        }
        self.user = Obj(pk=7, auth_version=3, is_active=True)
        self.session = Obj(pk="session", user_id=7)
        self.enterContext(patch.object(oauth, "_valid_session", return_value=True))
        self.enterContext(patch.object(oauth, "assert_device_owner"))

    def test_valley_scope_boundary_rejects_every_native_permission(self):
        self.assertEqual(config.resource_scopes(config.mcp_url()), config.SCOPES)
        self.assertFalse(DOT_SCOPES & config.resource_scopes(config.mcp_url()))
        self.assertTrue(DOT_SCOPES <= config.resource_scopes(config.mlai_url()))
        for scope in DOT_SCOPES:
            with self.subTest(scope=scope), self.assertRaises(oauth.OAuthError) as caught:
                oauth.create_intent({**self.query, "resource": config.mcp_url(), "scope": scope})
            self.assertEqual(caught.exception.error, "invalid_scope")

    def test_native_consent_requires_the_complete_triple_and_explicit_installation(self):
        intent = oauth.create_intent(self.query)
        with patch.object(oauth, "company_for") as company:
            for selected in (None, [], *([scope] for scope in DOT_SCOPES), sorted(DOT_SCOPES)):
                with self.subTest(selected=selected), self.assertRaises(ValidationError):
                    oauth.approve_intent(
                        intent["requestId"], user=self.user, session=self.session,
                        company_id=None, approve=True, scopes=selected,
                    )
        company.assert_not_called()
        self.assertIsNotNone(cache.get(oauth.key("intent", intent["requestId"])))

    def test_explicit_host_request_and_registered_identity_are_retained(self):
        intent = oauth.create_intent(self.query)
        self.assertEqual(intent["client_id"], self.client["client_id"])
        self.assertEqual(intent["clientName"], self.client["client_name"])
        self.assertEqual(intent["scopes"], sorted(DOT_SCOPES))
        self.assertNotIn("app_id", self.client)
        self.assertNotIn("app_id", intent)
        limited = oauth.create_intent({**self.query, "scope": "article:read"})
        self.assertEqual(limited["scopes"], ["article:read"])

    def test_native_permissions_do_not_change_legacy_metadata_or_oauth_prefixes(self):
        factory = APIRequestFactory()
        legacy = views.ResourceMetadataView.as_view(throttle_classes=())(
            factory.get("/.well-known/oauth-protected-resource/mcp/valley"))
        unified = views.MlaiResourceMetadataView.as_view(throttle_classes=())(
            factory.get("/.well-known/oauth-protected-resource/mcp/mlai"))
        self.assertEqual(json.loads(legacy.content)["scopes_supported"], sorted(config.SCOPES))
        self.assertTrue(DOT_SCOPES <= set(json.loads(unified.content)["scopes_supported"]))
        value = account_principal(client_id=self.client["client_id"])
        with patch.object(oauth, "valid_grant", return_value=value):
            tokens = oauth.issue_tokens(value.grant["id"])
        self.assertTrue(tokens["access_token"].startswith("valley_access_"))
        self.assertTrue(tokens["refresh_token"].startswith("valley_refresh_"))
        self.assertEqual(cache.get(oauth.key("refresh", tokens["refresh_token"]))["client_id"],
                         self.client["client_id"])
        self.assertEqual(oauth.PREFIX, "valley-mcp:")

    def test_refresh_of_an_existing_unified_connection_cannot_opt_it_into_dot(self):
        value = account_principal(client_id=self.client["client_id"], scopes=["community:read"])
        old_refresh = "valley_refresh_synthetic-existing-connection"
        cache.set(oauth.key("refresh", old_refresh), {
            "grant_id": value.grant["id"], "client_id": self.client["client_id"],
        })
        request = {
            "grant_type": "refresh_token", "client_id": self.client["client_id"],
            "refresh_token": old_refresh, "resource": config.mlai_url(),
        }
        with patch.object(oauth, "valid_grant", return_value=value):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth.token_exchange({**request, "scope": " ".join(sorted(DOT_SCOPES))})
            self.assertEqual(caught.exception.error, "invalid_scope")
            self.assertNotIn("spent", cache.get(oauth.key("refresh", old_refresh)))
            refreshed = oauth.token_exchange(request)
        self.assertEqual(refreshed["scope"], "community:read")
        self.assertEqual(value.grant["scopes"], ["community:read"])
        self.assertNotIn("native_agent_grant_id", value.grant)
        self.assertNotIn("native_agent_credential_id", value.grant)
        self.assertEqual(cache.get(oauth.key("access", refreshed["access_token"])),
                         {"grant_id": value.grant["id"]})

    def test_fresh_native_consent_keeps_real_client_and_account_only_subject(self):
        from startup_updates.mcp import native_agents

        intent = oauth.create_intent(self.query)
        installation_id, native_grant_id, credential_id = map(str, (uuid4(), uuid4(), uuid4()))

        def bind(user, session, value, selected_installation):
            self.assertIs(user, self.user)
            self.assertIs(session, self.session)
            self.assertEqual(selected_installation, installation_id)
            value.update(native_agent_grant_id=native_grant_id, native_agent_credential_id=credential_id)

        with (
            patch.object(oauth, "company_for") as company,
            patch.object(native_agents, "consent_fence", side_effect=lambda user: nullcontext()),
            patch.object(native_agents, "bind_native_grant", side_effect=bind) as binding,
        ):
            callback = oauth.approve_intent(
                intent["requestId"], user=self.user, session=self.session, company_id=None,
                approve=True, scopes=sorted(DOT_SCOPES), native_installation_id=installation_id,
            )
        company.assert_not_called()
        binding.assert_called_once()
        code = parse_qs(urlsplit(callback).query)["code"][0]
        record = cache.get(oauth.key("code", code))
        approved = cache.get(oauth.key("grant", record["grant_id"]))
        self.assertIsNone(approved["company_id"])
        self.assertEqual(approved["client_id"], self.client["client_id"])
        self.assertEqual(record["client_id"], self.client["client_id"])
        self.assertEqual(approved["scopes"], sorted(DOT_SCOPES))
        self.assertEqual(approved["native_agent_grant_id"], native_grant_id)
        self.assertEqual(approved["native_agent_credential_id"], credential_id)
        self.assertIsNone(cache.get(oauth.key("intent", intent["requestId"])))
        self.assertNotIn("access_token", approved)
        self.assertNotIn("token_hash", approved)

    def test_failed_native_enrollment_cannot_leave_an_authorization_code(self):
        from startup_updates.mcp import native_agents

        intent = oauth.create_intent(self.query)
        with (
            patch.object(native_agents, "consent_fence", side_effect=lambda user: nullcontext()),
            patch.object(native_agents, "bind_native_grant", side_effect=PermissionDenied("Wrong installation.")),
        ):
            with self.assertRaises(PermissionDenied):
                oauth.approve_intent(
                    intent["requestId"], user=self.user, session=self.session, company_id=None,
                    approve=True, scopes=sorted(DOT_SCOPES), native_installation_id=str(uuid4()),
                )
        self.assertEqual(cache.get(oauth.key("intent", intent["requestId"]))["client_id"], self.client["client_id"])
        self.assertIsNone(cache.get(oauth.key("index", f"{self.user.pk}:account")))

    def test_tokens_issued_at_consent_do_not_activate_native_authority_until_used(self):
        from startup_updates.mcp import native_agents

        value = account_principal(client_id=self.client["client_id"])
        with (
            patch.object(oauth, "valid_grant", return_value=value),
            patch.object(native_agents, "on_authenticated") as activate,
        ):
            tokens = oauth.issue_tokens(value.grant["id"])
            activate.assert_not_called()
            with self.assertRaises(AuthenticationFailed):
                oauth.authenticate_token(tokens["access_token"], resource=config.mcp_url())
            activate.assert_not_called()
            accepted = oauth.authenticate_token(tokens["access_token"], resource=config.mlai_url())
        self.assertIs(accepted, value)
        activate.assert_called_once_with(value)


@override_settings(**PILOT_SETTINGS)
class NativeCatalogueBoundaryTests(SimpleTestCase):
    def test_existing_unified_grants_keep_the_original_twenty_tools(self):
        value = oauth.Principal(Obj(pk=7), grant(
            resource=config.mlai_url(), scopes=sorted(config.ALL_SCOPES - DOT_SCOPES)))
        definitions = registry.catalogue(value)
        self.assertEqual(len(definitions), 20)
        names = {item["name"] for item in definitions}
        self.assertEqual(len(names), 20)
        self.assertNotIn("get_agent_account", names)
        self.assertNotIn("reply_to_invocation", names)

    def test_legacy_registry_rejects_native_tools_even_with_forged_scopes(self):
        value = oauth.Principal(Obj(pk=7), grant(scopes=sorted(config.SCOPES | DOT_SCOPES)))
        self.assertNotIn("get_agent_account", {item["name"] for item in registry.catalogue(value)})
        for name in ("get_agent_account", "get_invocation", "read_invocation_thread", "reply_to_invocation"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                registry.call(value, name, {})

    def test_existing_grants_never_implicitly_acquire_a_native_binding(self):
        from startup_updates.mcp import native_agents
        values = (
            oauth.Principal(Obj(pk=7), grant(resource=config.mlai_url(), scopes=["community:read"])),
            account_principal(),
        )
        for value in values:
            with self.subTest(scopes=value.grant["scopes"]):
                self.assertEqual(native_agents.available_tools(value), [])
                self.assertFalse(native_agents.supports_events(value))
                self.assertNotIn("native_agent_grant_id", value.grant)
                self.assertNotIn("native_agent_credential_id", value.grant)


@override_settings(**PILOT_SETTINGS)
class NativePrivateAdapterTests(SimpleTestCase):
    def setUp(self):
        from agent_runtime import unified_authority
        from startup_updates.mcp import native_agents

        self.adapter, self.authority = native_agents, unified_authority
        self.value, self.native, self.credential = native_authority()
        self.binding = Obj(
            credential=self.credential, credential_id=self.credential.pk,
            client_id=unified_authority.PREFIX + self.value.grant["id"],
        )
        # Decorated services capture Atomic at import. Mock its context boundary
        # as well as every ORM manager; the runner still forbids any real SQL.
        self.enterContext(patch.object(native_agents.transaction.Atomic, "__enter__", return_value=None))
        self.enterContext(patch.object(native_agents.transaction.Atomic, "__exit__", return_value=False))
        self.enterContext(patch.object(native_agents.runtime, "locked", side_effect=lambda rows: rows))
        self.users = self.enterContext(patch.object(native_agents, "get_user_model"))
        self.users.return_value.objects.all.return_value.get.return_value = self.value.user
        self.revalidate = self.enterContext(patch.object(oauth, "valid_grant", return_value=self.value))
        self.installations = self.enterContext(patch.object(native_agents.AgentInstallation, "objects"))
        self.installations.filter.return_value.first.return_value = self.native.installation
        self.grants = self.enterContext(patch.object(native_agents.AgentGrant, "objects"))
        self.grants.filter.return_value.select_related.return_value.first.return_value = self.native
        self.grants.filter.return_value.values_list.return_value.first.return_value = self.native.installation_id
        self.grants.filter.return_value.first.return_value = self.native
        self.credentials = self.enterContext(patch.object(native_agents.AgentCredential, "objects"))
        self.credentials.filter.return_value.select_related.return_value.first.return_value = self.credential
        self.bindings = self.enterContext(patch.object(native_agents.AgentOAuthBinding, "objects"))
        binding_query = self.bindings.filter.return_value
        binding_query.exists.return_value = True
        binding_query.select_related.return_value = binding_query
        binding_query.filter.return_value = binding_query
        binding_query.__iter__.side_effect = lambda: iter([self.binding])

    def test_adapter_uses_the_original_account_grant_and_private_credential(self):
        principal = self.adapter.principal_for(self.value)
        self.assertIs(principal.owner, self.value.user)
        self.assertIs(principal.grant, self.native)
        self.assertIs(principal.credential, self.credential)
        self.revalidate.assert_any_call(self.value.grant["id"])
        self.grants.filter.assert_any_call(
            pk=self.native.pk, installation__owner=self.value.user)
        self.grants.filter.assert_any_call(pk=self.native.pk, installation=self.native.installation)
        self.credentials.filter.assert_called_once_with(pk=self.credential.pk, grant=self.native)
        self.bindings.filter.assert_any_call(
            credential=self.credential, client_id="unified-mcp:" + self.value.grant["id"])
        self.assertEqual(self.value.grant["client_id"], "client")

    def test_cache_loss_or_account_revocation_denies_the_adapter_before_native_lookup(self):
        self.revalidate.side_effect = AuthenticationFailed("The account grant was revoked.")
        with self.assertRaises(AuthenticationFailed):
            self.adapter.principal_for(self.value)
        self.grants.filter.assert_not_called()
        self.credentials.filter.assert_not_called()

    def test_private_credential_wrong_generation_scopes_audience_and_revocation_fail_closed(self):
        from agent_runtime.policy import AgentPermissionDenied

        cases = (
            ("generation", self.native.generation - 1),
            ("scopes", ["events:read", "messages:read"]),
            ("scopes", ["events:read", "messages:read", "messages:reply", "messages:write"]),
            ("audience", config.mcp_url()),
            ("revoked_at", timezone.now()),
            ("expires_at", timezone.now() - timedelta(seconds=1)),
            ("kind", "refresh"),
        )
        for field, value in cases:
            old = getattr(self.credential, field)
            setattr(self.credential, field, value)
            try:
                with self.subTest(field=field, value=value), self.assertRaises(
                    (AuthenticationFailed, PermissionDenied, AgentPermissionDenied)
                ):
                    self.adapter.principal_for(self.value)
            finally:
                setattr(self.credential, field, old)

    def test_cached_wrong_owner_native_grant_and_private_credential_cannot_rebind(self):
        from agent_runtime.policy import AgentPermissionDenied

        cases = (
            ("native_agent_grant_id", str(uuid4())),
            ("native_agent_credential_id", str(uuid4())),
            ("resource", config.mcp_url()),
            ("scopes", ["agent:thread:read", "agent:thread:reply"]),
        )
        for field, value in cases:
            old = self.value.grant[field]
            self.value.grant[field] = value
            try:
                with self.subTest(field=field), self.assertRaises(
                    (AuthenticationFailed, PermissionDenied, AgentPermissionDenied)
                ):
                    self.adapter.principal_for(self.value)
            finally:
                self.value.grant[field] = old
        self.native.installation.owner_id = 999
        with self.assertRaises((PermissionDenied, AgentPermissionDenied)):
            self.adapter.principal_for(self.value)

    def test_missing_binding_stops_the_private_adapter(self):
        self.bindings.filter.return_value.exists.return_value = False
        with self.assertRaises(PermissionDenied):
            self.adapter.principal_for(self.value)

    def test_operator_gate_hides_native_capabilities_and_denies_native_calls(self):
        with override_settings(MLAI_MCP_NATIVE_AGENTS_ENABLED=False):
            self.assertEqual(self.adapter.available_tools(self.value), [])
            self.assertFalse(self.adapter.supports_events(self.value))
            with self.assertRaises(PermissionDenied):
                registry.call(self.value, "get_agent_account", {})
        self.grants.filter.assert_not_called()
        self.credentials.filter.assert_not_called()

    def test_both_durable_and_cached_grants_must_still_reference_the_same_connection(self):
        from agent_runtime.policy import AgentPermissionDenied

        self.value.grant["native_agent_credential_id"] = str(uuid4())
        with self.assertRaises(AgentPermissionDenied):
            self.authority.assert_unified_authority(self.native, credential_id=self.credential.pk)
        self.value.grant["native_agent_credential_id"] = str(self.credential.pk)
        self.binding.client_id = "unified-mcp:another-account-grant"

        def revalidate(identifier):
            if identifier == self.value.grant["id"]:
                return self.value
            raise AuthenticationFailed("Another grant has no authority.")

        self.revalidate.side_effect = revalidate
        with self.assertRaises(AgentPermissionDenied):
            self.authority.assert_unified_authority(self.native, credential_id=self.credential.pk)

    def test_linked_queue_records_require_their_exact_private_credential_marker(self):
        from agent_runtime.policy import AgentPermissionDenied

        for metadata in ({}, {"unified_credential_id": ""}, None):
            with self.subTest(metadata=metadata), self.assertRaises(AgentPermissionDenied):
                self.authority.assert_record_authority(self.native, metadata)
        with patch.object(self.authority, "assert_unified_authority", return_value=self.value) as check:
            self.assertIs(self.authority.assert_record_authority(
                self.native, {"unified_credential_id": str(self.credential.pk)}), self.value)
        check.assert_called_once_with(self.native, credential_id=str(self.credential.pk))

    def test_standalone_grants_keep_their_original_unified_authority_behavior(self):
        self.bindings.filter.return_value.exists.return_value = False
        self.bindings.filter.return_value.__iter__.side_effect = lambda: iter([])
        with override_settings(MLAI_MCP_NATIVE_AGENTS_ENABLED=False):
            self.assertIsNone(self.authority.assert_unified_authority(self.native))
            self.assertIsNone(self.authority.assert_record_authority(self.native, {}))
        self.revalidate.assert_not_called()

    def test_explicit_unified_mode_never_falls_back_when_bridge_binding_is_deleted(self):
        from agent_runtime.policy import AgentPermissionDenied

        self.bindings.filter.return_value.exists.return_value = False
        self.bindings.filter.return_value.__iter__.side_effect = lambda: iter([])
        with self.assertRaises(AgentPermissionDenied):
            self.authority.assert_unified_authority(self.native)
        with self.assertRaises(AgentPermissionDenied):
            self.authority.assert_record_authority(self.native, {})
        self.revalidate.assert_not_called()

    def test_native_credential_authority_rejects_a_non_model_object(self):
        from agent_runtime.policy import AgentPermissionDenied

        with self.assertRaisesRegex(AgentPermissionDenied, "unified_credential_unavailable"):
            self.authority.assert_credential_authority(self.native, self.credential)

    def test_consent_creates_only_an_expired_private_adapter_and_retains_actual_client(self):
        installation = self.native.installation
        installation.grants = MagicMock()
        installation.grants.filter.return_value.values_list.return_value = [self.native.pk]
        self.installations.filter.return_value.select_related.return_value.first.return_value = installation
        self.grants.filter.return_value.order_by.return_value.first.return_value = self.native
        new_grant = Obj(**{**vars(self.native), "pk": uuid4(), "generation": self.native.generation + 1})
        self.grants.create.return_value = new_grant
        self.credentials.create.return_value = self.credential
        session = Obj(pk=self.value.grant["session_id"])
        with (
            patch.object(self.adapter.native_sources, "current_device", return_value=installation.owner_device),
            patch.object(self.adapter.native_sources, "live_access", return_value=True),
            patch.object(self.adapter.runtime, "revoke_grant") as revoke,
            patch.object(self.adapter.runtime, "audit"),
        ):
            result = self.adapter.bind_native_grant(
                self.value.user, session, self.value.grant, str(installation.pk))
        self.assertIs(result, new_grant)
        self.assertEqual(self.value.grant["client_id"], "client")
        self.assertEqual(self.value.grant["native_agent_grant_id"], str(new_grant.pk))
        self.assertEqual(self.value.grant["native_agent_credential_id"], str(self.credential.pk))
        for field in ("max_concurrent_tasks", "daily_task_limit", "daily_cost_limit_microunits", "max_task_seconds"):
            self.assertEqual(self.grants.create.call_args.kwargs[field], getattr(self.native, field))
        revoke.assert_called_once_with(self.native.pk, owner=self.value.user)
        created = self.credentials.create.call_args.kwargs
        self.assertEqual(created["audience"], config.mlai_url())
        self.assertEqual(set(created["scopes"]), {"events:read", "messages:read", "messages:reply"})
        self.assertLessEqual(created["expires_at"], timezone.now())
        self.assertEqual(len(created["token_hash"]), 64)
        self.assertNotIn("access_token", created)
        self.assertNotIn("refresh_token", created)
        self.bindings.create.assert_called_once_with(
            credential=self.credential, client_id="unified-mcp:" + self.value.grant["id"])

    def test_native_consent_cannot_bind_another_account_session(self):
        installation = self.native.installation
        self.installations.filter.return_value.select_related.return_value.first.return_value = installation
        self.grants.filter.return_value.order_by.return_value.first.return_value = self.native
        for field, value in (("user_id", 999), ("auth_version", 99), ("session_id", "another-session"), ("client_id", "")):
            old = self.value.grant[field]
            self.value.grant[field] = value
            try:
                with (
                    self.subTest(field=field),
                    patch.object(self.adapter.native_sources, "current_device", return_value=installation.owner_device),
                    patch.object(self.adapter.native_sources, "live_access", return_value=True),
                    self.assertRaises(PermissionDenied),
                ):
                    self.adapter.bind_native_grant(
                        self.value.user, Obj(pk="session"), self.value.grant, str(installation.pk))
            finally:
                self.value.grant[field] = old
        self.grants.create.assert_not_called()
        self.credentials.create.assert_not_called()
        self.bindings.create.assert_not_called()

    def test_consent_without_completed_owner_proof_creates_no_private_authority(self):
        installation = self.native.installation
        installation.owner_attestation = []
        self.installations.filter.return_value.select_related.return_value.first.return_value = installation
        with self.assertRaises(self.adapter.NativeAgentError) as error:
            self.adapter.bind_native_grant(
                self.value.user, Obj(pk="session"), self.value.grant, str(installation.pk))
        self.assertEqual(error.exception.default_code, "owner_action_required")
        self.grants.create.assert_not_called()
        self.credentials.create.assert_not_called()

    def test_native_adapter_becomes_live_only_after_unified_bearer_authentication(self):
        from agent_runtime.policy import AgentPermissionDenied

        self.credential.expires_at = timezone.now() - timedelta(seconds=1)
        with self.assertRaises((PermissionDenied, AgentPermissionDenied)):
            self.adapter.principal_for(self.value)
        self.credential.save.assert_not_called()
        with patch.object(self.adapter.runtime, "audit"):
            principal = self.adapter.on_authenticated(self.value)
        self.assertIs(principal.credential, self.credential)
        self.assertGreater(self.credential.expires_at, timezone.now())
        self.credential.save.assert_called_once_with(update_fields=("expires_at",))

    def test_mixed_catalogue_keeps_twenty_existing_tools_and_adds_bounded_native_tools(self):
        self.value.grant["scopes"] = sorted(config.ALL_SCOPES)
        definitions = registry.catalogue(self.value)
        native_names = set(self.adapter.NAMES)
        names = {item["name"] for item in definitions}
        self.assertEqual(len(definitions), 20 + len(native_names))
        self.assertEqual(len(names), len(definitions))
        self.assertTrue(native_names <= names)
        profile = next(item for item in definitions if item["name"] == "get_agent_account")
        self.assertTrue(profile["_meta"]["openai/profile"])
        self.assertEqual(profile["securitySchemes"][0]["scopes"], sorted(DOT_SCOPES))
        self.assertEqual(profile["outputSchema"]["properties"], {
            "id": {"type": "string", "minLength": 1, "pattern": r"\S"},
            **{name: {"type": "string"} for name in ("name", "email", "nickname")}})
        self.assertEqual(profile["outputSchema"]["required"], ["id"])
        self.assertFalse(profile["outputSchema"]["additionalProperties"])
        reply = next(item for item in definitions if item["name"] == "reply_to_invocation")
        self.assertFalse(reply["annotations"]["readOnlyHint"])
        self.assertFalse(reply["inputSchema"]["additionalProperties"])
        self.assertEqual(set(reply["inputSchema"]["properties"]), {"invocation_id", "text", "idempotency_key"})

    def test_native_revocation_preserves_other_capabilities_and_removes_native_discovery(self):
        self.value.grant["scopes"] = sorted(config.ALL_SCOPES)
        self.credential.revoked_at = timezone.now()
        self.assertEqual(self.adapter.available_tools(self.value), [])
        self.assertFalse(self.adapter.supports_events(self.value))
        self.assertEqual(len(registry.catalogue(self.value)), 20)

    def test_revoked_native_tool_returns_a_permission_error_without_executing(self):
        self.credential.revoked_at = timezone.now()
        with patch.object(self.adapter.broker, "execute") as execute:
            with self.assertRaises(PermissionDenied):
                registry.call(self.value, "get_agent_account", {})
        execute.assert_not_called()

    def test_profile_tool_exposes_no_credentials_or_private_adapter(self):
        with patch.object(self.adapter.broker, "execute", return_value={"owner_id": "7"}) as execute:
            result = registry.call(self.value, "get_agent_account", {})
        self.assertEqual(result["id"], str(self.native.installation_id))
        self.assertEqual(result["email"], self.value.user.email)
        self.assertEqual(set(result), {"id", "name", "email", "nickname"})
        self.assertNotIn(self.credential.token_hash, json.dumps(result))
        self.assertNotIn(str(self.credential.pk), json.dumps(result))
        self.assertEqual(execute.call_args.args[1:], ("get_profile", {}))


@override_settings(**PILOT_SETTINGS)
class UnifiedDotProtocolTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def post(self, principal, payload, *, legacy=False, routing_headers=True, missing_headers=(), **headers):
        path = "/mcp/valley" if legacy else "/mcp/mlai"
        if routing_headers and not legacy:
            params = dict(payload.get("params", {}))
            params.setdefault("_meta", {"io.modelcontextprotocol/protocolVersion": "2026-07-28"})
            if isinstance(params["_meta"], dict):
                params["_meta"] = {"io.modelcontextprotocol/clientCapabilities": {}, **params["_meta"]}
            payload = {**payload, "params": params}
            headers.setdefault("HTTP_MCP_PROTOCOL_VERSION", "2026-07-28")
            headers.setdefault("HTTP_MCP_METHOD", payload["method"])
            if payload["method"] == "tools/call":
                headers.setdefault("HTTP_MCP_NAME", params.get("name", ""))
        headers = {key: value for key, value in headers.items() if key not in missing_headers}
        request = self.factory.post(
            path, payload, format="json", HTTP_AUTHORIZATION="Bearer valley_access_synthetic",
            HTTP_ACCEPT="application/json, text/event-stream", **headers,
        )
        view = views.McpView if legacy else views.MlaiMcpView
        with patch.object(oauth, "authenticate_token", return_value=principal) as authenticate:
            response = view.as_view(throttle_classes=())(request)
        authenticate.assert_called_once_with(
            "valley_access_synthetic", resource=config.mcp_url() if legacy else config.mlai_url())
        return response, json.loads(response.content)

    def test_existing_canonical_route_hosts_mcp_two_discovery(self):
        from django.urls import resolve
        from startup_updates.mcp import native_agents

        self.assertIs(resolve("/mcp/mlai").func.view_class, views.MlaiMcpView)
        value, _, _ = native_authority()
        with patch.object(native_agents, "supports_events", return_value=True):
            response, payload = self.post(value, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["resultType"], "complete")
        self.assertIn("2026-07-28", payload["result"]["supportedVersions"])
        self.assertIn("events", payload["result"]["capabilities"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_non_native_discovery_never_advertises_event_authority(self):
        value = account_principal(scopes=["community:read"])
        response, payload = self.post(value, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("events", payload["result"]["capabilities"])

    def test_events_use_the_unified_principal_and_strip_matched_protocol_metadata(self):
        from startup_updates.mcp import native_agents

        value, _, _ = native_authority()
        with (
            patch.object(native_agents, "handle_events", return_value={"events": []}) as handle,
            patch.object(native_agents, "supports_events", return_value=True),
        ):
            response, payload = self.post(value, {
                "jsonrpc": "2.0", "id": "list", "method": "events/list", "params": {},
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["events"], [])
        self.assertEqual(payload["result"]["resultType"], "complete")
        handle.assert_called_once_with(value, "events/list", {})

    def test_mcp_two_missing_or_mismatched_headers_fail_before_event_dispatch(self):
        from startup_updates.mcp import native_agents

        value, _, _ = native_authority()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "events/list"}
        cases = (
            {"missing_headers": ("HTTP_MCP_METHOD",)},
            {"missing_headers": ("HTTP_MCP_PROTOCOL_VERSION",)},
            {"HTTP_MCP_METHOD": "events/subscribe"},
            {"params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}}},
        )
        for case in cases:
            body = {**payload, "params": case.get("params", {})}
            headers = {key: value for key, value in case.items() if key != "params"}
            with self.subTest(case=case), patch.object(native_agents, "handle_events") as handle:
                response, answer = self.post(value, body, **headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(answer["error"]["code"], -32020)
            handle.assert_not_called()

    def test_body_only_mcp_two_cannot_bypass_mirrored_transport_fields(self):
        from startup_updates.mcp import native_agents

        value, _, _ = native_authority()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "events/list", "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {}}}}
        with patch.object(native_agents, "handle_events") as handle:
            response, answer = self.post(value, payload, routing_headers=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(answer["error"]["code"], -32020)
        handle.assert_not_called()

    def test_mcp_two_tool_name_header_must_match_the_tool_body(self):
        value = account_principal(scopes=["community:read"])
        for header in ("reply_to_invocation", None):
            options = {"HTTP_MCP_NAME": header} if header is not None else {"missing_headers": ("HTTP_MCP_NAME",)}
            with self.subTest(header=header), patch.object(registry, "call") as call:
                response, answer = self.post(value, {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "list_community_channels", "arguments": {}},
                }, **options)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(answer["error"]["code"], -32020)
            call.assert_not_called()

    def test_mcp_two_requires_client_capabilities_but_client_info_is_optional(self):
        value = account_principal(scopes=["community:read"])
        for capabilities in ("missing", None, [], "tools", True):
            meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
            if capabilities != "missing":
                meta["io.modelcontextprotocol/clientCapabilities"] = capabilities
            with self.subTest(capabilities=capabilities):
                response, answer = self.post(value, {
                    "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": meta},
                }, routing_headers=False, HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="ping")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(answer["error"]["code"], -32602)
        response, answer = self.post(value, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(answer["result"]["resultType"], "complete")

    def test_mcp_two_base64_tool_name_mirror_dispatches_the_same_named_tool(self):
        value, _, _ = native_authority()
        tool_name = "get_agent_account"
        mirrored = "=?base64?" + base64.b64encode(tool_name.encode()).decode() + "?="
        profile = {"id": "synthetic-installation", "name": "My Dot"}
        with patch.object(registry, "call", return_value=profile) as call:
            response, answer = self.post(value, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": tool_name, "arguments": {}},
            }, HTTP_MCP_NAME=mirrored)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(answer["result"]["structuredContent"], profile)
        call.assert_called_once_with(value, tool_name, {})

    def test_legacy_route_never_dispatches_native_events_even_for_a_forged_principal(self):
        from startup_updates.mcp import native_agents

        value = oauth.Principal(Obj(pk=7), grant(scopes=sorted(config.SCOPES | DOT_SCOPES)))
        with patch.object(native_agents, "handle_events") as handle:
            response, payload = self.post(value, {
                "jsonrpc": "2.0", "id": 1, "method": "events/list",
            }, legacy=True, HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        self.assertIn(response.status_code, (200, 400))
        self.assertIn("error", payload)
        handle.assert_not_called()

    def test_mcp_two_tool_discovery_is_private_and_uncached(self):
        value = account_principal(scopes=["community:read"])
        response, payload = self.post(value, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        }, HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["cacheScope"], "private")
        self.assertEqual(payload["result"]["ttlMs"], 0)
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_tool_linking_challenge_requests_the_complete_atomic_pilot_permission(self):
        value = account_principal(scopes=["community:read"])
        with patch.object(registry, "call", side_effect=PermissionDenied('private detail "\\\nsecret')):
            response, payload = self.post(value, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_agent_account", "arguments": {}}})
        challenge = payload["result"]["_meta"]["mcp/www_authenticate"][0]
        self.assertIn('error="insufficient_scope"', challenge)
        self.assertIn('error_description="Reconnect with the required permission for this capability."', challenge)
        self.assertIn('scope="' + " ".join(sorted(DOT_SCOPES)) + '"', challenge)
        self.assertNotIn("secret", challenge)
        self.assertNotIn("\n", challenge)

    def test_http_bearer_challenges_use_safe_constant_descriptions(self):
        for insufficient in (False, True):
            with self.subTest(insufficient=insufficient):
                failure = views.bearer_failure(AuthenticationFailed('private " detail\nsecret'),
                    insufficient=insufficient, resource=config.mlai_url())
                self.assertIn('error_description="', failure["WWW-Authenticate"])
                self.assertNotIn("secret", failure["WWW-Authenticate"])
                self.assertNotIn("\n", failure["WWW-Authenticate"])


@override_settings(**{**PILOT_SETTINGS, "VALLEY_MCP_ENABLED": True, "VALLEY_MCP_UNIFIED_ALIAS_ENABLED": True})
class RegisteredTransportAliasTests(SimpleTestCase):
    """Immutable registered transport accepts only explicitly selected audiences."""
    def setUp(self):
        self.factory = APIRequestFactory()

    def post(self, value, method="tools/list", *, params=None, modern=False):
        params = params or {}
        headers = {"HTTP_AUTHORIZATION": "Bearer valley_access_synthetic", "HTTP_ACCEPT": "application/json, text/event-stream"}
        if modern:
            params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}}}
            headers.update(HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD=method)
            if method == "tools/call": headers["HTTP_MCP_NAME"] = params["name"]
        request = self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": params}, format="json", **headers)
        with patch.object(oauth, "authenticate_token", return_value=value) as authenticate:
            result = views.McpView.as_view(throttle_classes=())(request)
        authenticate.assert_called_once_with("valley_access_synthetic")
        return result, json.loads(result.content)

    def test_legacy_tokens_keep_four_tools_and_no_event_or_modern_authority(self):
        value = oauth.Principal(Obj(pk=7), grant(scopes=sorted(config.SCOPES)))
        response, payload = self.post(value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(payload["result"]["tools"]), 4)
        with patch("startup_updates.mcp.native_agents.handle_events") as events:
            response, payload = self.post(value, "events/list", modern=True)
        self.assertEqual(response.status_code, 400)
        events.assert_not_called()

    def test_fresh_main_grant_discovers_native_tools_and_dispatches_events_at_registered_transport(self):
        from startup_updates.mcp import native_agents
        value, _, _ = native_authority()
        with patch.object(native_agents, "principal_for"):
            response, payload = self.post(value, modern=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({tool["name"] for tool in payload["result"]["tools"]}, set(native_agents.NAMES))
        with patch.object(native_agents, "handle_events", return_value={"events": []}) as handle:
            response, payload = self.post(value, "events/list", modern=True)
        self.assertEqual(response.status_code, 200)
        handle.assert_called_once_with(value, "events/list", {})

    def test_another_logical_audience_is_rejected_before_catalogue_or_dispatch(self):
        value = account_principal(resource="https://other.example/mcp/mlai")
        with patch.object(registry, "catalogue") as catalogue:
            response, payload = self.post(value, modern=True)
        self.assertEqual(response.status_code, 401)
        catalogue.assert_not_called()
        self.assertIn("/oauth-protected-resource/mcp/valley", response["WWW-Authenticate"])

    def test_alias_disabled_authentication_still_requires_exact_valley_audience(self):
        request = self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            format="json", HTTP_AUTHORIZATION="Bearer valley_access_synthetic")
        with override_settings(VALLEY_MCP_UNIFIED_ALIAS_ENABLED=False), patch.object(
                oauth, "authenticate_token", side_effect=AuthenticationFailed("Different audience")) as authenticate:
            response = views.McpView.as_view(throttle_classes=())(request)
        self.assertEqual(response.status_code, 401)
        authenticate.assert_called_once_with("valley_access_synthetic", resource=config.mcp_url())

    def test_anonymous_discovery_keeps_registered_valley_resource_and_two_scopes(self):
        request = self.factory.post("/mcp/valley", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json")
        response = views.McpView.as_view(throttle_classes=())(request)
        self.assertEqual(response.status_code, 401)
        self.assertIn("/oauth-protected-resource/mcp/valley", response["WWW-Authenticate"])
        metadata = views.ResourceMetadataView.as_view(throttle_classes=())(self.factory.get("/metadata"))
        payload = json.loads(metadata.content)
        self.assertEqual(payload["resource"], config.mcp_url())
        self.assertEqual(set(payload["scopes_supported"]), set(config.SCOPES))


@override_settings(**PILOT_SETTINGS)
class QueuedUnifiedAuthorityTests(SimpleTestCase):
    """Revocation after admission must stop callbacks and frozen reply events."""

    def test_revocation_before_callback_dispatch_stops_the_exact_subscription(self):
        from agent_runtime import event_webhooks, native_invocations, unified_authority
        from agent_runtime.policy import AgentPermissionDenied
        from agent_runtime.tests.test_event_webhooks import SECRET
        from agent_runtime.webhook_transport import WebhookResponse

        _, native, credential = native_authority()
        now = timezone.now()
        invocation_id = uuid4()
        subscription = Obj(
            pk=uuid4(), grant_id=native.pk, generation=native.generation,
            resource=native.resources[0], is_active=True, cursor=0,
            event_types=["agent.mentioned"], save=MagicMock(),
            delivery={
                "mode": "webhook", "unified_credential_id": str(credential.pk),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "sealed": event_webhooks.seal({"url": "https://callback.invalid/dot", "secret": SECRET}),
            },
        )
        event = Obj(
            pk=1, resource=native.resources[0], event_type="agent.mentioned",
            occurred_at=now, payload={"installation_id": str(native.installation_id),
                "invocation_id": str(invocation_id), "source_event_id": "f" * 64},
        )
        transport = MagicMock(return_value=WebhookResponse(202, b"{}"))
        with (
            patch.object(event_webhooks, "AgentSubscription") as subscriptions,
            patch.object(event_webhooks, "AgentEvent") as events,
            patch.object(unified_authority.AgentOAuthBinding, "objects") as bindings,
            patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()),
            patch.object(event_webhooks, "locked", side_effect=lambda rows: rows),
            patch.object(event_webhooks, "_grant", return_value=native),
            patch.object(event_webhooks, "authorize"),
            patch.object(event_webhooks, "should_wake", return_value=True),
            patch.object(event_webhooks, "event_payload", return_value={"id": "synthetic-event-1"}),
            patch.object(native_invocations, "invocation_deliverable", return_value=True),
            patch.object(unified_authority, "assert_unified_authority",
                         side_effect=AgentPermissionDenied("unified_grant_revoked")) as authority,
        ):
            subscriptions.objects.filter.return_value.order_by.return_value.values_list.return_value = [subscription.pk]
            subscriptions.objects.values_list.return_value.get.return_value = native.pk
            subscriptions.objects.all.return_value.get.return_value = subscription
            events.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [event]
            bindings.filter.return_value.exists.return_value = True
            self.assertEqual(event_webhooks.dispatch_webhooks(now=now, transport=transport), 0)
        authority.assert_called_once()
        self.assertEqual(authority.call_args.kwargs.get("credential_id"), str(credential.pk))
        transport.assert_not_called()
        self.assertFalse(subscription.is_active)
        self.assertEqual(subscription.cursor, 0)

    def test_revocation_before_reply_dispatch_cancels_without_sending_the_signed_event(self):
        from agent_runtime import native_invocations, native_sources, unified_authority
        from agent_runtime.policy import AgentPermissionDenied

        for initial, stopped in (("pending", "cancelled"), ("unknown", "stopped_unknown")):
            _, native, credential = native_authority()
            now = timezone.now()
            invocation = Obj(
                pk=uuid4(), grant=native, grant_id=native.pk,
                installation=native.installation, installation_id=native.installation_id,
                generation=native.generation, status="pending", resource=native.resources[0],
                owner_public_key=native.installation.owner_device.public_key,
                source_event_id="e" * 64, thread_id="f" * 64,
                expires_at=now + timedelta(minutes=15), save=MagicMock(),
            )
            row = Obj(
                status=initial, receipt={"unified_credential_id": str(credential.pk)},
                event={"id": "d" * 64, "content": "The exact already queued answer"},
                error_code="", save=MagicMock(),
            )
            with (
                self.subTest(initial=initial),
                patch.object(native_invocations, "AgentInvocation") as invocations,
                patch.object(native_invocations, "AgentInvocationReply") as replies,
                patch.object(unified_authority.AgentOAuthBinding, "objects") as bindings,
                patch.object(native_invocations.transaction, "atomic", side_effect=lambda: nullcontext()),
                patch.object(native_invocations.runtime, "locked", side_effect=lambda rows: rows),
                patch.object(native_invocations.runtime, "_grant", return_value=native),
                patch.object(native_sources, "require_native"),
                patch.object(native_sources, "current_device", return_value=native.installation.owner_device),
                patch.object(native_sources, "live_access", return_value=True),
                patch.object(native_sources, "send_reply", return_value={"accepted": True}) as send,
                patch.object(unified_authority, "assert_unified_authority",
                             side_effect=AgentPermissionDenied("unified_grant_revoked")) as authority,
            ):
                replies.objects.filter.return_value.order_by.return_value.values_list.return_value = [invocation.pk]
                replies.objects.all.return_value.get.return_value = row
                invocations.objects.values_list.return_value.get.return_value = native.pk
                invocations.objects.all.return_value.get.return_value = invocation
                bindings.filter.return_value.exists.return_value = True
                self.assertEqual(native_invocations.dispatch_native_replies(now=now), 0)
            authority.assert_called_once()
            self.assertEqual(authority.call_args.kwargs.get("credential_id"), str(credential.pk))
            send.assert_not_called()
            self.assertEqual(row.status, stopped)
            self.assertEqual(invocation.status, "pending")
            self.assertEqual(row.event["content"], "The exact already queued answer")
