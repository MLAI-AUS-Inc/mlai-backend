"""Dot pilot crypto, isolation, transport and durable reply contracts, without DB/network."""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from coincurve import PrivateKey, PublicKeyXOnly
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from agent_runtime import broker, native_crypto, native_installations, native_invocations, native_sources, policy, serializers
from agent_runtime.native_views import NativeAppsView, NativeInstallationsView, NativeRegistryView
from agent_runtime.webhook_transport import CallbackEndpointError, WebhookResponse

NOW = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)
COMMUNITY = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
RESOURCE = f"community:{COMMUNITY}:channel:{CHANNEL}"
CONFIG = {COMMUNITY: {"relay_url": "https://chat.example", "channel_ids": [CHANNEL]}}
OWNER_KEY = PrivateKey(bytes.fromhex("01" * 32))
BOT_KEY = PrivateKey(bytes.fromhex("02" * 32))


def fixture():
    device = SimpleNamespace(pk=uuid4(), public_key=native_crypto.public_key(OWNER_KEY))
    installation = SimpleNamespace(pk=uuid4(), community_id=COMMUNITY, app_id="openai-dot", name="My Dot",
        is_active=True, state="active", owner=SimpleNamespace(pk=7, is_active=True),
        owner_device=device, owner_device_id=device.pk, public_key=native_crypto.public_key(BOT_KEY),
        sealed_signing_key=native_crypto.seal_key(BOT_KEY.secret.hex()))
    grant = SimpleNamespace(pk=uuid4(), installation=installation, installation_id=installation.pk,
        scopes=native_installations.DOT_SCOPES, resources=[RESOURCE], generation=2,
        expires_at=NOW + timedelta(days=1), revoked_at=None, daily_task_limit=20)
    invocation = SimpleNamespace(pk=uuid4(), grant=grant, grant_id=grant.pk,
        installation=installation, installation_id=installation.pk, generation=2, resource=RESOURCE,
        source_event_id="a" * 64, thread_id="b" * 64, owner_public_key=device.public_key,
        expires_at=NOW + timedelta(minutes=15), status="pending")
    return installation, grant, invocation


class NativeCryptoTests(SimpleTestCase):
    def test_sealed_key_round_trip_never_stores_plaintext(self):
        raw = BOT_KEY.secret.hex()
        sealed = native_crypto.seal_key(raw)
        self.assertNotIn(raw, sealed)
        self.assertEqual(native_crypto.public_key(native_crypto.unseal_key(sealed)), native_crypto.public_key(BOT_KEY))

    def test_tampered_key_and_wrong_encryption_key_fail_closed(self):
        sealed = native_crypto.seal_key(BOT_KEY.secret.hex())
        with override_settings(SECRET_KEY="other-test-key"), self.assertRaises(PermissionDenied):
            native_crypto.unseal_key(sealed)
        with self.assertRaises(PermissionDenied):
            native_crypto.unseal_key("invalid")

    def enrollment(self):
        installation, _, _ = fixture()
        return SimpleNamespace(pk=uuid4(), installation=installation, resource=RESOURCE,
            consent_version=native_installations.CONSENT_VERSION, nonce="synthetic-nonce",
            expires_at=NOW + timedelta(minutes=10))

    def signed_challenge(self, enrollment):
        challenge = native_crypto.enrollment_challenge(enrollment, relay_url="https://chat.example",
            audience="https://api.example/mcp/", now=NOW)
        return native_crypto.sign_event(OWNER_KEY, **challenge)

    def test_owner_challenge_binds_installation_community_bot_and_resource(self):
        enrollment = self.enrollment()
        event = self.signed_challenge(enrollment)
        native_crypto.verify_enrollment(event, enrollment, relay_url="https://chat.example", audience="https://api.example/mcp/", now=NOW)
        for field, value in (("pk", uuid4()), ("resource", RESOURCE.replace(CHANNEL, str(uuid4()))), ("nonce", "changed")):
            original = getattr(enrollment, field)
            setattr(enrollment, field, value)
            with self.subTest(field=field), self.assertRaises(PermissionDenied):
                native_crypto.verify_enrollment(event, enrollment, relay_url="https://chat.example", audience="https://api.example/mcp/", now=NOW)
            setattr(enrollment, field, original)

    def test_old_future_expired_and_wrong_owner_proofs_fail(self):
        enrollment = self.enrollment()
        event = self.signed_challenge(enrollment)
        for delta in (-301, 301):
            changed = native_crypto.sign_event(OWNER_KEY, kind=event["kind"], created_at=int(NOW.timestamp()) + delta,
                                              content=event["content"], tags=event["tags"])
            with self.assertRaises(PermissionDenied):
                native_crypto.verify_enrollment(changed, enrollment, relay_url="https://chat.example", audience="https://api.example/mcp/", now=NOW)
        event["pubkey"] = native_crypto.public_key(BOT_KEY)
        with self.assertRaises(PermissionDenied):
            native_crypto.verify_enrollment(event, enrollment, relay_url="https://chat.example", audience="https://api.example/mcp/", now=NOW)

    def test_nip_oa_requires_exact_owner_bot_and_expiry(self):
        installation, _, _ = fixture()
        conditions = f"created_at<{int((NOW + timedelta(days=30)).timestamp())}"
        digest = hashlib.sha256(f"nostr:agent-auth:{installation.public_key}:{conditions}".encode()).digest()
        tag = ["auth", installation.owner_device.public_key, conditions, OWNER_KEY.sign_schnorr(digest).hex()]
        self.assertEqual(native_crypto.verify_owner_attestation(json.dumps(tag), installation, conditions), tag)
        for wrong in (conditions + "&kind=9", ""):
            with self.assertRaises(ValidationError):
                native_crypto.verify_owner_attestation(tag, installation, wrong)


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_NATIVE_ENABLED=True,
                   AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True, AGENT_RUNTIME_NATIVE_COMMUNITIES=CONFIG,
                   AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY="03" * 32)
