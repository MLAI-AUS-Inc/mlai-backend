"""Database-free regression coverage for Slack's per-account unread contract."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from integrations.services import slack_chat_read_state as reads
from integrations.services.slack_dm_mirror import SlackDmMirrorAuthorizationError


class SlackReadStateTests(SimpleTestCase):
    def snapshot(self, details, messages=(), kind="im"):
        return reads.read_state_snapshot(
            details, kind=kind, messages=messages, owner_id="UOWNER"
        )

    def test_dm_count_includes_unimported_messages_and_zero_is_authoritative(self):
        for count in (0, 3):
            result = self.snapshot(
                {
                    "last_read": "100.000001",
                    "latest": {"ts": "101.000001"},
                    "unread_count_display": count,
                }
            )
            self.assertEqual(result["is_unread"], count > 0)
            self.assertEqual(result["unread_count"], count)
            self.assertEqual(result["count_source"], "slack")

    def test_absent_or_invalid_cursor_is_unknown_not_read(self):
        for value in (None, "bad", "NaN", "Infinity", "-1"):
            self.assertIsNone(
                self.snapshot({"last_read": value, "unread_count_display": 0})
            )

    def test_group_read_cursor_uses_source_microseconds_and_ignores_own_and_threads(
        self,
    ):
        result = self.snapshot(
            {"last_read": "100.000001"},
            [
                {"ts": "100.000001", "user": "UALICE"},
                {"ts": "100.000002", "user": "UALICE"},
                {"ts": "101.000001", "user": "UOWNER"},
                {"ts": "102.000001", "user": "UALICE", "thread_ts": "99.000001"},
            ],
            kind="mpim",
        )
        self.assertTrue(result["is_unread"])
        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["latest_ts"], "101.000001")

    def test_channel_badges_count_explicit_mentions_not_every_unread_message(self):
        result = self.snapshot(
            {"last_read": "100.000001"},
            [
                {"ts": "101.000001", "user": "UALICE", "text": "A regular message"},
                {"ts": "102.000001", "user": "UALICE", "text": "Hello <@UOWNER>"},
                {"ts": "103.000001", "user": "UALICE", "text": "Hi <!channel>"},
                {"ts": "104.000001", "user": "UOWNER", "text": "<@UOWNER>"},
            ],
            kind="private_channel",
        )
        self.assertTrue(result["is_unread"])
        self.assertEqual(result["unread_count"], 2)
        self.assertEqual(result["count_source"], "imported_messages")

    def test_channel_regular_unread_has_dot_and_read_cursor_clears_it(self):
        messages = [{"ts": "101.000001", "user": "UALICE", "text": "hello"}]
        unread = self.snapshot(
            {"last_read": "100.000001"}, messages, kind="public_channel"
        )
        read = self.snapshot(
            {"last_read": "101.000001"}, messages, kind="public_channel"
        )
        self.assertTrue(unread["is_unread"])
        self.assertEqual(unread["unread_count"], 0)
        self.assertFalse(read["is_unread"])

    def authority(self, **changes):
        fields = dict(
            user_id=1,
            grant_id=2,
            connection_id=3,
            consent_generation=4,
            oauth_generation=5,
            workspace_id="TWORK",
            slack_user_id="UOWNER",
        )
        fields.update(changes)
        return SimpleNamespace(**fields)

    def test_cache_isolated_by_owner_workspace_and_consent(self):
        target = reads.ReadTarget("channel", "D123", "im")
        original = reads._cache_key(self.authority(), target)
        for change in (
            {"user_id": 9},
            {"workspace_id": "TOTHER"},
            {"consent_generation": 9},
            {"oauth_generation": 9},
        ):
            self.assertNotEqual(
                original, reads._cache_key(self.authority(**change), target)
            )

    def mark(self, scopes, last_read="100.000001", source_ts="101.000001", kind="im"):
        grant = SimpleNamespace(connection=SimpleNamespace(scopes=scopes))
        target = reads.ReadTarget("mirror", "D123", kind)
        with patch.object(
            reads, "active_grant_for_user", return_value=grant
        ), patch.object(reads, "_assert_grant_connection_authorized"), patch.object(
            reads, "_targets", return_value=[target]
        ), patch.object(
            reads, "_capture_slack_grant_api_authority", return_value=self.authority()
        ), patch.object(
            reads.transaction, "atomic", side_effect=nullcontext
        ), patch.object(
            reads, "_lock_slack_grant_api_authority"
        ), patch.object(
            reads.cache, "delete"
        ), patch.object(
            reads,
            "_call_slack_with_grant_authority",
            return_value={"channel": {"id": "D123", "last_read": last_read}},
        ) as call:
            result = reads.mark_read(
                object(), public_key="key", channel_id="mirror", source_ts=source_ts
            )
            return result, call.call_args_list

    def test_mark_read_never_moves_slack_backwards(self):
        result, calls = self.mark(["im:write"], last_read="102.000001")
        self.assertTrue(result["synced"])
        self.assertEqual([c.args[1] for c in calls], ["conversations_info"])
        result, calls = self.mark(["im:write"])
        self.assertTrue(result["synced"])
        self.assertEqual(calls[-1].args[1], "conversations_mark")
        self.assertEqual(calls[-1].kwargs["ts"], "101.000001")

    def test_missing_write_scope_makes_no_slack_call(self):
        result, calls = self.mark(["groups:read"], kind="private_channel")
        self.assertFalse(result["synced"])
        self.assertTrue(result["needs_reauthorization"])
        self.assertEqual(calls, [])

    def test_revoked_grant_cannot_read_cached_metadata(self):
        grant = SimpleNamespace()
        target = reads.ReadTarget("mirror", "D123", "im")
        with patch.object(
            reads, "active_grant_for_user", return_value=grant
        ), patch.object(reads, "_assert_grant_connection_authorized"), patch.object(
            reads, "_targets", return_value=[target]
        ), patch.object(
            reads, "_capture_slack_grant_api_authority", return_value=self.authority()
        ), patch.object(
            reads.transaction, "atomic", side_effect=nullcontext
        ), patch.object(
            reads,
            "_lock_slack_grant_api_authority",
            side_effect=SlackDmMirrorAuthorizationError("revoked"),
        ), patch.object(
            reads.cache, "get"
        ) as cached:
            with self.assertRaises(SlackDmMirrorAuthorizationError):
                reads.read_state_page(object(), public_key="key")
            cached.assert_not_called()


class PrivateUnreadHistoryTests(SimpleTestCase):
    def test_erased_queue_bodies_cannot_hide_source_mentions(self):
        target = reads.ReadTarget(
            "mirror",
            "C123",
            "private_channel",
            conversation=SimpleNamespace(grant=object()),
        )
        with patch.object(reads, "_grant_history_days", return_value=0), patch.object(
            reads,
            "_call_slack_with_grant_authority",
            return_value={
                "messages": [
                    {"ts": "101.000001", "user": "UALICE", "text": "Hello <@UOWNER>"}
                ],
                "has_more": False,
            },
        ) as call:
            messages, source, partial = reads._unread_messages(
                object(), target, "100.000001"
            )
        snapshot = reads.read_state_snapshot(
            {"last_read": "100.000001"},
            kind="private_channel",
            messages=messages,
            owner_id="UOWNER",
        )
        self.assertEqual(snapshot["unread_count"], 1)
        self.assertEqual(source, "slack_history")
        self.assertFalse(partial)
        self.assertEqual(call.call_args.kwargs["oldest"], "100.000001")
        self.assertEqual(call.call_args.kwargs["required_scopes"], {"groups:history"})

    def test_private_read_probe_respects_existing_history_consent(self):
        target = reads.ReadTarget(
            "mirror", "G123", "mpim", conversation=SimpleNamespace(grant=object())
        )
        with patch.object(reads, "_grant_history_days", return_value=7), patch.object(
            reads,
            "_call_slack_with_grant_authority",
            return_value={"messages": [], "has_more": False},
        ) as call:
            _, _, partial = reads._unread_messages(object(), target, "100.000001")
        self.assertTrue(partial)
        self.assertGreater(float(call.call_args.kwargs["oldest"]), 100)
        self.assertEqual(call.call_args.kwargs["required_scopes"], {"mpim:history"})


class ReadStatePageTests(SimpleTestCase):
    def page(self, requested=None):
        authority = SlackReadStateTests().authority()
        grant = SimpleNamespace(slack_user_id="UOWNER")
        targets = [reads.ReadTarget(f"mirror-{i}", f"D{i}", "im") for i in range(8)]

        def response(*args, **kwargs):
            return {
                "channel": {
                    "id": kwargs["channel"],
                    "last_read": "100.000001",
                    "latest": {"ts": "101.000001"},
                    "unread_count_display": 1,
                }
            }

        with patch.object(
            reads, "active_grant_for_user", return_value=grant
        ), patch.object(reads, "_assert_grant_connection_authorized"), patch.object(
            reads, "_targets", return_value=targets
        ), patch.object(
            reads, "_capture_slack_grant_api_authority", return_value=authority
        ), patch.object(
            reads.transaction, "atomic", side_effect=nullcontext
        ), patch.object(
            reads, "_lock_slack_grant_api_authority"
        ), patch.object(
            reads.cache, "get", return_value=None
        ), patch.object(
            reads.cache, "get_many", return_value={}
        ), patch.object(
            reads.cache, "set"
        ), patch.object(
            reads, "_call_slack_with_grant_authority", side_effect=response
        ) as call:
            result = reads.read_state_page(
                object(), public_key="key", channel_ids=requested
            )
            return result, call.call_args_list

    def test_background_work_is_bounded_and_returns_a_continuation(self):
        result, calls = self.page()
        self.assertEqual(len(result["channels"]), 4)
        self.assertEqual(result["next_cursor"], "4")
        self.assertEqual(len(calls), 4)

    def test_visible_priority_is_limited_to_authorized_channel_ids(self):
        result, calls = self.page(["mirror-7", "someone-elses-channel"])
        self.assertEqual(set(result["channels"]), {"mirror-7"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["channel"], "D7")
