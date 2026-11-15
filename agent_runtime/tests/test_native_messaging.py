"""Proactive Dot messages (send_message, open_dm, list_my_conversations), without a database."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from agent_runtime import native_crypto, native_messaging as nm
from agent_runtime.policy import AgentPermissionDenied

COMMUNITY = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
OWNER_KEY = "a" * 64
CONFIG = (COMMUNITY, {"relay_url": "https://chat.mlai.au"})
ON = dict(AGENT_RUNTIME_DOT_MESSAGING_ENABLED=True, AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=True)


def native():
    installation = SimpleNamespace(pk=uuid4(), app_id="openai-dot", state="active", is_active=True,
        community_id=COMMUNITY, owner_device_id=uuid4(), owner_device=SimpleNamespace(public_key=OWNER_KEY),
        owner=SimpleNamespace(pk=1), public_key="b" * 64, sealed_signing_key="sealed")
    grant = SimpleNamespace(installation=installation, generation=3,
                            expires_at=timezone.now() + timedelta(days=10))
    return SimpleNamespace(grant=grant)


def counts(value=0):
    query = MagicMock()
    query.count.return_value = value
    query.first.return_value = None
    return query


@override_settings(**ON)
@patch.object(nm.native_sources, "community_config", return_value=CONFIG)
@patch.object(nm.native_sources, "current_device", return_value=SimpleNamespace(public_key=OWNER_KEY))
class DirectiveTests(SimpleTestCase):
    def test_conversations_is_a_short_personal_delegation_without_destination(self, *_):
        with patch.object(nm.native_sources, "_request", return_value={"conversations": [
                {"id": CHANNEL, "type": "stream", "name": "general"},
                {"id": "x", "type": "forum", "name": "skip"},
                {"id": CHANNEL, "type": "dm", "name": "", "participants": [OWNER_KEY, "bad", "c" * 64]}]}) as request:
            result = nm.execute(native(), "list_my_conversations", {"limit": 5})
        directive = request.call_args.args[2][0]["native_agent"]
        self.assertEqual(directive["operation"], "conversations")
        self.assertEqual(directive["execution_mode"], "personal_context")
        self.assertNotIn("channel_id", directive)
        self.assertLessEqual(directive["expires_at"], int(timezone.now().timestamp()) + 840)
        self.assertEqual([row["type"] for row in result["conversations"]], ["channel", "dm"])
        self.assertEqual(result["conversations"][1]["participants"], [OWNER_KEY, "c" * 64])

    def test_disabled_flag_refuses_everything(self, *_):
        with override_settings(AGENT_RUNTIME_DOT_MESSAGING_ENABLED=False):
            with self.assertRaises(AgentPermissionDenied):
                nm.execute(native(), "list_my_conversations", {})

    def test_paused_dot_cannot_message(self, *_):
        value = native()
        value.grant.installation.state = "paused"
        with self.assertRaises(AgentPermissionDenied):
            nm.execute(value, "list_my_conversations", {})


@override_settings(**ON)
@patch.object(nm.native_sources, "community_config", return_value=CONFIG)
@patch.object(nm.native_sources, "current_device", return_value=SimpleNamespace(public_key=OWNER_KEY))
@patch.object(nm.runtime, "audit")
class OpenDmTests(SimpleTestCase):
    def test_keys_are_sent_as_participants_and_new_dms_are_audited(self, audit, *_):
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=counts()), \
                patch.object(nm.native_sources, "_request", return_value={"channel_id": CHANNEL, "created": True}) as request:
            result = nm.execute(native(), "open_dm", {"members": ["C" * 64, "c" * 64]})
        directive = request.call_args.args[2][0]["native_agent"]
        self.assertEqual(directive["operation"], "open_dm")
        self.assertEqual(directive["participants"], ["c" * 64])
        self.assertEqual(result, {"conversation_id": CHANNEL, "created": True})
        self.assertEqual(audit.call_args.args[0], nm.DM_EVENT)
        self.assertNotIn("c" * 64, str(audit.call_args))

    def test_daily_new_dm_cap(self, *_):
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=counts(nm.DAILY_NEW_DMS)), \
                patch.object(nm.native_sources, "_request") as request:
            with self.assertRaises(AgentPermissionDenied):
                nm.execute(native(), "open_dm", {"members": ["c" * 64]})
        request.assert_not_called()

    def test_member_list_is_bounded_and_validated(self, *_):
        for members in ([], ["c" * 64] * 8, ["not a member"], [5]):
            with self.assertRaises(ValidationError):
                nm.execute(native(), "open_dm", {"members": members})

    def test_owner_cannot_be_listed(self, *_):
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=counts()):
            with self.assertRaises(ValidationError):
                nm.execute(native(), "open_dm", {"members": [OWNER_KEY]})


@override_settings(**ON)
@patch.object(nm.native_sources, "community_config", return_value=CONFIG)
@patch.object(nm.native_sources, "current_device", return_value=SimpleNamespace(public_key=OWNER_KEY))
@patch.object(nm.runtime, "audit")
class SendTests(SimpleTestCase):
    def signed(self, key, *, kind, created_at, tags, content):
        return {"id": "e" * 64, "kind": kind, "tags": tags, "content": content, "created_at": created_at}

    def test_post_is_top_level_bot_message_in_the_conversation(self, audit, *_):
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=counts()), \
                patch.object(nm.native_crypto, "unseal_key", return_value="k"), \
                patch.object(nm.native_crypto, "sign_event", side_effect=self.signed), \
                patch.object(nm.native_sources, "_request", return_value={"event_id": "e" * 64, "accepted": True}) as request:
            result = nm.execute(native(), "send_message", {"conversation_id": CHANNEL, "text": "Daily update",
                                                             "idempotency_key": "daily-2026-11-15"})
        path, event = request.call_args.args[1], request.call_args.args[2]
        self.assertEqual(path, "/events")
        names = [tag[0] for tag in event["tags"]]
        self.assertEqual(sorted(names), ["client", "h", "native_installation", "native_invocation"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(audit.call_args.args[0], nm.SEND_EVENT)
        self.assertNotIn("text", audit.call_args.kwargs)

    def test_same_key_never_posts_twice(self, *_):
        previous = MagicMock()
        previous.first.return_value = SimpleNamespace(metadata={"event_id": "e" * 64})
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=previous), \
                patch.object(nm.native_sources, "_request") as request:
            result = nm.execute(native(), "send_message", {"conversation_id": CHANNEL, "text": "again",
                                                             "idempotency_key": "daily-2026-11-15"})
        request.assert_not_called()
        self.assertEqual(result["event_id"], "e" * 64)

    def test_daily_send_cap(self, *_):
        with patch.object(nm.AgentAuditEvent.objects, "filter", return_value=counts(nm.DAILY_SENDS)), \
                patch.object(nm.native_sources, "_request") as request:
            with self.assertRaises(AgentPermissionDenied):
                nm.execute(native(), "send_message", {"conversation_id": CHANNEL, "text": "hi",
                                                      "idempotency_key": "daily-2026-11-15"})
        request.assert_not_called()

    def test_bad_input_is_refused(self, *_):
        for arguments in ({"conversation_id": "x", "text": "hi", "idempotency_key": "12345678"},
                          {"conversation_id": CHANNEL, "text": " ", "idempotency_key": "12345678"},
                          {"conversation_id": CHANNEL, "text": "hi", "idempotency_key": "short"},
                          {"conversation_id": CHANNEL, "text": "x" * 12001, "idempotency_key": "12345678"}):
            with self.assertRaises(ValidationError):
                nm.execute(native(), "send_message", arguments)


class CatalogueTests(SimpleTestCase):
    def test_tools_and_scope(self):
        self.assertEqual(nm.NAMES, {"send_message", "open_dm", "list_my_conversations"})
        from startup_updates.mcp import config, native_agents
        self.assertIn(nm.SCOPE, config.ALL_SCOPES)
        for name in nm.NAMES:
            self.assertIn(nm.SCOPE, native_agents.TOOL_SCOPES[name])