class NativeResourceTests(SimpleTestCase):
    def test_canonical_resource_and_config_only(self):
        self.assertEqual(native_sources.parse_resource(RESOURCE), (COMMUNITY, CHANNEL))
        for value in (RESOURCE.upper(), "community:" + "z" * 36 + ":channel:" + CHANNEL,
                      RESOURCE + ":extra", "https://attacker.example"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                native_sources.parse_resource(value)
        with self.assertRaises(PermissionDenied):
            native_sources.community_config(uuid4())

    def test_relay_resolution_accepts_canonical_https_and_wss_only(self):
        self.assertEqual(native_sources.community_for_relay("wss://chat.example/")[0], COMMUNITY)
        for value in ("https://chat.example.evil", "https://chat.example/private", "https://user@chat.example", "http://chat.example"):
            with self.assertRaises(PermissionDenied):
                native_sources.community_for_relay(value)

    def test_plain_native_reads_and_search_never_reach_provider(self):
        from agent_runtime.sources import read_messages
        with self.assertRaises(PermissionDenied):
            read_messages(SimpleNamespace(), RESOURCE, thread_id="a" * 64)

    def test_read_request_is_payload_bound_randomized_and_total_bounded(self):
        with patch("agent_runtime.webhook_transport.post_webhook", return_value=WebhookResponse(200, b'{"channels":[]}')) as transport:
            native_sources._request(CONFIG[COMMUNITY], "/query", [{"native_agent": {"operation": "channels"}}])
            native_sources._request(CONFIG[COMMUNITY], "/query", [{"native_agent": {"operation": "channels"}}])
        proofs = []
        for call in transport.call_args_list:
            url, body, headers = call.args
            proof = json.loads(base64.b64decode(headers["Authorization"][6:]))
            tags = dict(proof["tags"])
            self.assertEqual(tags["u"], url)
            self.assertEqual(tags["payload"], hashlib.sha256(body).hexdigest())
            self.assertTrue(PublicKeyXOnly(bytes.fromhex(proof["pubkey"])).verify(bytes.fromhex(proof["sig"]), bytes.fromhex(proof["id"])))
            self.assertEqual(call.kwargs, {"timeout": 10, "max_response_bytes": 262144})
            proofs.append(proof["id"])
        self.assertNotEqual(*proofs)

    def test_transport_denial_ambiguity_and_malformed_reply(self):
        for response in (WebhookResponse(401, b'{}'), WebhookResponse(403, b'{}')):
            with patch("agent_runtime.webhook_transport.post_webhook", return_value=response), self.assertRaises(PermissionDenied):
                native_sources._request(CONFIG[COMMUNITY], "/query", [])
        for response in (WebhookResponse(503, b'{}'), WebhookResponse(200, b'[]'), WebhookResponse(200, b'invalid')):
            with patch("agent_runtime.webhook_transport.post_webhook", return_value=response), self.assertRaises(native_sources.SourceUnavailable):
                native_sources._request(CONFIG[COMMUNITY], "/query", [])
        with patch("agent_runtime.webhook_transport.post_webhook", side_effect=CallbackEndpointError("timeout")), self.assertRaises(native_sources.SourceUnavailable):
            native_sources._request(CONFIG[COMMUNITY], "/query", [])


@override_settings(AGENT_RUNTIME_ENABLED=True)
class InvocationAuthorityTests(SimpleTestCase):
    def test_valid_invocation_and_cross_authority_rejections(self):
        _, grant, invocation = fixture()
        native_invocations.validate_invocation(invocation, grant, now=NOW)
        mutations = [("grant_id", uuid4()), ("installation_id", uuid4()), ("generation", 1),
                     ("resource", RESOURCE.replace(CHANNEL, str(uuid4()))), ("owner_public_key", "invalid"),
                     ("thread_id", "not-an-event"), ("expires_at", NOW), ("status", "cancelled")]
        for field, value in mutations:
            _, grant, invocation = fixture()
            setattr(invocation, field, value)
            with self.subTest(field=field), self.assertRaises(policy.AgentPermissionDenied):
                native_invocations.validate_invocation(invocation, grant, now=NOW)

    def test_pause_revocation_expiry_and_device_identity_are_fenced(self):
        for state in ("paused", "revoked", "awaiting_owner_proof", "provisioning"):
            installation, grant, invocation = fixture()
            installation.state = state
            with self.assertRaises(policy.AgentPermissionDenied):
                native_invocations.validate_invocation(invocation, grant, now=NOW)
        _, grant, invocation = fixture()
        invocation.expires_at = NOW - timedelta(seconds=1)
        native_invocations.validate_invocation(invocation, grant, now=NOW, allow_expired=True)
        grant.revoked_at = NOW
        with self.assertRaises(policy.AgentPermissionDenied):
            native_invocations.validate_invocation(invocation, grant, now=NOW, allow_expired=True)

    def test_signed_reply_has_frozen_destination_and_bot_identity(self):
        installation, _, invocation = fixture()
        event = native_invocations.reply_event(invocation, "Answer", created_at=int(NOW.timestamp()))
        self.assertEqual(event["pubkey"], installation.public_key)
        context = json.loads(next(t[1] for t in event["tags"] if t[0] == "native_invocation"))
        self.assertEqual(context["trigger_event_id"], invocation.source_event_id)
        self.assertEqual(context["root_event_id"], invocation.thread_id)
        self.assertEqual(context["invocation_id"], str(invocation.pk))
        self.assertEqual(context["expires_at"], int(invocation.expires_at.timestamp()))
        self.assertTrue(PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])))

    def test_reply_serializer_rejects_caller_selected_destination_identity_and_blank(self):
        valid = {"invocation_id": str(uuid4()), "text": "answer", "idempotency_key": "reply-1"}
        for extra in ({"channel_id": CHANNEL}, {"owner_id": 9}, {"pubkey": "a" * 64}, {"text": ""}):
            with self.assertRaises(ValidationError):
                serializers.validated(serializers.InvocationReplySerializer, {**valid, **extra})

    @override_settings(AGENT_RUNTIME_NATIVE_ENABLED=True, AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True)
    def test_exact_retry_returns_same_receipt_and_changed_reply_conflicts(self):
        _, grant, invocation = fixture()
        text, key = "Answer", "reply-1"
        row = SimpleNamespace(invocation_id=invocation.pk, payload_hash=policy.canonical_hash({"invocation_id": str(invocation.pk), "text": text}),
            idempotency_key=key, status="unknown", event={"id": "c" * 64}, receipt={}, error_code="receipt_unknown")
        with patch("agent_runtime.native_invocations._authorized_invocation", return_value=(None, invocation)), \
             patch("agent_runtime.native_invocations.AgentInvocationReply.objects") as rows:
            rows.filter.return_value.first.return_value = row
            self.assertEqual(native_invocations.reply_to_invocation.__wrapped__(None, invocation.pk, text, key)["event_id"], "c" * 64)
            rows.create.assert_not_called()
            with self.assertRaises(ValidationError):
                native_invocations.reply_to_invocation.__wrapped__(None, invocation.pk, "different", key)

    def test_native_tool_catalog_omits_arbitrary_history_search_and_actions(self):
        _, grant, _ = fixture()
        names = {tool["name"] for tool in broker.available_tools(SimpleNamespace(grant=grant))}
        self.assertEqual(names, broker.NATIVE_TOOLS)
        self.assertTrue(names.isdisjoint({"read_channel", "read_thread", "search_messages", "propose_action", "get_events_since"}))


