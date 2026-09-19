"""Database-free regression tests for foreground Slack refresh requests."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from integrations.services import slack_chat_refresh as refresh
from integrations.services import slack_dm_mirror as mirror
from integrations.models import BridgeSyncState, SlackDmMirrorDelivery
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture


class SlackChannelRefreshTests(SimpleTestCase):
    def conversation(self):
        return SimpleNamespace(
            pk=12,
            mlai_channel_id=uuid4(),
            history_backfilled_at=timezone.now() - timedelta(hours=1),
            slack_conversation_id="DPRIVATE",
            grant=SimpleNamespace(
                history_days=30, connection=SimpleNamespace(provider_metadata={})
            ),
            deliveries=MagicMock(),
            last_error="",
            participant_hash="a" * 64,
        )

    def test_invalid_channel_is_rejected_before_database_access(self):
        for channel_id in (None, "", "not-a-channel"):
            with self.subTest(channel_id=channel_id), self.assertRaises(
                ValidationError
            ):
                refresh._authorized_conversation(object(), channel_id, "device")

    @patch.object(refresh.SlackDmMirrorGrant, "objects")
    def test_other_accounts_channel_is_unavailable(self, grants):
        user = object()
        channel_id = uuid4()
        grants.filter.return_value.filter.return_value.first.return_value = None
        with self.assertRaises(NotFound):
            refresh._authorized_conversation(user, channel_id, "device")
        grants.filter.assert_called_once_with(
            user=user, status="active", revoked_at__isnull=True
        )

    @patch.object(refresh.SlackDmMirrorConversation, "objects")
    @patch.object(refresh.SlackDmMirrorGrant, "objects")
    def test_unprovisioned_device_is_rejected(self, grants, conversations):
        grants.filter.return_value.filter.return_value.first.return_value = object()
        conversations.filter.return_value.first.return_value = SimpleNamespace(
            participant_buzz_pubkeys=["owner"]
        )
        with self.assertRaises(NotFound):
            refresh._authorized_conversation(object(), uuid4(), "other")

    def request(self, conversation, marker=None):
        conversation.deliveries.filter.return_value.first.return_value = marker
        with patch.object(
            refresh, "_authorized_conversation", return_value=conversation
        ) as authorize, patch.object(
            refresh, "_refresh_status", return_value={"status": "syncing"}
        ), patch.object(
            mirror, "_ensure_history_state"
        ) as ensure, patch.object(
            mirror, "_mark_conversation_history_due"
        ) as mark:
            # Exercise the service body without opening a transaction or database.
            result = refresh.request_conversation_refresh.__wrapped__(
                "user", "channel", public_key="device"
            )
        authorize.assert_called_once_with("user", "channel", "device", lock=True)
        ensure.assert_called_once()
        self.assertEqual(
            ensure.call_args.kwargs["source_message_id"], refresh.FOREGROUND_STATE_ID
        )
        self.assertGreater(ensure.return_value.available_at, timezone.now())
        self.assertEqual(result, {"status": "syncing"})
        return mark

    def test_open_refreshes_completed_history_without_widening_window(self):
        conversation = self.conversation()
        mark = self.request(conversation)
        mark.assert_called_once_with(
            conversation,
            reason="Opened in MLAI Chat",
            reset_deliveries=False,
            reconcile_current_state=True,
        )
        self.assertEqual(conversation.grant.history_days, 30)

    def test_reopening_pending_history_preserves_scan_progress(self):
        conversation = self.conversation()
        conversation.history_backfilled_at = None
        self.request(conversation).assert_not_called()

    def test_rapid_reopen_coalesces_even_after_completion(self):
        self.request(
            self.conversation(), SimpleNamespace(updated_at=timezone.now())
        ).assert_not_called()

    def test_priority_uses_only_unexpired_completed_control_rows(self):
        queryset = refresh.prioritize_open_conversations(
            refresh.SlackDmMirrorConversation.objects.all()
        )
        sql, params = queryset.query.sql_with_params()
        self.assertIn("EXISTS", sql)
        self.assertIn(refresh.FOREGROUND_STATE_ID, params)
        self.assertIn("completed", params)

    def test_completion_preserves_foreground_priority_for_delivery(self):
        with patch.object(mirror.SlackDmMirrorDelivery, "objects") as rows:
            mirror._clear_history_scan_states([12], preserve_foreground=True)
            rows.filter.return_value.exclude.assert_called_once_with(
                source_message_id=refresh.FOREGROUND_STATE_ID
            )
            rows.filter.return_value.exclude.return_value.delete.assert_called_once()

    @patch.object(BridgeSyncState, "objects")
    def test_complete_waits_for_message_delivery_and_reports_failure(self, states):
        states.filter.return_value.first.return_value = None
        for backfilled, pending, failed, expected in [
            (None, False, False, "syncing"),
            (timezone.now(), True, False, "syncing"),
            (timezone.now(), False, False, "complete"),
            (timezone.now(), False, True, "error"),
        ]:
            with self.subTest(expected=expected, pending=pending):
                conversation = self.conversation()
                conversation.history_backfilled_at = backfilled
                rows = MagicMock()
                rows.exclude.return_value = rows

                rows.filter.return_value = rows
                rows.aggregate.return_value = {
                    "imported_messages": 2,
                    "queued_messages": int(pending),
                    "failed_messages": int(failed),
                }
                conversation.deliveries = rows
                self.assertEqual(
                    refresh._refresh_status(conversation)["status"], expected
                )


class SlackCurrentRoomRefreshTests(SlackDmIoAuthorityFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()

    def row(self, source, *, audience=None, status="dead", error="Delivery failed"):
        return SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack",
            source_message_id=source, source_author_id="UOTHER", operation="reaction_add",
            metadata={"participant_hash": audience or self.conversation.participant_hash},
            status=status, last_error=error, available_at=timezone.now(),
        )

    def test_old_room_reactions_do_not_poison_a_successful_current_import(self):
        old = self.row("old-reaction", audience="b" * 64)
        retired = self.row("retired-reaction", error="Private conversation participants changed")
        result = refresh._refresh_status(self.conversation)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["failed_messages"], 0)
        self.assertEqual(SlackDmMirrorDelivery.objects.filter(pk__in=[old.pk, retired.pk], status="dead").count(), 2)

    def test_current_room_failure_is_not_hidden(self):
        self.row("current-failure")
        result = refresh._refresh_status(self.conversation)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failed_messages"], 1)

    def test_current_room_pending_work_keeps_syncing(self):
        self.row("pending-reaction", status="pending", error="Waiting for parent")
        result = refresh._refresh_status(self.conversation)
        self.assertEqual(result["status"], "syncing")
        self.assertEqual(result["queued_messages"], 1)
