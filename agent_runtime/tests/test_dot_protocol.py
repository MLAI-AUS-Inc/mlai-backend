"""Synthetic MCP Events contract tests; no provider, network, or database.

The callback fixture verifies signed wire bytes. It is intentionally not proof
that a real Dot connected, subscribed, ran, or posted a native chat reply.
"""

import base64
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import hmac
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from agent_runtime import event_webhooks
from agent_runtime.mcp import MCPView, input_schema
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.tests.test_core import NOW, grant_fixture
from agent_runtime.webhook_transport import CallbackEndpointError, WebhookResponse, post_webhook


SECRET = "whsec_" + base64.b64encode(b"synthetic-dot-callback-key-32-byte").decode()


def native_grant():
    grant = grant_fixture()
    grant.installation.app_id = "openai-dot"
    grant.installation.community_id = "synthetic-community"
    grant.resources = ["community:synthetic-community:channel:" + str(uuid4())]
    return grant


def subscription_params(grant, *, url="https://callback.invalid/selected-dot"):
    return {"name": "agent.mentioned", "arguments": {
        "installation_id": str(grant.installation_id), "resource": grant.resources[0]},
        "delivery": {"mode": "webhook", "url": url, "secret": SECRET}}


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=True,
                   AGENT_RUNTIME_NATIVE_ENABLED=True, SECRET_KEY="synthetic-dot-protocol-test")
