"""Background page/restart/permission tests using synthetic provider responses."""
import json
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import BridgeSyncInbox, BridgeSyncJob, CommunityBridgeDelivery, CommunityBridgeReceipt, CommunityBridgeChannel
from integrations.services.message_sync.history import ensure_state, public_page
from integrations.services.message_sync.inbox import enqueue_slack_callback, process_inbox_once
from integrations.services.message_sync.private_history import private_page
from integrations.services.message_sync.scheduler import claim_job, schedule_job


class PublicHistoryTests(TransactionTestCase):
    def state(self, channel):
        return ensure_state(CommunityBridgeChannel.objects.create(
            slack_workspace_id="T1", slack_channel_id=channel, destination_platform="buzz",
            destination_workspace_id="test.invalid", destination_channel_id=f"fixture-{channel}",
        ))

    def test_checkpoint_resumes_next_page_and_keeps_old_thread_job(self):
        state = self.state("C1")
        job = schedule_job(state, "archive")
        client = MagicMock()
        client.conversations_history.side_effect = [
            {"ok": True, "messages": [{"ts": "1700000001.000001", "user": "U1", "text": "newer", "reply_count": 1}],
             "response_metadata": {"next_cursor": "page2"}},
            {"ok": True, "messages": [{"ts": "1700000000.000001", "user": "U1", "text": "older"}]},
        ]
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["archive"]), state)
            self.assertEqual(BridgeSyncJob.objects.get(pk=job.pk).checkpoint["cursor"], "page2")
            state.refresh_from_db()
            self.assertEqual(state.verified_ranges["archive"]["classification"], "incomplete")
            public_page(claim_job(kinds=["archive"]), state)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["classification"], "accessible_range")
        self.assertEqual(client.conversations_history.call_args.kwargs["cursor"], "page2")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 2)
        self.assertTrue(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key="1700000001.000001").exists())
        self.assertIsNotNone(BridgeSyncJob.objects.get(pk=job.pk).completed_at)

    def test_source_limited_empty_scan_does_not_claim_an_empty_conversation(self):
        state = self.state("C1")
        client = MagicMock()
        client.conversations_history.return_value = {"ok": True, "messages": [], "is_limited": True}
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["head"]), state)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["head"]["classification"], "source_limited")
        self.assertEqual(state.verified_ranges["head"]["absence"], "unknown")
        self.assertFalse(CommunityBridgeDelivery.objects.filter(delivery_type="delete").exists())

    def test_expired_cursor_restarts_without_losing_imported_data(self):
        from integrations.services.message_sync.scheduler import fail_job
        state = self.state("C1")
        job = schedule_job(state, "head")
        job.checkpoint = {"cursor": "expired"}
        job.save()
        lease = claim_job(kinds=["head"])
        fail_job(lease, error_code="invalid_cursor")
        job.refresh_from_db()
        self.assertEqual(job.checkpoint, {})
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["head"]["absence"], "unknown")

    def test_page_effects_and_cursor_roll_back_together(self):
        state = self.state("C1")
        job = schedule_job(state, "archive")
        lease = claim_job()
        client = MagicMock()
        client.conversations_history.return_value = {"ok": True, "messages": [
            {"ts": "1700000000.000001", "user": "U1", "text": "one"},
            {"ts": "1700000001.000001", "user": "U1", "text": "two"},
        ]}
        from integrations.services.community_bridge.store import ingest_slack_event
        def fail_second(payload):
            if payload["event"]["text"] == "two":
                raise RuntimeError("interrupted")
            return ingest_slack_event(payload)
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client), patch(
            'integrations.services.message_sync.history.ingest_slack_event', side_effect=fail_second,
        ):
            with self.assertRaises(RuntimeError):
                public_page(lease, state)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)
        self.assertEqual(BridgeSyncJob.objects.get(pk=job.pk).checkpoint, {})


class PrivateHeadTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def test_recent_head_imports_while_archive_is_incomplete(self):
        state = ensure_state(self.conversation)
        schedule_job(state, "head")
        source_ts = f"{int(timezone.now().timestamp()) - 60}.000001"
        response = {"ok": True, "messages": [{"ts": source_ts, "user": "UOTHER", "text": "latest", "reply_count": 1}]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response):
            private_page(claim_job(), state)
        self.assertTrue(self.conversation.deliveries.filter(source_message_id=source_ts).exists())
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.history_backfilled_at)
        self.assertTrue(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key=source_ts).exists())
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)

    def test_revoke_during_head_read_prevents_every_response_write(self):
        from integrations.services import slack_dm_mirror as dm
        state = ensure_state(self.conversation)
        schedule_job(state, "head")
        source_ts = f"{int(timezone.now().timestamp()) - 60}.000001"
        def revoke(*args, **kwargs):
            self.grant.status = "revoked"
            self.grant.revoked_at = timezone.now()
            self.grant.save()
            return {"ok": True, "messages": [{"ts": source_ts, "user": "UOTHER", "text": "revoked"}]}
        with patch.object(dm, '_call_slack_with_grant_authority', side_effect=revoke):
            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                private_page(claim_job(), state)
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=source_ts).exists())


