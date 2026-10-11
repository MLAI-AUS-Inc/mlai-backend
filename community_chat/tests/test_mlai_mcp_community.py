"""Database-free native-community grant, signature and replay boundaries."""
import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from coincurve import PrivateKey, PublicKeyXOnly
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from community_chat import mcp_relay as relay
from startup_updates.mcp import community

CHANNEL = "04d89807-5884-4b0e-83cc-311abf13b648"
OTHER = "7e399ce7-8d85-4970-ad8e-aa13bc3b028c"
REQUEST = "8591b71a-afbc-47f2-97c2-bcab1c005ee5"
OWNER = PrivateKey(bytes.fromhex("01" * 32)).public_key.format(compressed=True)[1:].hex()
ORIGIN = "https://chat.example.test"
SETTINGS = {"MLAI_MCP_COMMUNITY_ENABLED": True, "MLAI_MCP_COMMUNITY_RELAY_URL": ORIGIN,
    "MLAI_MCP_COMMUNITY_SECRET": "synthetic-community-delegation-secret-32-bytes", "DEBUG": True}


def resource():
    return {"id": CHANNEL, "name": "engineering", "channelType": "stream", "ownerPubkey": OWNER,
        "communityId": OTHER, "relayUrl": ORIGIN}


def principal():
    return Obj(user=Obj(pk=7), grant={"id": "grant", "session_id": "session", "expiresAt": int(time.time()) + 3600,
        "clientName": "Claude", "community_channels": [resource()]})


def event(event_id="a" * 64):
    return {"id": event_id, "pubkey": OWNER, "kind": 9, "created_at": int(time.time()), "tags": [["h", CHANNEL]], "content": "Member message"}