class DotDiscoveryTests(SimpleTestCase):
    def setUp(self):
        self.grant = native_grant()
        self.principal = SimpleNamespace(grant=self.grant, credential=SimpleNamespace(scopes=["events:read"]))
        self.enterContext(patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(event_webhooks, "_authorized", return_value=self.principal))

    def test_native_catalog_is_installation_bound_and_has_no_broad_message_events(self):
        events = event_webhooks.event_definitions(self.principal)["events"]
        self.assertEqual([event["name"] for event in events], ["agent.mentioned"])
        schema = events[0]["inputSchema"]
        self.assertEqual(schema["properties"]["installation_id"]["enum"], [str(self.grant.installation_id)])
        self.assertEqual(schema["properties"]["resource"]["enum"], self.grant.resources)
        self.assertEqual(events[0]["payloadSchema"]["required"],
                         ["installation_id", "resource", "invocation_id", "source_event_id"])

    def test_operator_and_product_gate_native_catalog(self):
        for setting in (False, True):
            with override_settings(AGENT_RUNTIME_NATIVE_ENABLED=setting):
                self.grant.installation.app_id = "other-agent"
                self.assertEqual(event_webhooks.event_definitions(self.principal), {"events": []})
        self.grant.installation.app_id = "openai-dot"
        with override_settings(AGENT_RUNTIME_NATIVE_ENABLED=False):
            self.assertEqual(event_webhooks.event_definitions(self.principal), {"events": []})
        self.principal.credential.scopes = []
        self.assertEqual(event_webhooks.event_definitions(self.principal), {"events": []})

    def test_slack_event_catalog_is_unchanged(self):
        self.grant = grant_fixture()
        self.principal.grant = self.grant
        events = event_webhooks.event_definitions(self.principal)["events"]
        self.assertEqual([event["name"] for event in events], list(event_webhooks.EVENT_NAMES))
        self.assertEqual(events[0]["inputSchema"]["required"], ["resource"])

    def test_identity_survives_grant_renewal_but_not_another_installation(self):
        params = subscription_params(self.grant)
        original = event_webhooks.identity(self.grant, params)[0]
        self.grant.pk = uuid4()
        self.assertEqual(original, event_webhooks.identity(self.grant, params)[0])
        self.assertEqual(original, event_webhooks.identity(self.grant, {**params, "ttlMs": 1000})[0])
        different = native_grant()
        with self.assertRaisesRegex(AgentPermissionDenied, "invalid_subscription"):
            event_webhooks.identity(different, params)

    def test_native_identity_rejects_cross_community_and_unknown_filters(self):
        params = subscription_params(self.grant)
        for arguments in (
            {"resource": self.grant.resources[0]},
            {**params["arguments"], "installation_id": str(uuid4())},
            {**params["arguments"], "owner_id": "forged"},
            {**params["arguments"], "resource": "community:other:channel:" + str(uuid4())},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentPermissionDenied):
                event_webhooks.identity(self.grant, {**params, "arguments": arguments})
        with self.assertRaises(AgentPermissionDenied):
            event_webhooks.identity(self.grant, {**params, "name": "message.created",
                                                "arguments": {"resource": self.grant.resources[0]}})

    def test_reply_schema_has_no_caller_selected_destination_or_identity(self):
        schema = input_schema("reply_to_invocation")
        self.assertEqual(schema["required"], ["invocation_id", "text", "idempotency_key"])
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("resource", schema["properties"])
        self.assertNotIn("thread_id", schema["properties"])
        self.assertNotIn("bot_public_key", schema["properties"])

    def test_native_tools_are_private_and_invocation_reply_is_marked_as_a_write(self):
        request = SimpleNamespace(agent_principal=self.principal, headers={}, data={"jsonrpc": "2.0",
            "id": 1, "method": "tools/list", "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
        result = MCPView().post(request).data["result"]
        tools = {tool["name"]: tool for tool in result["tools"]}
        self.assertEqual(set(tools), {"get_profile", "list_channels", "get_invocation",
                                     "read_invocation_thread", "reply_to_invocation", "get_invocation_status"})
        self.assertFalse(tools["reply_to_invocation"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["read_invocation_thread"]["annotations"]["readOnlyHint"])
        self.assertEqual(result["cacheScope"], "private")
        self.assertEqual(result["ttlMs"], 0)


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=True,
                   AGENT_RUNTIME_NATIVE_ENABLED=True, SECRET_KEY="synthetic-dot-binding-test")
class DotBindingTests(SimpleTestCase):
    def setUp(self):
        self.grant = native_grant()
        self.params = subscription_params(self.grant)
        self.principal = SimpleNamespace(grant=self.grant)
        self.subscriptions = self.enterContext(patch.object(event_webhooks, "AgentSubscription"))
        self.subscriptions.objects.all.return_value.filter.return_value.first.return_value = None
        self.subscriptions.objects.filter.return_value.exclude.return_value = []
        self.subscriptions.objects.filter.return_value.count.return_value = 0
        self.enterContext(patch.object(event_webhooks, "locked", side_effect=lambda queryset: queryset))
        self.enterContext(patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(event_webhooks, "_authorized", return_value=self.principal))
        self.enterContext(patch.object(event_webhooks, "resolve_callback"))
        self.verify = self.enterContext(patch.object(event_webhooks, "verify_callback"))
        self.enterContext(patch("agent_runtime.events.lock_event_resource"))
        self.events = self.enterContext(patch.object(event_webhooks, "AgentEvent"))
        self.events.objects.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = 23
        self.enterContext(patch.object(event_webhooks, "audit"))

    def test_different_active_callback_requires_explicit_unsubscribe_before_replace(self):
        binding = SimpleNamespace(is_active=True, delivery={"expires_at": (NOW + timedelta(hours=1)).isoformat()})
        self.subscriptions.objects.filter.return_value.exclude.return_value = [binding]
        with self.assertRaisesRegex(AgentPermissionDenied, "delivery_binding_exists"):
            event_webhooks.subscribe(self.principal, self.params, now=NOW)
        self.verify.assert_not_called()
        self.subscriptions.objects.update_or_create.assert_not_called()
        self.assertTrue(binding.is_active)

    def test_binding_query_spans_all_grants_for_one_installation(self):
        event_webhooks.subscribe(self.principal, self.params, now=NOW)
        self.subscriptions.objects.filter.assert_any_call(
            grant__installation_id=self.grant.installation_id, resource=self.grant.resources[0],
            event_types=["agent.mentioned"], is_active=True, delivery__mode="webhook")
        defaults = self.subscriptions.objects.update_or_create.call_args.kwargs["defaults"]
        self.assertEqual(defaults["delivery"]["binding"]["installation_id"], str(self.grant.installation_id))
        self.assertEqual(defaults["cursor"], 23)
        self.assertEqual(defaults["delivery"]["listening_since"], NOW.isoformat())
        self.assertNotIn(SECRET, json.dumps(defaults["delivery"]))

    def test_expired_old_binding_can_be_replaced_and_stored_secret_is_purged(self):
        binding = SimpleNamespace(is_active=True, save=MagicMock(),
                                  delivery={"expires_at": NOW.isoformat(), "sealed": "expired-secret"})
        self.subscriptions.objects.filter.return_value.exclude.return_value = [binding]
        event_webhooks.subscribe(self.principal, self.params, now=NOW)
        self.assertFalse(binding.is_active)
        self.assertEqual(binding.delivery, {"mode": "webhook", "stopped": True})
        self.verify.assert_called_once()

    def test_reconnect_uses_same_row_but_starts_after_existing_events(self):
        existing = SimpleNamespace(is_active=True, grant_id=uuid4(), generation=1, cursor=2,
            delivery={"sealed": event_webhooks.seal({"url": self.params["delivery"]["url"],
                "secret": SECRET, "verified_at": NOW.isoformat()}),
                "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        self.subscriptions.objects.all.return_value.filter.return_value.first.return_value = existing
        result = event_webhooks.subscribe(self.principal, self.params, now=NOW)
        self.assertEqual(result["id"], str(event_webhooks.identity(self.grant, self.params)[0]))
        defaults = self.subscriptions.objects.update_or_create.call_args.kwargs["defaults"]
        self.assertEqual(defaults["grant"], self.grant)
        self.assertEqual(defaults["generation"], self.grant.generation)
        self.assertEqual(defaults["cursor"], 23)
        self.assertEqual(defaults["delivery"]["listening_since"], NOW.isoformat())

    def test_continuing_refresh_preserves_listening_start_but_resubscribe_resets_it(self):
        earlier = (NOW - timedelta(minutes=5)).isoformat()
        existing = SimpleNamespace(is_active=True, grant_id=self.grant.pk, generation=self.grant.generation,
            cursor=2, delivery={"sealed": event_webhooks.seal({"url": self.params["delivery"]["url"],
                "secret": SECRET, "verified_at": NOW.isoformat()}), "listening_since": earlier,
                "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        self.subscriptions.objects.all.return_value.filter.return_value.first.return_value = existing
        event_webhooks.subscribe(self.principal, self.params, now=NOW)
        delivery = self.subscriptions.objects.update_or_create.call_args.kwargs["defaults"]["delivery"]
        self.assertEqual(delivery["listening_since"], earlier)
        existing.is_active = False
        event_webhooks.subscribe(self.principal, self.params, now=NOW)
        delivery = self.subscriptions.objects.update_or_create.call_args.kwargs["defaults"]["delivery"]
        self.assertEqual(delivery["listening_since"], NOW.isoformat())

    def test_unsubscribe_can_stop_previous_grant_binding_idempotently(self):
        delivery = {key: value for key, value in self.params["delivery"].items() if key != "secret"}
        params = {**self.params, "delivery": delivery}
        self.assertEqual(event_webhooks.unsubscribe(self.principal, params), {})
        self.subscriptions.objects.filter.assert_called_once_with(
            pk=event_webhooks.identity(self.grant, params, subscribing=False)[0],
            grant__installation_id=self.grant.installation_id)
        self.subscriptions.objects.filter.return_value.update.assert_called_once_with(
            is_active=False, delivery={"mode": "webhook", "stopped": True})


class SyntheticDotCallback:
    """In-process receiver with synthetic key material; never opens a socket."""

    def __init__(self):
        self.events = []
        self.challenges = []

    def __call__(self, url, body, headers):
        material = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
        expected = "v1," + base64.b64encode(hmac.new(
            event_webhooks.signing_key(SECRET), material, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(headers["webhook-signature"], expected):
            return WebhookResponse(401, b"{}")
        payload = json.loads(body)
        if payload.get("type") == "verification":
            self.challenges.append(payload["challenge"])
            return WebhookResponse(200, json.dumps({"challenge": payload["challenge"]}).encode())
        if payload.get("eventId") != headers["webhook-id"]:
            return WebhookResponse(400, b"{}")
        self.events.append(payload)
        return WebhookResponse(202, b"{}")


class NativeTransportBoundsTests(SimpleTestCase):
    def test_native_response_bound_is_explicit_and_cannot_be_unlimited(self):
        for bound in (True, 0, 262145, "262144", None):
            with self.subTest(bound=bound), self.assertRaises(CallbackEndpointError) as caught:
                post_webhook("https://relay.invalid", b"{}", {}, max_response_bytes=bound)
            self.assertEqual(caught.exception.reason, "invalid_response_bound")

    def test_event_default_and_native_bounds_read_only_one_extra_byte(self):
        from agent_runtime.tests.test_event_webhooks import resolver_for
        for explicit, expected in (({}, 65537), ({"max_response_bytes": 262144}, 262145)):
            reader = MagicMock(return_value=b"{}")
            connection = SimpleNamespace(sock=None, request=lambda *a, **k: None,
                getresponse=lambda: SimpleNamespace(status=200, read=reader), close=lambda: None)
            with patch("agent_runtime.webhook_transport.http.client.HTTPSConnection", return_value=connection):
                self.assertEqual(post_webhook("https://relay.invalid", b"{}", {},
                    resolver=resolver_for("8.8.8.8"), **explicit), WebhookResponse(200, b"{}"))
            reader.assert_called_once_with(expected)


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=True,
                   AGENT_RUNTIME_NATIVE_ENABLED=True, SECRET_KEY="synthetic-dot-round-trip-test")
class CallbackFixtureRoundTripTests(SimpleTestCase):
    def setUp(self):
        self.grant = native_grant()
        # This standalone wire fixture has no SQL bridge binding. Unified
        # record fences are covered by the separate bridge contract tests.
        self.enterContext(patch("agent_runtime.unified_authority.assert_record_authority"))
        self.invocation_id = str(uuid4())
        self.event = SimpleNamespace(pk=15, source="mlai-chat", resource=self.grant.resources[0],
            event_type="agent.mentioned", payload={"invocation_id": self.invocation_id,
                "installation_id": str(self.grant.installation_id), "source_event_id": "a" * 64,
                "text": "Untrusted content must not be included in event metadata."}, actor_kind="human",
            chain_id=uuid4(), chain_depth=0, occurred_at=NOW)
        self.subscription = SimpleNamespace(pk=uuid4(), grant_id=self.grant.pk, generation=self.grant.generation,
            resource=self.grant.resources[0], cursor=0, is_active=True, event_types=["agent.mentioned"],
            allow_agent_events=False, max_chain_depth=1, save=MagicMock(), delivery={"mode": "webhook",
                "sealed": event_webhooks.seal({"url": "https://callback.invalid/selected-dot", "secret": SECRET}),
                "expires_at": (NOW + timedelta(hours=1)).isoformat(), "attempts": 0})
        self.enterContext(patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(event_webhooks, "_grant", return_value=self.grant))
        self.authorize = self.enterContext(patch.object(event_webhooks, "authorize"))
        subscriptions = self.enterContext(patch.object(event_webhooks, "AgentSubscription"))
        subscriptions.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [self.subscription.pk]
        subscriptions.objects.values_list.return_value.get.return_value = self.grant.pk
        self.enterContext(patch.object(event_webhooks, "locked", return_value=SimpleNamespace(get=lambda **kw: self.subscription)))
        events = self.enterContext(patch.object(event_webhooks, "AgentEvent"))
        events.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [self.event]
        self.deliverable = MagicMock(return_value=True)
        self.enterContext(patch.dict(sys.modules, {"agent_runtime.native_invocations":
            SimpleNamespace(invocation_deliverable=self.deliverable)}))

    def test_signed_challenge_and_metadata_only_mention_round_trip(self):
        receiver = SyntheticDotCallback()
        event_webhooks.verify_callback(self.subscription.pk, "https://callback.invalid/selected-dot", SECRET,
                                      transport=receiver)
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 1)
        self.assertEqual(len(receiver.challenges), 1)
        self.assertEqual(len(receiver.events), 1)
        event = receiver.events[0]
        self.assertEqual(set(event), {"eventId", "name", "timestamp", "data", "cursor"})
        self.assertEqual(event["name"], "agent.mentioned")
        self.assertEqual(event["data"], {"installation_id": str(self.grant.installation_id),
            "invocation_id": self.invocation_id, "source_event_id": "a" * 64, "resource": self.grant.resources[0]})
        self.assertEqual(self.subscription.cursor, 15)
        self.assertIn("last_delivered_at", self.subscription.delivery)
        self.deliverable.assert_called_once_with(self.grant, self.invocation_id,
                                                expected_resource=self.grant.resources[0], now=NOW)

    def test_other_installations_are_filtered_before_invocation_lookup(self):
        self.event.payload["installation_id"] = str(uuid4())
        receiver = SyntheticDotCallback()
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 0)
        self.assertEqual(receiver.events, [])
        self.deliverable.assert_not_called()
        self.assertEqual(self.subscription.cursor, 15)

    def test_expired_cancelled_or_revoked_invocation_is_not_delivered(self):
        self.deliverable.return_value = False
        receiver = SyntheticDotCallback()
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 0)
        self.assertEqual(receiver.events, [])
        self.assertEqual(self.subscription.cursor, 15)

    def test_source_outage_preserves_pending_invocation_for_retry(self):
        self.deliverable.side_effect = AgentPermissionDenied("resource_unavailable")
        receiver = SyntheticDotCallback()
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 0)
        self.assertEqual(self.subscription.cursor, 0)
        self.assertTrue(self.subscription.is_active)
        self.assertEqual(receiver.events, [])

    def test_receiver_gone_or_payload_rejected_stops_retries(self):
        for status in (410, 413):
            with self.subTest(status=status):
                self.subscription.is_active = True
                self.subscription.cursor = 0
                self.subscription.delivery["attempts"] = 0
                self.subscription.delivery.pop("retry_at", None)
                transport = MagicMock(return_value=WebhookResponse(status, b"{}"))
                self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=transport), 0)
                self.assertFalse(self.subscription.is_active)
                self.assertEqual(self.subscription.cursor, 0)
                self.assertEqual(self.subscription.delivery["last_error"], f"http_{status}")
                transport.assert_called_once()

    def test_native_gate_disable_stops_existing_delivery(self):
        receiver = SyntheticDotCallback()
        with override_settings(AGENT_RUNTIME_NATIVE_ENABLED=False):
            self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 0)
        self.assertFalse(self.subscription.is_active)
        self.assertNotIn("sealed", self.subscription.delivery)
        self.assertEqual(receiver.events, [])

    def test_grant_rebinding_while_dispatch_waits_does_not_consume_new_events(self):
        self.subscription.grant_id = uuid4()
        receiver = SyntheticDotCallback()
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=receiver), 0)
        self.assertEqual(self.subscription.cursor, 0)
        self.assertTrue(self.subscription.is_active)
        self.authorize.assert_not_called()
        self.assertEqual(receiver.events, [])

    def test_uncertain_delivery_preserves_id_and_new_signing_timestamp(self):
        attempts = []
        def transport(url, body, headers):
            attempts.append((body, headers))
            return WebhookResponse(503, b"{}")
        event_webhooks.dispatch_webhooks(now=NOW, transport=transport)
        event_webhooks.dispatch_webhooks(now=NOW + timedelta(seconds=30), transport=transport)
        self.assertEqual(attempts[0][0], attempts[1][0])
        self.assertEqual(attempts[0][1]["webhook-id"], attempts[1][1]["webhook-id"])
        self.assertNotEqual(attempts[0][1]["webhook-timestamp"], attempts[1][1]["webhook-timestamp"])
        self.assertEqual(self.subscription.cursor, 0)