class RecipientPaginationTests(TransactionTestCase):
    @override_settings(MESSAGE_SYNC_SLACK_APP_ID="A1", MESSAGE_SYNC_SLACK_APP_TOKEN="synthetic-app-token")
    def test_paginated_recipients_resume_after_worker_restart_and_never_cross_workspace(self):
        payload = {"api_app_id": "A1", "team_id": "T1", "event_id": "E1", "event_context": "context",
                   "event": {"type": "message", "channel": "D1", "text": "fixture"},
                   "authorizations": [{"team_id": "T1", "user_id": "U1"}]}
        enqueue_slack_callback(payload)
        client = MagicMock()
        client.apps_event_authorizations_list.side_effect = [
            {"ok": True, "authorizations": [{"team_id": "T1", "user_id": "U1"}], "response_metadata": {"next_cursor": "next"}},
            {"ok": True, "authorizations": [{"team_id": "T1", "user_id": "U2"}, {"team_id": "T2", "user_id": "SECRET"}]},
        ]
        with patch('integrations.services.message_sync.authorizations.WebClient', return_value=client), patch(
            'integrations.services.slack_dm_mirror.ingest_slack_dm_event', return_value={"status": "enqueued"},
        ) as ingest:
            self.assertEqual(process_inbox_once(), 0)
            ingest.assert_not_called()
            row = BridgeSyncInbox.objects.get()
            self.assertEqual(json.loads(row.encrypted_payload)["_sync_authorizations_cursor"], "next")
            BridgeSyncInbox.objects.update(available_at=timezone.now())
            self.assertEqual(process_inbox_once(), 1)
        self.assertEqual(client.apps_event_authorizations_list.call_args.kwargs["cursor"], "next")
        self.assertEqual(ingest.call_args.args[0]["authorizations"], [
            {"team_id": "T1", "user_id": "U1"}, {"team_id": "T1", "user_id": "U2"},
        ])


class PrivateDeliveryFairnessTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_quota_wait_refunds_turn_without_consuming_an_attempt(self):
        from integrations.models import SlackDmMirrorDelivery, BridgeSyncState
        from integrations.services import slack_dm_mirror as dm
        from integrations.services.message_sync.scheduler import BudgetDeferred
        row = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id="1700000000.000001",
            source_author_id="UOTHER", operation="create", encrypted_text="fixture", available_at=timezone.now(),
        )
        claimed = dm._claim_ready_private_delivery_batch(limit=1)[0]
        dm._record_private_delivery_failure(claimed, BudgetDeferred(60))
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("pending", 0))
        self.assertIsNone(BridgeSyncState.objects.get(private_conversation=self.conversation).last_served_at)

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_unopened_conversation_gets_a_turn_before_more_busy_conversation_work(self):
        import uuid
        from integrations.models import SlackDmMirrorConversation, SlackDmMirrorDelivery
        from integrations.services import slack_dm_mirror as dm
        other = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id=self.grant.slack_workspace_id, slack_conversation_id="DQUIET",
            participant_slack_ids=self.conversation.participant_slack_ids, participant_hash="b" * 64,
            mlai_channel_id=uuid.uuid4(), status="live",
        )
        now = timezone.now()
        for index, conversation in enumerate([self.conversation, self.conversation, other]):
            SlackDmMirrorDelivery.objects.create(
                conversation=conversation, source_platform="slack", source_message_id=f"{int(now.timestamp())}.{index:06d}",
                source_author_id="UOTHER", operation="create", encrypted_text="fixture", available_at=now,
            )
        first = dm._claim_ready_private_delivery_batch(limit=1)
        self.assertEqual(first[0].conversation_id, self.conversation.pk)
        SlackDmMirrorDelivery.objects.filter(pk=first[0].pk).update(status="completed")
        second = dm._claim_ready_private_delivery_batch(limit=1)
        self.assertEqual(second[0].conversation_id, other.pk)
