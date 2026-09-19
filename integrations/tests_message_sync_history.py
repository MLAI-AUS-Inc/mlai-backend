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
    def setUp(self):
        self.now = int(timezone.now().timestamp())
        clock = patch("integrations.services.message_sync.history.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

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
            {"ok": True, "messages": [{"ts": f"{self.now - 60}.000001", "user": "U1", "text": "newer", "reply_count": 1}],
             "response_metadata": {"next_cursor": "page2"}},
            {"ok": True, "messages": [{"ts": f"{self.now - 61}.000001", "user": "U1", "text": "older"}]},
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
        self.assertTrue(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key=f"{self.now - 60}.000001").exists())
        self.assertIsNotNone(BridgeSyncJob.objects.get(pk=job.pk).completed_at)

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="internal")
    def test_archive_bounds_thirty_days_and_reads_a_full_internal_page(self):
        state = self.state("C1")
        client = MagicMock()

        def slack_history(**kwargs):
            self.assertEqual(kwargs["oldest"], f"{self.now - 30 * 86400}.000000")
            self.assertEqual(kwargs["limit"], 200)
            return {"ok": True, "messages": []}

        client.conversations_history.side_effect = slack_history
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["archive"]), state)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["oldest"], f"{self.now - 30 * 86400}.000000")

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="restricted")
    def test_restricted_public_history_keeps_fifteen_message_limit(self):
        state = self.state("C1")
        client = MagicMock()
        client.conversations_history.return_value = {"ok": True, "messages": []}
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["head"]), state)
        self.assertEqual(client.conversations_history.call_args.kwargs["limit"], 15)
        self.assertGreater(int(client.conversations_history.call_args.kwargs["oldest"].split(".")[0]), 0)

    def test_legacy_public_archive_cursor_restarts_at_thirty_days(self):
        state = self.state("C1")
        job = schedule_job(state, "archive")
        job.checkpoint = {"oldest": "0.000000", "upper_bound": f"{self.now}.999999",
                          "cursor": "years-old-page", "latest": "1700000000.000001"}
        job.save()
        client = MagicMock()
        client.conversations_history.return_value = {"ok": True, "messages": []}
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["archive"]), state)
        request = client.conversations_history.call_args.kwargs
        self.assertNotIn("cursor", request)
        self.assertEqual(request["oldest"], f"{self.now - 30 * 86400}.000000")
        self.assertEqual(request["latest"], f"{self.now}.999999")

    def test_public_archive_stops_at_floor_and_keeps_recent_reply_locator(self):
        state = self.state("C1")
        old = f"{self.now - 60 * 86400}.000001"
        recent = f"{self.now - 60}.000001"
        client = MagicMock()
        client.conversations_history.return_value = {
            "ok": True, "has_more": True, "response_metadata": {"next_cursor": "ancient-page"},
            "messages": [{"ts": recent, "user": "U1", "text": "recent"},
                         {"ts": old, "user": "U1", "text": "excluded root", "latest_reply": recent}],
        }
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["archive"]), state)
        self.assertFalse(CommunityBridgeDelivery.objects.filter(source_message_id=old).exists())
        self.assertTrue(CommunityBridgeDelivery.objects.filter(source_message_id=recent).exists())
        self.assertTrue(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key=old).exists())
        self.assertEqual(state.jobs.get(kind="archive").checkpoint, {})
        self.assertIsNotNone(state.jobs.get(kind="archive").completed_at)

    def test_public_old_root_does_not_stop_ascending_recent_reply_pages(self):
        state = self.state("C1")
        root = f"{self.now - 60 * 86400}.000001"
        reply = f"{self.now - 60}.000001"
        schedule_job(state, "thread", source_object_key=root)
        client = MagicMock()
        client.conversations_replies.side_effect = [
            {"ok": True, "messages": [{"ts": root, "user": "U1", "text": "outside window"}],
             "has_more": True, "response_metadata": {"next_cursor": "recent-replies"}},
            {"ok": True, "messages": [{"ts": reply, "user": "U1", "text": "recent reply"}]},
        ]
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["thread"]), state)
            self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
            public_page(claim_job(kinds=["thread"]), state)
        self.assertEqual(client.conversations_replies.call_args.kwargs["cursor"], "recent-replies")
        row = CommunityBridgeDelivery.objects.get(source_message_id=reply)
        self.assertEqual(row.source_parent_message_id, "")
        self.assertEqual(row.payload["text"], "recent reply")
        self.assertFalse(CommunityBridgeDelivery.objects.filter(source_message_id=root).exists())

    def test_public_head_preserves_parent_inside_thirty_days(self):
        state = self.state("C1")
        root = f"{self.now - 2 * 86400}.000001"
        reply = f"{self.now - 60}.000001"
        client = MagicMock()
        client.conversations_history.return_value = {"ok": True, "messages": [
            {"ts": reply, "thread_ts": root, "user": "U1", "text": "recent reply"},
        ]}
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["head"]), state)
        self.assertEqual(CommunityBridgeDelivery.objects.get(source_message_id=reply).source_parent_message_id, root)

    def test_quiet_old_public_thread_waits_a_day_and_records_empty_window(self):
        state = self.state("C1")
        root = f"{self.now - 60 * 86400}.000001"
        job = schedule_job(state, "thread", source_object_key=root)
        client = MagicMock()
        client.conversations_replies.return_value = {"ok": True, "messages": [
            {"ts": root, "user": "U1", "text": "outside window"},
        ]}
        with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
            public_page(claim_job(kinds=["thread"]), state)
        state.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(state.verified_ranges["thread"]["classification"], "empty_accessible_range")
        self.assertGreater(job.due_at.timestamp(), self.now + 23 * 3600)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

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
            {"ts": f"{self.now - 61}.000001", "user": "U1", "text": "one"},
            {"ts": f"{self.now - 60}.000001", "user": "U1", "text": "two"},
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

    def test_live_create_and_history_share_source_identity_in_either_order(self):
        from integrations.services.community_bridge.store import ingest_slack_event
        for history_first in [False, True]:
            with self.subTest(history_first=history_first):
                channel_id = "CHISTORY" if history_first else "CLIVE"
                state = self.state(channel_id)
                message = {"ts": f"{self.now - 61}.000001", "user": "U1", "text": "fixture"}
                payload = {"team_id": "T1", "event_id": f"live:{channel_id}", "event": {
                    **message, "type": "message", "channel": channel_id, "channel_type": "channel",
                }}
                client = MagicMock()
                client.conversations_history.return_value = {"ok": True, "messages": [message]}
                if not history_first:
                    ingest_slack_event(payload)
                with patch('integrations.services.message_sync.history.SlackBridgeClient.get_client', return_value=client):
                    public_page(claim_job(kinds=["archive"]), state)
                if history_first:
                    ingest_slack_event(payload)
                self.assertEqual(CommunityBridgeDelivery.objects.filter(
                    channel=state.public_channel, delivery_type="create").count(), 1)



class PrivateHeadTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def test_group_head_keeps_historical_bot_attribution(self):
        from integrations.services.slack_chat_catalog import ALL_HISTORY_CONSENT
        self.grant.consent_version = ALL_HISTORY_CONSENT
        self.grant.save()
        self.conversation.slack_conversation_id = "GIOAUTH"
        self.conversation.save()
        state = ensure_state(self.conversation)
        schedule_job(state, "head")
        source_ts = f"{int(timezone.now().timestamp()) - 60}.000001"
        response = {"ok": True, "messages": [
            {"ts": source_ts, "user": "USLACKBOT", "text": "system context"},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response):
            private_page(claim_job(kinds=["head"]), state)
        row = self.conversation.deliveries.get(source_message_id=source_ts)
        self.assertEqual(row.source_author_id, "USLACKBOT")
        self.assertEqual(row.metadata["history_author"]["display_name"], "USLACKBOT")

    def test_head_skips_authors_that_archive_cannot_represent(self):
        state = ensure_state(self.conversation)
        schedule_job(state, "head")
        now = int(timezone.now().timestamp())
        messages = [
            {"ts": f"{now - 60}.000001", "user": "USLACKBOT", "text": "system parent"},
            {"ts": f"{now - 59}.000001", "user": "UNOTMEMBER", "text": "unrepresented"},
            {"ts": f"{now - 58}.000001", "user": "UOTHER", "text": "authorized reply"},
        ]
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority',
                   return_value={"ok": True, "messages": messages}):
            private_page(claim_job(kinds=["head"]), state)
        rows = self.conversation.deliveries.filter(source_message_id__in=[m["ts"] for m in messages])
        self.assertEqual(list(rows.values_list("source_author_id", flat=True)), ["UOTHER"])

    def test_quiet_old_private_thread_waits_a_day_and_records_empty_window(self):
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        root = f"{now - 60 * 86400}.000001"
        job = schedule_job(state, "thread", source_object_key=root)
        response = {"ok": True, "messages": [
            {"ts": root, "user": "UOTHER", "text": "outside consent"},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response):
            private_page(claim_job(kinds=["thread"]), state)
        state.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(state.verified_ranges["thread"]["classification"], "empty_accessible_range")
        self.assertGreater(job.due_at.timestamp(), now + 23 * 3600)
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=root).exists())

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_initial_archive_and_standalone_thread_do_not_fetch_same_replies(self):
        from integrations.services import slack_dm_mirror as dm
        state = ensure_state(self.conversation)
        parent = f"{int(timezone.now().timestamp()) - 60}.000001"
        authority = dm._capture_slack_grant_api_authority(self.grant)
        scopes = dm._history_required_scopes(self.conversation.slack_conversation_id)
        scan, *_ = dm._prepare_history_scan_page(self.conversation.pk, self.grant.pk, authority, scopes)
        # A recent-head observation can create this job before the archive sees
        # the same parent. The archive's legacy cursor then owns initial replies.
        job = schedule_job(state, "thread", source_object_key=parent)
        dm._ensure_thread_state(self.conversation, parent, scan_epoch=scan.epoch)
        with patch.object(dm, '_call_slack_with_grant_authority') as api:
            private_page(claim_job(kinds=["thread"]), state)
        api.assert_not_called()
        job.refresh_from_db()
        self.assertGreater(job.due_at, timezone.now())
        self.assertIsNotNone(dm._next_incomplete_thread_state(self.conversation, scan_epoch=scan.epoch))

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_new_archive_thread_schedules_later_repair(self):
        from integrations.services import slack_dm_mirror as dm
        parent = f"{int(timezone.now().timestamp()) - 60}.000001"
        dm._ensure_thread_state(self.conversation, parent, scan_epoch="bootstrap")
        job = BridgeSyncJob.objects.get(state__private_conversation=self.conversation,
                                       kind="thread", source_object_key=parent)
        self.assertGreater(job.due_at, timezone.now())
        self.assertIsNone(claim_job(kinds=["thread"]))

    def test_durable_deep_reconciliation_runs_daily_without_changing_legacy_cadence(self):
        from datetime import timedelta
        from integrations.services import slack_dm_mirror as dm
        for enabled, age_hours, expected in [(True, 2, False), (True, 25, True), (False, 2, True)]:
            with self.subTest(enabled=enabled, age_hours=age_hours), override_settings(MESSAGE_SYNC_ENABLED=enabled):
                self.conversation.history_backfilled_at = timezone.now() - timedelta(hours=age_hours)
                authority = dm._capture_slack_grant_api_authority(self.grant)
                with patch.object(dm, '_conversation_participant_ids', return_value=['UOWNER', 'UOTHER']), \
                        patch.object(dm, '_store_conversation_membership_intent', return_value=(self.conversation, False)), \
                        patch.object(dm, '_preload_slack_profiles'), \
                        patch.object(dm, '_slack_profile', return_value={}), \
                        patch.object(dm, '_store_conversation_profiles', return_value=self.conversation), \
                        patch.object(dm, '_provision_owner_conversation') as provision:
                    dm._discover_conversation(self.grant, authority,
                        {'id': self.conversation.slack_conversation_id, 'is_im': True, 'user': 'UOTHER'},
                        profile_cache={}, force_backfill=False, reset_history=False,
                        activity_seconds=int(timezone.now().timestamp()))
                self.assertEqual(provision.call_args.kwargs['force_backfill'], expected)

    def test_durable_old_root_keeps_recent_replies_without_importing_parent(self):
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        parent = f"{now - 60 * 86400}.000001"
        reply = f"{now - 60}.000001"
        schedule_job(state, "thread", source_object_key=parent)
        response = {"ok": True, "messages": [
            {"ts": parent, "user": "UOTHER", "text": "outside consent"},
            {"ts": reply, "user": "UOTHER", "text": "eligible reply"},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertEqual(api.call_args.kwargs["ts"], parent)
        self.assertGreaterEqual(int(api.call_args.kwargs["oldest"].split(".")[0]), now - 30 * 86400)
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=parent).exists())
        row = self.conversation.deliveries.get(source_message_id=reply)
        self.assertEqual(row.metadata["thread_ts"], "")
        self.assertEqual(row.metadata["original_thread_ts"], parent)

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

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="internal")
    def test_recent_reply_on_outside_window_root_does_not_import_parent(self):
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        parent_ts = f"{now - 60 * 86400}.000001"
        source_ts = f"{now - 60}.000001"
        response = {"ok": True, "messages": [
            {"ts": parent_ts, "user": "UOTHER", "text": "outside consent"},
            {"ts": source_ts, "user": "UOTHER", "text": "recent reply", "thread_ts": parent_ts},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response) as api:
            private_page(claim_job(kinds=["head"]), state)
        row = self.conversation.deliveries.get(source_message_id=source_ts)
        self.assertEqual(row.metadata["thread_ts"], "")
        self.assertEqual(row.metadata["original_thread_ts"], parent_ts)
        self.assertTrue(row.metadata["thread_parent_outside_history_window"])
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=parent_ts).exists())
        self.assertFalse(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key=parent_ts).exists())
        self.assertEqual(api.call_args.kwargs["limit"], 200)

    def test_recent_reply_preserves_parent_inside_consent_but_outside_head(self):
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        parent_ts = f"{now - 2 * 86400}.000001"
        source_ts = f"{now - 60}.000001"
        response = {"ok": True, "messages": [
            {"ts": source_ts, "user": "UOTHER", "text": "recent reply", "thread_ts": parent_ts},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response):
            private_page(claim_job(kinds=["head"]), state)
        row = self.conversation.deliveries.get(source_message_id=source_ts)
        self.assertEqual(row.metadata["thread_ts"], parent_ts)
        self.assertNotIn("thread_parent_outside_history_window", row.metadata)
        self.assertTrue(BridgeSyncJob.objects.filter(state=state, kind="thread", source_object_key=parent_ts).exists())

    def test_resumed_thread_enforces_rolling_window_without_restarting_cursor(self):
        self.grant.history_days = 7
        self.grant.save()
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        root = f"{now - 6 * 86400}.000001"
        old = f"{now - 8 * 86400}.000001"
        job = schedule_job(state, "thread", source_object_key=root)
        job.checkpoint = {"scan_id": "in-progress", "upper_bound": f"{now - 2 * 86400}.999999",
                          "oldest": f"{now - 9 * 86400}.000000", "cursor": "next-page"}
        job.save()
        response = {"ok": True, "messages": [
            {"ts": old, "user": "UOTHER", "text": "aged out"},
            {"ts": root, "user": "UOTHER", "text": "current"},
        ]}
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority', return_value=response) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertEqual(api.call_args.kwargs["cursor"], "next-page")
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=old).exists())
        self.assertTrue(self.conversation.deliveries.filter(source_message_id=root).exists())

    def test_reduced_consent_restarts_saved_thread_cursor(self):
        self.grant.history_days = 7
        self.grant.save()
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        root = f"{now - 86400}.000001"
        job = schedule_job(state, "thread", source_object_key=root)
        job.checkpoint = {"scan_id": "old-consent", "upper_bound": f"{now}.999999",
                          "oldest": f"{now - 30 * 86400}.000000", "cursor": "old-cursor"}
        job.save()
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority',
                   return_value={"ok": True, "messages": []}) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertNotIn("cursor", api.call_args.kwargs)
        self.assertGreaterEqual(int(api.call_args.kwargs["oldest"].split(".")[0]), now - 7 * 86400)

    def test_legacy_zero_consent_bounds_reply_query_even_for_old_root(self):
        self.grant.history_days = 0
        self.grant.save()
        state = ensure_state(self.conversation)
        root = f"{int(timezone.now().timestamp()) - 60 * 86400}.000001"
        schedule_job(state, "thread", source_object_key=root)
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority',
                   return_value={"ok": True, "messages": []}) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertEqual(api.call_args.args[1], "conversations_replies")
        self.assertGreaterEqual(int(api.call_args.kwargs["oldest"].split(".")[0]),
                                int(timezone.now().timestamp()) - 30 * 86400 - 1)

    def test_explicit_all_history_thread_omits_zero_oldest(self):
        from integrations.services import slack_dm_mirror as dm
        self.grant.history_days = 0
        self.grant.consent_version = dm.ALL_HISTORY_CONSENT
        self.grant.save()
        state = ensure_state(self.conversation)
        root = f"{int(timezone.now().timestamp()) - 60 * 86400}.000001"
        schedule_job(state, "thread", source_object_key=root)
        with patch.object(dm, '_call_slack_with_grant_authority',
                          return_value={"ok": True, "messages": []}) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertNotIn("oldest", api.call_args.kwargs)

    def test_thread_root_aged_out_still_resumes_recent_reply_cursor(self):
        self.grant.history_days = 7
        self.grant.save()
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        root = f"{now - 8 * 86400}.000001"
        job = schedule_job(state, "thread", source_object_key=root)
        job.checkpoint = {"upper_bound": f"{now - 2 * 86400}.999999",
                          "oldest": f"{now - 9 * 86400}.000000", "cursor": "old-page"}
        job.save()
        with patch('integrations.services.slack_dm_mirror._call_slack_with_grant_authority',
                   return_value={"ok": True, "messages": []}) as api:
            private_page(claim_job(kinds=["thread"]), state)
        self.assertEqual(api.call_args.kwargs["cursor"], "old-page")
        self.assertEqual(api.call_args.kwargs["ts"], root)


    def test_archive_stops_at_consent_cutoff_despite_provider_has_more(self):
        from integrations.services import slack_dm_mirror as dm
        self.grant.history_days = 7
        self.grant.save()
        now = int(timezone.now().timestamp())
        recent = f"{now - 86400}.000001"
        old = f"{now - 60 * 86400}.000001"
        response = {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "older-page"},
                    "messages": [
                        {"ts": recent, "user": "UOTHER", "text": "in consent"},
                        {"ts": old, "user": "UOTHER", "text": "outside consent"},
                    ]}
        with patch.object(dm, '_call_slack_with_grant_authority', return_value=response) as api:
            dm._enqueue_history_page(self.conversation, self.grant)
        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.history_backfilled_at)
        self.assertTrue(self.conversation.deliveries.filter(source_message_id=recent).exists())
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=old).exists())
        self.assertEqual(api.call_count, 1)

    def test_archive_thread_filters_old_root_and_keeps_ascending_pagination(self):
        from integrations.services import slack_dm_mirror as dm
        self.grant.history_days = 7
        self.grant.save()
        now = int(timezone.now().timestamp())
        parent = f"{now - 8 * 86400}.000001"
        old_reply = f"{now - 8 * 86400 + 60}.000001"
        recent_reply = f"{now - 86400}.000001"
        authority = dm._capture_slack_grant_api_authority(self.grant)
        scopes = dm._history_required_scopes(self.conversation.slack_conversation_id)
        scan, *_ = dm._prepare_history_scan_page(self.conversation.pk, self.grant.pk, authority, scopes)
        thread = dm._ensure_thread_state(self.conversation, parent, scan_epoch=scan.epoch)
        responses = [
            {"ok": True, "messages": [
                {"ts": parent, "user": "UOTHER", "text": "old root"},
                {"ts": old_reply, "user": "UOTHER", "text": "old reply"},
            ], "has_more": True, "response_metadata": {"next_cursor": "newer-replies"}},
            {"ok": True, "messages": [
                {"ts": recent_reply, "user": "UOTHER", "text": "recent reply"},
            ]},
        ]
        with patch.object(dm, '_call_slack_with_grant_authority', side_effect=responses) as api:
            dm._enqueue_reply_page(self.conversation.pk, self.grant.pk,
                                   self.conversation.slack_conversation_id, authority, scan, thread)
            thread.refresh_from_db()
            self.assertFalse(thread.metadata["complete"])
            self.assertFalse(self.conversation.deliveries.filter(source_message_id__in=[parent, old_reply]).exists())
            dm._enqueue_reply_page(self.conversation.pk, self.grant.pk,
                                   self.conversation.slack_conversation_id, authority, scan, thread)
        self.assertEqual(api.call_args.kwargs["cursor"], "newer-replies")
        self.assertEqual(api.call_args_list[0].kwargs["oldest"], api.call_args_list[1].kwargs["oldest"])
        row = self.conversation.deliveries.get(source_message_id=recent_reply)
        self.assertEqual(row.metadata["thread_ts"], "")
        self.assertEqual(row.metadata["original_thread_ts"], parent)
        thread.refresh_from_db()
        self.assertTrue(thread.metadata["complete"])


    def test_source_limited_empty_archive_cannot_delete_a_received_message(self):
        from integrations.models import SlackDmMirrorDelivery
        from integrations.services import slack_dm_mirror as dm
        state = ensure_state(self.conversation)
        authority = dm._capture_slack_grant_api_authority(self.grant)
        scopes = dm._history_required_scopes(self.conversation.slack_conversation_id)
        scan, *_ = dm._prepare_history_scan_page(self.conversation.pk, self.grant.pk, authority, scopes)
        main = self.conversation.deliveries.get(source_message_id=dm.HISTORY_MAIN_STATE_ID)
        main.metadata = {**main.metadata, dm.HISTORY_RECONCILE_EPOCH_KEY: "limited-scan"}
        main.save()
        recent = f"{int(timezone.now().timestamp()) - 60}.000001"
        prior = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=recent,
            source_author_id="UOTHER", operation="create", status="completed", encrypted_text="",
            available_at=timezone.now(),
            metadata={"history_reconcile_candidate": True,
                      dm.HISTORY_RECONCILE_EPOCH_KEY: "limited-scan",
                      dm.HISTORY_RECONCILE_OLDEST_KEY: scan.oldest},
        )
        with patch.object(dm, '_call_slack_with_grant_authority',
                          return_value={"ok": True, "messages": [], "is_limited": True}):
            dm._enqueue_history_page(self.conversation, self.grant)
        self.assertFalse(self.conversation.deliveries.filter(operation="delete").exists())
        prior.refresh_from_db()
        self.assertEqual(prior.status, "completed")
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["classification"], "source_limited")
        self.assertEqual(state.verified_ranges["archive"]["absence"], "unknown")
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=dm.HISTORY_MAIN_STATE_ID).exists())

    def test_archive_keeps_source_limit_from_an_earlier_page_after_state_cleanup(self):
        from integrations.services import slack_dm_mirror as dm
        state = ensure_state(self.conversation)
        now = int(timezone.now().timestamp())
        responses = [
            {"ok": True, "is_limited": True, "has_more": True,
             "messages": [{"ts": f"{now - 60}.000001", "user": "UOTHER", "text": "available"}]},
            {"ok": True, "messages": []},
        ]
        with patch.object(dm, '_call_slack_with_grant_authority', side_effect=responses):
            dm._enqueue_history_page(self.conversation, self.grant)
            main = self.conversation.deliveries.get(source_message_id=dm.HISTORY_MAIN_STATE_ID)
            self.assertTrue(main.metadata["source_limited"])
            self.conversation.refresh_from_db()
            dm._enqueue_history_page(self.conversation, self.grant)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["classification"], "source_limited")
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=dm.HISTORY_MAIN_STATE_ID).exists())

    def test_source_limited_thread_keeps_whole_archive_unqualified(self):
        from integrations.services import slack_dm_mirror as dm
        state = ensure_state(self.conversation)
        root = f"{int(timezone.now().timestamp()) - 60}.000001"
        responses = [
            {"ok": True, "messages": [
                {"ts": root, "user": "UOTHER", "text": "root", "reply_count": 1},
            ]},
            {"ok": True, "messages": [], "is_limited": True},
        ]
        with patch.object(dm, '_call_slack_with_grant_authority', side_effect=responses):
            dm._enqueue_history_page(self.conversation, self.grant)
            self.conversation.refresh_from_db()
            dm._enqueue_history_page(self.conversation, self.grant)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["classification"], "source_limited")
        self.assertFalse(self.conversation.deliveries.filter(source_message_id=dm.HISTORY_MAIN_STATE_ID).exists())



class PrivateQuietSchedulingTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def test_discovery_quiet_result_defers_new_jobs_without_blocking_later_seed(self):
        from datetime import timedelta
        from integrations.services.message_sync.scheduler import defer_quiet_history_jobs
        before = timezone.now()
        defer_quiet_history_jobs(self.conversation)
        state = ensure_state(self.conversation)
        self.assertEqual(set(state.jobs.values_list("kind", flat=True)), {"head", "archive"})
        self.assertFalse(state.jobs.filter(due_at__lt=before + timedelta(minutes=59)).exists())
        self.assertIsNone(claim_job(kinds=["head", "archive"]))

    def test_quiet_deferral_preserves_active_lease_cursor_and_longer_backoff(self):
        from datetime import timedelta
        from integrations.services.message_sync.scheduler import defer_quiet_history_jobs
        state = ensure_state(self.conversation)
        head = schedule_job(state, "head")
        head.checkpoint = {"cursor": "in-flight-page"}
        head.save()
        lease = claim_job(kinds=["head"])
        head.refresh_from_db()
        previous_due = head.due_at
        archive = schedule_job(state, "archive")
        archive.due_at = timezone.now() + timedelta(hours=2)
        archive.save()
        prior_archive_due = archive.due_at
        defer_quiet_history_jobs(self.conversation)
        head.refresh_from_db()
        archive.refresh_from_db()
        self.assertEqual(head.lease_token, lease.token)
        self.assertEqual(head.checkpoint, {"cursor": "in-flight-page"})
        self.assertEqual(head.due_at, previous_due)
        self.assertEqual(archive.due_at, prior_archive_due)



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
            conversation=self.conversation, source_platform="slack", source_message_id=f"{int(timezone.now().timestamp()) - 60}.000001",
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