@override_settings(AGENT_RUNTIME_ENABLED=False, AGENT_RUNTIME_NATIVE_COMMUNITIES=CONFIG,
                   AGENT_RUNTIME_NATIVE_REGISTRY_KEY="r" * 40)
class NativeDisabledBoundaryTests(SimpleTestCase):
    def test_disabled_owner_catalog_and_listing_do_not_query_database(self):
        owner = SimpleNamespace(pk=7, is_authenticated=True, is_active=True)
        request = APIRequestFactory().get("/apps/", {"relay_url": "https://chat.example"})
        force_authenticate(request, user=owner)
        response = NativeAppsView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["enabled"])
        request = APIRequestFactory().get("/installations/", {"community": COMMUNITY})
        force_authenticate(request, user=owner)
        self.assertEqual(NativeInstallationsView.as_view()(request).data["installations"], [])

    def test_registry_keeps_tombstones_when_runtime_stopped(self):
        request = APIRequestFactory().get("/registry/", {"relay_url": "https://chat.example"}, HTTP_AUTHORIZATION="Bearer " + "r" * 40)
        with patch("agent_runtime.native_views.AgentInstallation.objects") as rows:
            rows.filter.return_value.exists.return_value = True
            response = NativeRegistryView.as_view()(request, public_key="a" * 64)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["registered"])
        self.assertEqual(rows.filter.call_args.kwargs, {"public_key": "a" * 64, "community_id__isnull": False})

    def test_registry_bad_service_secret_stops_before_database(self):
        request = APIRequestFactory().get("/registry/", {"relay_url": "https://chat.example"}, HTTP_AUTHORIZATION="Bearer wrong")
        response = NativeRegistryView.as_view()(request, public_key="a" * 64)
        self.assertEqual(response.status_code, 403)

    def test_service_classification_is_not_charged_to_public_agent_budget(self):
        factory = APIRequestFactory()
        with patch("agent_runtime.native_views.AgentInstallation.objects") as rows:
            rows.filter.return_value.exists.return_value = False
            for _ in range(65):
                request = factory.get("/registry/", {"relay_url": "https://chat.example"}, HTTP_AUTHORIZATION="Bearer " + "r" * 40)
                self.assertEqual(NativeRegistryView.as_view()(request, public_key="a" * 64).status_code, 200)
        self.assertEqual(rows.filter.call_count, 65)

    def test_nonpilot_tenant_identity_classification_does_not_grant_native_access(self):
        request = APIRequestFactory().get("/registry/", {"relay_url": "wss://other.example"}, HTTP_AUTHORIZATION="Bearer " + "r" * 40)
        with patch("agent_runtime.native_views.AgentInstallation.objects") as rows:
            rows.filter.return_value.exists.return_value = False
            response = NativeRegistryView.as_view()(request, public_key="a" * 64)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["registered"])
        self.assertEqual(response.data["relay_url"], "https://other.example")
        with self.assertRaises(PermissionDenied):
            native_sources.community_for_relay("wss://other.example")


class NativeCatalogSafetyTests(SimpleTestCase):
    def test_operator_url_and_client_errors_do_not_enable_catalog(self):
        with override_settings(AGENT_RUNTIME_DOT_SETUP_URL="https://[invalid"):
            self.assertIsNone(native_installations.setup_url())
        with override_settings(AGENT_RUNTIME_OAUTH_CLIENTS={"bad": {"app_id": "openai-dot", "scopes": None}}):
            self.assertFalse(native_installations.dot_available())

    def test_profile_name_byte_limit_rejects_before_reserving_a_slot(self):
        data = {"community_id": COMMUNITY, "app_id": "openai-dot", "name": "🦘" * 30,
                "resource": RESOURCE, "idempotency_key": "test", "owner_public_key": "a" * 64,
                "consent_version": native_installations.CONSENT_VERSION}
        with self.assertRaises(ValidationError):
            serializers.validated(serializers.EnrollmentSerializer, data)
