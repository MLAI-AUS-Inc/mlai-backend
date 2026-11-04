import base64
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import hmac
import json
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from agent_runtime import event_webhooks
from agent_runtime.event_webhooks import identity, seal, unseal, signed_headers, signing_key, verify_callback
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.webhook_transport import CallbackEndpointError, WebhookResponse, post_webhook, resolve_callback
from agent_runtime.tests.test_core import NOW, grant_fixture


SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()


def resolver_for(*ips):
    return lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


class CallbackSecurityTests(unittest.TestCase):
    def test_slow_dns_is_bounded_before_opening_a_socket(self):
        released = threading.Event()
        def resolver(*args, **kwargs):
            released.wait(timeout=1)
            return resolver_for("8.8.8.8")()
        try:
            with self.assertRaises(CallbackEndpointError) as caught:
                resolve_callback("https://receiver.invalid/callback", resolver=resolver, dns_timeout=0.01)
            self.assertEqual(caught.exception.reason, "dns_timeout")
        finally:
            released.set()

    def test_unicode_target_is_encoded_and_control_url_rejected(self):
        self.assertEqual(resolve_callback("https://receiver.invalid/結果", resolver=resolver_for("8.8.8.8"))[2], "/%E7%B5%90%E6%9E%9C")
        with self.assertRaises(CallbackEndpointError):
            resolve_callback("https://receiver.invalid/a\x00b", resolver=resolver_for("8.8.8.8"))

    def test_private_mixed_local_and_transition_destinations_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:7f00:1::", "192.0.2.1"):
            with self.subTest(address=address), self.assertRaises(CallbackEndpointError):
                resolve_callback("https://receiver.invalid/callback", resolver=resolver_for("8.8.8.8", address))

    def test_only_https_without_credentials_fragment_or_alternate_port(self):
        for url in ("http://receiver.invalid", "https://user:secret@receiver.invalid", "https://receiver.invalid:8443", "https://receiver.invalid/#fragment"):
            with self.assertRaises(CallbackEndpointError):
                resolve_callback(url, resolver=resolver_for("8.8.8.8"))

    def test_public_destination_keeps_original_hostname(self):
        self.assertEqual(resolve_callback("https://receiver.invalid/path?token=public-id", resolver=resolver_for("8.8.8.8")),
                         ("receiver.invalid", "8.8.8.8", "/path?token=public-id"))

    def test_actual_connection_is_pinned_not_resolved_again(self):
        class Connection:
            sock = None
            def __init__(self, hostname, port, **kwargs):
                self.hostname = hostname
                self.port = port
            def request(self, method, target, **kwargs):
                self._create_connection((self.hostname, self.port), 2)
            def getresponse(self):
                return SimpleNamespace(status=204, read=lambda n: b"")
            def close(self): pass
        with patch("agent_runtime.webhook_transport.http.client.HTTPSConnection", Connection), patch("socket.create_connection") as connect:
            response = post_webhook("https://receiver.invalid/callback", b"{}", {}, resolver=resolver_for("8.8.8.8"))
        self.assertEqual(response.status, 204)
        self.assertEqual(connect.call_args.args[0], ("8.8.8.8", 443))

    def test_redirect_is_never_followed(self):
        connection = SimpleNamespace(sock=None, request=lambda *a, **k: None,
                                     getresponse=lambda: SimpleNamespace(status=307, read=lambda n: b""), close=lambda: None)
        with patch("agent_runtime.webhook_transport.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(CallbackEndpointError) as caught:
                post_webhook("https://receiver.invalid", b"{}", {}, resolver=resolver_for("8.8.8.8"))
        self.assertEqual(caught.exception.reason, "redirect_rejected")


class EventSigningTests(unittest.TestCase):
    def test_key_sizes_and_prefix_are_validated(self):
        self.assertEqual(signing_key(SECRET), b"s" * 32)
        for invalid in ("s" * 32, "whsec_" + base64.b64encode(b"short").decode(), "whsec_!invalid", "whsec_" + base64.b64encode(b"s" * 65).decode()):
            with self.assertRaises(CallbackEndpointError):
                signing_key(invalid)

    def test_standard_webhook_signature_binds_exact_bytes_and_event_id(self):
        now, body = timezone.now(), b'{"id":"one"}'
        headers = signed_headers("subscription", "event", body, SECRET, now=now)
        expected = hmac.new(b"s" * 32, b"event." + str(int(now.timestamp())).encode() + b"." + body, hashlib.sha256).digest()
        self.assertEqual(headers["webhook-signature"], "v1," + base64.b64encode(expected).decode())
        self.assertNotEqual(headers["webhook-signature"], signed_headers("subscription", "other", body, SECRET, now=now)["webhook-signature"])

    def test_rotation_includes_both_signatures(self):
        old = "whsec_" + base64.b64encode(b"o" * 32).decode()
        headers = signed_headers("subscription", "event", b"{}", SECRET, old_secret=old)
        self.assertEqual(len(headers["webhook-signature"].split(" ")), 2)

    def test_challenge_must_echo_and_has_fresh_id(self):
        seen = []
        def transport(url, body, headers):
            seen.append((body, headers))
            return WebhookResponse(200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode())
        verify_callback("subscription", "https://receiver.invalid", SECRET, transport=transport)
        verify_callback("subscription", "https://receiver.invalid", SECRET, transport=transport)
        self.assertNotEqual(seen[0][0], seen[1][0])
        self.assertNotEqual(seen[0][1]["webhook-id"], seen[1][1]["webhook-id"])
        with self.assertRaises(CallbackEndpointError):
            verify_callback("subscription", "https://receiver.invalid", SECRET,
                            transport=lambda *args: WebhookResponse(200, b'{"challenge":"wrong"}'))

    @override_settings(SECRET_KEY="synthetic-test-key")
    def test_callback_credentials_are_encrypted_and_integrity_protected(self):
        value = {"url": "https://receiver.invalid", "secret": SECRET}
        ciphertext = seal(value)
        self.assertNotIn(SECRET, ciphertext)
        self.assertEqual(unseal(ciphertext), value)
        with self.assertRaises(CallbackEndpointError):
            unseal(ciphertext[:-2] + "zz")

    def test_identity_is_owner_scoped_and_canonical(self):
        grant = SimpleNamespace(pk=uuid4())
        params = {"name": "message.created", "arguments": {"resource": "slack:T1:C1"},
                  "delivery": {"mode": "webhook", "url": "https://receiver.invalid", "secret": SECRET}}
        one = identity(grant, params)[0]
        self.assertEqual(one, identity(grant, {**params, "ttlMs": 1000})[0])
        self.assertNotEqual(one, identity(SimpleNamespace(pk=uuid4()), params)[0])
        with self.assertRaises(AgentPermissionDenied):
            identity(grant, {**params, "owner_id": "forged"})


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=True,
                   SECRET_KEY="synthetic-webhook-lifecycle-test")
class WebhookLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.grant = grant_fixture()
        self.subscription = SimpleNamespace(pk=uuid4(), grant_id=self.grant.pk, generation=self.grant.generation,
            resource=self.grant.resources[0], cursor=0, is_active=True, event_types=["message.created"],
            allow_agent_events=False, max_chain_depth=1, save=MagicMock(),
            delivery={"mode": "webhook", "sealed": seal({"url": "https://receiver.invalid", "secret": SECRET}),
                      "expires_at": (NOW + timedelta(hours=1)).isoformat(), "attempts": 0})
        self.event = SimpleNamespace(pk=15, source="slack", resource=self.subscription.resource,
            event_type="message.created", payload={"message_ts": "1.001"}, actor_kind="human",
            chain_id=uuid4(), chain_depth=0, occurred_at=NOW)
        self.enterContext(patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(event_webhooks, "_grant", return_value=self.grant))
        self.authorization = self.enterContext(patch.object(event_webhooks, "authorize"))
        subscriptions = self.enterContext(patch.object(event_webhooks, "AgentSubscription"))
        subscriptions.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [self.subscription.pk]
        subscriptions.objects.values_list.return_value.get.return_value = self.grant.pk
        self.enterContext(patch.object(event_webhooks, "locked", return_value=SimpleNamespace(get=lambda **kw: self.subscription)))
        events = self.enterContext(patch.object(event_webhooks, "AgentEvent"))
        events.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [self.event]

    def test_uncertain_delivery_retries_same_event_id_without_advancing_cursor(self):
        sent = []
        def transport(url, body, headers):
            sent.append((body, headers))
            raise CallbackEndpointError("timeout")
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=transport), 0)
        self.assertEqual(self.subscription.cursor, 0)
        self.assertTrue(self.subscription.is_active)
        self.assertEqual(self.subscription.delivery["attempts"], 1)
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW + timedelta(seconds=30), transport=transport), 0)
        self.assertEqual(sent[0][0], sent[1][0])
        self.assertEqual(sent[0][1]["webhook-id"], sent[1][1]["webhook-id"])
        self.assertEqual(self.subscription.cursor, 0)

    def test_confirmed_delivery_advances_only_that_subscription(self):
        transport = MagicMock(return_value=WebhookResponse(204, b""))
        self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=transport), 1)
        self.assertEqual(self.subscription.cursor, 15)
        self.assertEqual(self.subscription.delivery["attempts"], 0)
        self.authorization.assert_called_once_with(self.grant, "events:read", self.subscription.resource,
            expected_generation=self.grant.generation, access_checker=event_webhooks.live_access)

    def test_definite_revocation_stops_but_source_outage_keeps_pending(self):
        transport = MagicMock()
        self.authorization.side_effect = AgentPermissionDenied("resource_unavailable")
        event_webhooks.dispatch_webhooks(now=NOW, transport=transport)
        self.assertTrue(self.subscription.is_active)
        self.assertEqual(self.subscription.cursor, 0)
        self.authorization.side_effect = AgentPermissionDenied("grant_changed")
        event_webhooks.dispatch_webhooks(now=NOW, transport=transport)
        self.assertFalse(self.subscription.is_active)
        self.assertNotIn("sealed", self.subscription.delivery)
        transport.assert_not_called()

    def test_expiry_and_retry_backoff_prevent_network_delivery(self):
        transport = MagicMock()
        self.subscription.delivery["retry_at"] = (NOW + timedelta(minutes=1)).isoformat()
        event_webhooks.dispatch_webhooks(now=NOW, transport=transport)
        self.subscription.delivery["expires_at"] = NOW.isoformat()
        event_webhooks.dispatch_webhooks(now=NOW, transport=transport)
        self.assertFalse(self.subscription.is_active)
        transport.assert_not_called()