@override_settings(**SETTINGS)
class NativeRelayTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_signed_actor_is_distinct_and_event_signature_is_valid(self):
        item = relay.signed_event("grant", content="An update", channel_id=CHANNEL,
            request_id=REQUEST, owner=OWNER, client_name="Claude")
        self.assertNotEqual(item["pubkey"], OWNER)
        serialized = json.dumps([0, item["pubkey"], item["created_at"], item["kind"], item["tags"], item["content"]], ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(serialized).hexdigest(), item["id"])
        self.assertTrue(PublicKeyXOnly(bytes.fromhex(item["pubkey"])).verify(bytes.fromhex(item["sig"]), bytes.fromhex(item["id"])))
        self.assertIn(["mlai-mcp", OWNER, REQUEST], item["tags"])
        self.assertTrue(item["content"].endswith(f"_Sent with Claude via MLAI MCP for {OWNER[:8]}._"))
        self.assertNotEqual(relay.actor_key("different-grant").secret, relay.actor_key("grant").secret)

    def test_delegation_binds_request_body_resource_scope_and_owner(self):
        response = MagicMock(status_code=200)
        response.json.return_value = []
        with patch.object(relay.requests, "post", return_value=response) as post:
            relay.request("/query", owner=OWNER, scope="community:read", grant_id="grant",
                channel_ids=[CHANNEL], community_id=OTHER, payload=[{"kinds": [9], "#h": [CHANNEL], "limit": 5}])
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        encoded = kwargs["headers"]["X-MLAI-MCP-Delegation"].encode()
        claims = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
        self.assertEqual(url, ORIGIN + "/query")
        self.assertEqual(claims["bodyHash"], hashlib.sha256(kwargs["data"]).hexdigest())
        self.assertEqual(claims["ownerPubkey"], OWNER)
        self.assertEqual(claims["channelIds"], [CHANNEL])
        self.assertEqual(claims["scope"], "community:read")
        self.assertEqual(claims["expiresAt"] - claims["issuedAt"], 10)
        expected = hmac.new(SETTINGS["MLAI_MCP_COMMUNITY_SECRET"].encode(), b"mlai-mcp-delegation-v1:" + encoded, hashlib.sha256).hexdigest()
        self.assertEqual(expected, kwargs["headers"]["X-MLAI-MCP-Signature"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_no_configuration_weak_secret_and_redirects_fail_closed(self):
        for changes in ({"MLAI_MCP_COMMUNITY_ENABLED": False}, {"MLAI_MCP_COMMUNITY_SECRET": "short"},
                {"MLAI_MCP_COMMUNITY_RELAY_URL": "https://chat.example.test/path"},
                {"MLAI_MCP_COMMUNITY_RELAY_URL": "https://user:secret@chat.example.test"}):
            with self.subTest(changes=changes), override_settings(**changes), self.assertRaises(PermissionDenied):
                relay.configuration()
        with patch.object(relay.requests, "post", return_value=Obj(status_code=302)), self.assertRaises(ValidationError):
            relay.request("/query", owner=OWNER, scope="community:read", grant_id="grant", channel_ids=[CHANNEL], payload=[])

    def test_native_device_must_be_verified_and_current(self):
        user = Obj(pk=7, is_active=True, auth_version=1)
        session = Obj(user_id=7, user=user, public_key=OWNER, auth_version=1, revoked_at=None,
            expires_at=timezone.now() + timedelta(hours=1))
        with patch.object(relay.CommunityChatDevice, "objects") as manager:
            manager.filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                relay.current_owner(user, session)
            manager.filter.return_value.first.return_value = Obj()
            self.assertEqual(relay.current_owner(user, session), OWNER)
            self.assertEqual(manager.filter.call_args.kwargs["status"], "verified")
        session.revoked_at = timezone.now()
        with self.assertRaises(AuthenticationFailed):
            relay.current_owner(user, session)

    def test_channel_selection_stores_authoritative_records_and_rejects_wildcards(self):
        with patch.object(relay, "consent_channels", return_value=[resource()]):
            self.assertEqual(relay.validate_channel_selection(Obj(), Obj(), [CHANNEL]), [resource()])
            with self.assertRaises(PermissionDenied):
                relay.validate_channel_selection(Obj(), Obj(), [OTHER])
            for value in ([], ["*"], [CHANNEL, CHANNEL], [resource()]):
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    relay.validate_channel_selection(Obj(), Obj(), value)


@override_settings(**SETTINGS)
class CommunityToolTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.principal = principal()
        self.grant_patch = patch.object(community, "valid_grant", return_value=self.principal)
        self.session_patch = patch.object(relay, "session_for", return_value=(Obj(), OWNER))
        self.transaction_patch = patch.object(community.transaction.Atomic, "__enter__", return_value=None)
        self.transaction_exit_patch = patch.object(community.transaction.Atomic, "__exit__", return_value=False)
        self.user_patch = patch.object(community, "get_user_model", return_value=Obj(objects=MagicMock()))
        self.grant = self.grant_patch.start()
        self.session_patch.start()
        self.transaction_patch.start()
        self.transaction_exit_patch.start()
        self.user_patch.start()
        self.addCleanup(self.grant_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.transaction_patch.stop)
        self.addCleanup(self.transaction_exit_patch.stop)
        self.addCleanup(self.user_patch.stop)

    def test_wrong_channel_or_forged_author_never_reaches_relay(self):
        for arguments in ({"channelId": OTHER}, {"channelId": CHANNEL, "ownerPubkey": OWNER}, {"channelId": CHANNEL, "relayUrl": "https://attacker.test"}):
            with patch.object(relay, "request") as request, self.assertRaises((ValidationError, PermissionDenied)):
                community.call_tool(self.principal, "read_community_messages", arguments)
            request.assert_not_called()

    def test_read_scope_rechecked_before_current_identity_or_network(self):
        self.grant.side_effect = PermissionDenied("revoked")
        with patch.object(relay, "request") as request, self.assertRaises(PermissionDenied):
            community.call_tool(self.principal, "read_community_messages", {"channelId": CHANNEL})
        request.assert_not_called()

    def test_inconsistent_relay_or_device_grant_is_rejected(self):
        for field, value in (("relayUrl", "https://other.test"), ("ownerPubkey", "b" * 64), ("channelType", "dm")):
            original = self.principal.grant["community_channels"][0][field]
            self.principal.grant["community_channels"][0][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionDenied):
                community.call_tool(self.principal, "read_community_messages", {"channelId": CHANNEL})
            self.principal.grant["community_channels"][0][field] = original

    def test_channel_list_intersects_current_membership_with_exact_opt_in(self):
        with patch.object(relay, "consent_channels", return_value=[{**resource(), "id": OTHER}]):
            self.assertEqual(community.call_tool(self.principal, "list_community_channels", {}), {"channels": []})

    def test_bounded_read_query_contains_explicit_kinds_and_h_channel(self):
        with patch.object(relay, "request", return_value=[event()]) as request:
            result = community.call_tool(self.principal, "read_community_messages", {"channelId": CHANNEL, "before": 1000, "limit": 10})
        self.assertEqual(result["messages"][0]["id"], "a" * 64)
        self.assertEqual(request.call_args.kwargs["payload"], [{"kinds": relay.MESSAGE_KINDS, "#h": [CHANNEL], "limit": 10, "until": 1000}])
        for value in (True, 0, 101, "10"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                community.call_tool(self.principal, "read_community_messages", {"channelId": CHANNEL, "limit": value})

    def test_out_of_scope_response_or_reply_target_is_rejected(self):
        foreign = event()
        foreign["tags"] = [["h", OTHER]]
        with patch.object(relay, "request", return_value=[foreign]), self.assertRaises(PermissionDenied):
            community.call_tool(self.principal, "read_community_messages", {"channelId": CHANNEL})
        with patch.object(relay, "request", return_value=[]), self.assertRaises(ValidationError):
            community.call_tool(self.principal, "reply_community_message", {"channelId": CHANNEL,
                "requestId": REQUEST, "eventId": "a" * 64, "content": "Reply"})

    def test_changed_content_retry_rejected_and_same_send_reconciled(self):
        args = {"channelId": CHANNEL, "requestId": REQUEST, "content": "Update"}
        sent = []
        def response(path, **kwargs):
            if path == "/events":
                sent.append(kwargs["payload"])
                return {"event_id": kwargs["payload"]["id"], "accepted": True}
            return sent
        with patch.object(relay, "request", side_effect=response) as request:
            first = community.call_tool(self.principal, "post_community_message", args)
            replay = community.call_tool(self.principal, "post_community_message", args)
            with self.assertRaises(ValidationError):
                community.call_tool(self.principal, "post_community_message", {**args, "content": "Changed"})
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["pubkey"], relay.actor_key("grant").public_key.format(compressed=True)[1:].hex())
        self.assertEqual(request.call_args.kwargs["scope"], "community:write")

    def test_lost_send_ack_retries_the_identical_signed_event(self):
        args = {"channelId": CHANNEL, "requestId": REQUEST, "content": "Update"}
        sent = []
        def response(path, **kwargs):
            if path == "/query":
                return []
            sent.append(kwargs["payload"])
            if len(sent) == 1:
                raise ValidationError("lost ACK")
            return {"event_id": kwargs["payload"]["id"], "accepted": True}
        with patch.object(relay, "request", side_effect=response):
            with self.assertRaises(ValidationError):
                community.call_tool(self.principal, "post_community_message", args)
            community.call_tool(self.principal, "post_community_message", args)
        self.assertEqual(sent[0], sent[1])

    def test_send_receipt_cannot_revive_revoked_write_grant(self):
        self.grant.side_effect = PermissionDenied("revoked")
        with patch.object(relay, "request") as request, self.assertRaises(PermissionDenied):
            community.call_tool(self.principal, "post_community_message", {"channelId": CHANNEL,
                "requestId": REQUEST, "content": "Update"})
        request.assert_not_called()
