"""Database-free regression tests; run with scripts/test_without_database.py."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from integrations.services.community_bridge.broadcast_mentions import (
    BroadcastPermissionDenied,
    has_broadcast_mentions,
    render_broadcast_mentions,
    prepare_slack_broadcasts,
    prepare_private_slack_broadcasts,
)


class BroadcastMentionTests(unittest.TestCase):
    def test_both_spellings_and_all_three_audiences(self):
        self.assertEqual(
            render_broadcast_mentions(
                "@channel <!here> @everyone", role="member",
                channel={"is_general": True, "num_members": 20},
            ),
            "<!channel> <!here> <!everyone>",
        )

    def test_admin_everyone_stays_in_current_channel(self):
        self.assertEqual(
            render_broadcast_mentions("@everyone @here", role="admin", channel={"is_general": False}),
            "<!channel> <!here>",
        )

    def test_members_and_moderators_follow_slack_default_permissions(self):
        for role in ("member", "moderator"):
            with self.subTest(role=role):
                self.assertEqual(
                    render_broadcast_mentions("@channel @here", role=role, channel={"num_members": 9_999}),
                    "<!channel> <!here>",
                )
                with self.assertRaises(BroadcastPermissionDenied):
                    render_broadcast_mentions("@everyone", role=role, channel={"is_general": False})
                with self.assertRaises(BroadcastPermissionDenied):
                    render_broadcast_mentions("@here", role=role, channel={"num_members": 10_000})

    def test_guest_cannot_everyone_in_general(self):
        with self.assertRaises(BroadcastPermissionDenied):
            render_broadcast_mentions("@everyone", role="guest", channel={"is_general": True})

    def test_missing_member_count_fails_closed(self):
        with self.assertRaises(RuntimeError):
            render_broadcast_mentions("@channel", role="member", channel={})

    def test_threads_never_broadcast(self):
        self.assertEqual(
            render_broadcast_mentions("<!channel> @here <!everyone>", role="member", channel={}, is_thread=True),
            "@channel @here @everyone",
        )

    def test_code_links_escapes_and_longer_identifiers_stay_literal(self):
        for name in ("channel", "here", "everyone"):
            for text in (
                f"me@{name}.org", f"@{name}s", f"@@{name}", f"\\@{name}",
                f"\\<!{name}>", f"`@{name}`", f"```\n@{name}\n```",
                f"`<!{name}>`", f"[@{name}](https://example.test)",
                f"https://example.test/@{name}", f"    @{name}", f"````\n```\n@{name}\n````",
                f"~~~\n@{name}\n~~~", f"```\n@{name}",
            ):
                with self.subTest(text=text):
                    self.assertFalse(has_broadcast_mentions(text))
                    self.assertEqual(render_broadcast_mentions(text, role="admin", channel={}), text.replace(f"<!{name}>", f"&lt;!{name}&gt;"))

    def test_plain_mentions_next_to_code_still_convert(self):
        self.assertEqual(
            render_broadcast_mentions("`@here` @channel", role="admin", channel={}),
            "`@here` <!channel>",
        )


class BroadcastDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = {
            "source_platform": "buzz", "delivery_type": "create",
            "source_parent_message_id": "", "target_channel_id": "CGENERAL",
            "payload": {"source_author_id": "ab" * 32, "metadata": {"role": "admin"}},
            "channel": {"slack_workspace_id": "TMLAI"},
        }
        self.role = self.enterContext(patch("community_chat.permissions.device_chat_role", return_value="member"))
        self.client = self.enterContext(patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client"))
        self.client.return_value.conversations_info.return_value = {
            "ok": True, "channel": {"id": "CGENERAL", "is_general": True, "num_members": 5},
        }
        self.identity = self.enterContext(patch(
            "integrations.services.community_bridge.identity.verified_identity_for_buzz", return_value=None,
        ))

    def test_sender_metadata_cannot_grant_admin_override(self):
        self.client.return_value.conversations_info.return_value["channel"]["is_general"] = False
        with self.assertRaises(BroadcastPermissionDenied):
            prepare_slack_broadcasts(self.delivery, "@everyone")
        self.role.assert_called_once_with("ab" * 32)

    def test_general_identity_comes_from_slack_not_a_client_label(self):
        self.delivery["channel"]["slack_channel_name"] = "renamed-general"
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "@everyone"), "<!everyone>")
        self.client.return_value.conversations_info.assert_called_once_with(channel="CGENERAL", include_num_members=True)

    def test_live_admin_can_notify_any_channel(self):
        self.role.return_value = "admin"
        self.client.return_value.conversations_info.return_value["channel"]["is_general"] = False
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "@everyone @here"), "<!channel> <!here>")

    def test_linked_slack_guest_cannot_everyone(self):
        self.identity.return_value = {"slack_user_id": "UGUEST"}
        self.client.return_value.users_info.return_value = {
            "ok": True, "user": {"id": "UGUEST", "is_restricted": True},
        }
        with self.assertRaises(BroadcastPermissionDenied):
            prepare_slack_broadcasts(self.delivery, "@everyone")

    def test_wrong_channel_response_fails_closed(self):
        self.client.return_value.conversations_info.return_value["channel"]["id"] = "COTHER"
        with self.assertRaises(RuntimeError):
            prepare_slack_broadcasts(self.delivery, "@channel")

    def test_reply_and_edit_markup_never_broadcasts_or_queries_permissions(self):
        self.delivery["source_parent_message_id"] = "thread-id"
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "<!channel>"), "@channel")
        self.delivery["source_parent_message_id"] = ""
        self.delivery["delivery_type"] = "edit"
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "<!everyone>"), "@everyone")
        self.role.assert_not_called()
        self.client.assert_not_called()

    def test_literals_and_non_chat_origins_do_not_query_account_authority(self):
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "`@channel`"), "`@channel`")
        self.delivery["source_platform"] = "slack"
        self.assertEqual(prepare_slack_broadcasts(self.delivery, "@channel"), "@channel")
        self.role.assert_not_called()
        self.client.assert_not_called()

    def test_member_capability_advertises_slack_default(self):
        from community_chat.permissions import role_capabilities
        capabilities = role_capabilities("member")
        self.assertTrue(capabilities["can_mention_channel"])
        self.assertFalse(capabilities["can_manage_channels"])


class BroadcastWireLiteralTests(unittest.TestCase):
    def test_raw_slack_tokens_cannot_bypass_policy_in_links_or_escapes(self):
        delivery = {"source_platform": "buzz", "delivery_type": "create"}
        for name in ("channel", "here", "everyone"):
            for text in (f"[<!{name}>](https://example.test)", f"\\<!{name}>", f"`<!{name}>`"):
                with self.subTest(text=text):
                    wire = prepare_slack_broadcasts(delivery, text)
                    self.assertNotIn(f"<!{name}>", wire)
                    self.assertIn(f"&lt;!{name}&gt;", wire)

    def test_author_and_attachment_labels_cannot_inject_mentions(self):
        from integrations.services.community_bridge.formatting import build_mirrored_text
        text = build_mirrored_text(
            destination_platform="slack", source_platform="buzz", author_display_name="<!everyone>",
            body="hello", attachments=[{"url": "https://example.test/file", "title": "<!channel>"}],
        )
        self.assertNotIn("<!everyone>", text)
        self.assertNotIn("<!channel>", text)
        self.assertIn("&lt;!everyone&gt;", text)

    def test_labelled_slack_tokens_cannot_skip_the_permission_gate(self):
        delivery = {"source_platform": "buzz", "delivery_type": "create"}
        for name in ("channel", "here", "everyone"):
            for text in (f"<!{name}|{name}>", f"[<!{name}|{name}>](https://example.test)"):
                with self.subTest(text=text):
                    wire = prepare_slack_broadcasts(delivery, text)
                    self.assertNotIn(f"<!{name}", wire)
                    self.assertIn(f"&lt;!{name}|{name}&gt;", wire)

    def test_reference_and_nested_links_remain_literal(self):
        delivery = {"source_platform": "buzz", "delivery_type": "create"}
        for text in (
            "[@here][label]\n\n[label]: https://example.test",
            "[@here][]\n\n[@here]: https://example.test",
            "[@here]\n\n[@here]: https://example.test",
            "[nested [@here]](https://example.test)",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_broadcast_mentions(text))
                self.assertEqual(prepare_slack_broadcasts(delivery, text), text)

    def test_plain_mentions_inside_raw_slack_labels_cannot_split_escaping(self):
        delivery = {"source_platform": "buzz", "delivery_type": "create"}
        for text in ("<!everyone|@channel>", "<!everyone|<!channel>>", "<!here|@everyone>"):
            with self.subTest(text=text):
                wire = prepare_slack_broadcasts(delivery, text)
                self.assertNotIn("<!everyone", wire)
                self.assertNotIn("<!channel", wire)
                self.assertNotIn("<!here", wire)


class PrivateBroadcastDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = SimpleNamespace(
            encrypted_text="@channel @here", operation="create", metadata={},
            source_author_id="ab" * 32,
            conversation=SimpleNamespace(slack_conversation_id="CPRIVATE"),
        )
        self.role = self.enterContext(patch("community_chat.permissions.device_chat_role", return_value="member"))
        self.kind = self.enterContext(patch("integrations.services.slack_chat_catalog.conversation_kind", return_value="private_channel"))
        self.client = Mock()
        self.client.conversations_info.return_value = {
            "ok": True, "channel": {"id": "CPRIVATE", "is_general": False, "num_members": 20},
        }

    def test_owner_user_token_encodes_callouts_in_private_channels(self):
        self.assertEqual(prepare_private_slack_broadcasts(self.delivery, self.client), "<!channel> <!here>")
        self.client.conversations_info.assert_called_once_with(channel="CPRIVATE", include_num_members=True)
        self.role.assert_called_once_with("ab" * 32)

    def test_private_channel_everyone_requires_live_admin(self):
        self.delivery.encrypted_text = "@everyone"
        with self.assertRaises(BroadcastPermissionDenied):
            prepare_private_slack_broadcasts(self.delivery, self.client)
        self.role.return_value = "admin"
        self.assertEqual(prepare_private_slack_broadcasts(self.delivery, self.client), "<!channel>")

    def test_dms_replies_and_edits_stay_silent(self):
        self.delivery.encrypted_text = "<!channel> @here"
        for kind in ("im", "mpim"):
            self.kind.return_value = kind
            self.assertEqual(prepare_private_slack_broadcasts(self.delivery, self.client), "@channel @here")
        self.kind.return_value = "private_channel"
        for metadata in ({"source_parent_message_id": "root"}, {"original_source_parent_message_id": "missing-root"}):
            self.delivery.metadata = metadata
            self.assertEqual(prepare_private_slack_broadcasts(self.delivery, self.client), "@channel @here")
        self.delivery.metadata = {}
        self.delivery.operation = "edit"
        self.assertEqual(prepare_private_slack_broadcasts(self.delivery, self.client), "@channel @here")
        self.role.assert_not_called()
        self.client.conversations_info.assert_not_called()

    def test_wrong_private_channel_cannot_authorize_broadcast(self):
        self.client.conversations_info.return_value["channel"]["id"] = "COTHER"
        with self.assertRaises(RuntimeError):
            prepare_private_slack_broadcasts(self.delivery, self.client)
