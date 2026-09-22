"""Selected-window publication and stale registration regressions."""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from community_chat.tests.test_slack_dm_io_authority import SCOPES, SlackDmIoAuthorityFixture
from integrations.models import BridgeSyncState, SlackDmMirrorConversation, SlackDmMirrorDelivery, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_chat_catalog import (
    _publication_key, catalog_conversations, catalog_payload, retired_catalog_payload,
)


class SlackImportReadinessTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.conversation.participant_buzz_pubkeys = [self.owner_key]
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 60}.000001"
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()
        self.state = BridgeSyncState.objects.create(
            private_conversation=self.conversation, workspace_id="TIOAUTH",
            source_channel_id="DIOAUTH", verified_ranges={"archive": self.archive_proof()},
        )

    def archive_proof(self, **overrides):
        return {
            "classification": "accessible_range", "import_contract_version": 2,
            "participant_hash": self.conversation.participant_hash,
            "channel_id": str(self.conversation.mlai_channel_id), **overrides,
        }

    def catalog(self):
        return catalog_payload(catalog_conversations(self.grant.conversations.all()), self.owner_key)

    def record_publication(self):
        from integrations.services.message_sync.publication import record_publication_locked
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(pk=self.conversation.pk)
            conversation.grant = grant
            return record_publication_locked(conversation)

    def test_recent_import_requires_versioned_current_room_archive_proof(self):
        for proof in (
            {}, {"classification": "accessible_range"},
            self.archive_proof(import_contract_version=1),
            self.archive_proof(participant_hash="old-audience"),
            self.archive_proof(channel_id=str(uuid.uuid4())),
            self.archive_proof(classification="unknown"),
        ):
            with self.subTest(proof=proof):
                self.state.verified_ranges = {"archive": proof}
                self.state.save(update_fields=["verified_ranges"])
                self.assertFalse(self.catalog()[0]["ready_for_display"])
                status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
                self.assertEqual(status["backfill"]["complete"], 0)
                self.assertEqual(status["backfill"]["pending"], 1)
        self.state.verified_ranges = {"archive": self.archive_proof()}
        self.state.save(update_fields=["verified_ranges"])
        self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_previous_publication_cache_cannot_bypass_current_room_proof(self):
        self.state.verified_ranges = {}
        self.state.save(update_fields=["verified_ranges"])
        old_key = _publication_key(self.conversation).replace("published-v3:", "published-v1:")
        cache.set(old_key, True, 86400)
        self.assertFalse(self.catalog()[0]["ready_for_display"])

    def test_completed_old_activity_does_not_require_recent_room_reimport(self):
        self.state.verified_ranges = {}
        self.state.save(update_fields=["verified_ranges"])
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 31 * 86400}.000001"
        self.conversation.save()
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["complete"], 1)
        self.assertEqual(status["backfill"]["pending"], 0)
        self.assertFalse(status["channel_catalog"][0]["ready_for_display"])

    def test_only_fresh_scoped_quiet_source_check_completes_unverified_progress(self):
        self.state.verified_ranges = {}
        self.state.save(update_fields=["verified_ranges"])
        self.conversation.latest_synced_ts = ""
        self.conversation.save()
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        scope = {**dm._discovery_checkpoint_identity(authority), "history_days": 30}
        for age, history_days, expected in ((30, 30, 1), (3601, 30, 0), (30, 7, 0)):
            with self.subTest(age=age, history_days=history_days):
                self.connection.sync_cursor = {dm.RECENT_ACTIVITY_CACHE_KEY: {
                    **scope, "history_days": history_days,
                    "entries": {self.conversation.slack_conversation_id: {
                        "activity": 0, "checked_at": int(timezone.now().timestamp()) - age,
                    }},
                }}
                self.connection.save(update_fields=["sync_cursor"])
                with patch.object(dm, "WebClient") as provider:
                    status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
                provider.assert_not_called()
                self.assertEqual(status["backfill"]["complete"], expected)
                self.assertEqual(status["backfill"]["pending"], 1 - expected)
                self.assertFalse(status["channel_catalog"][0]["ready_for_display"])

    def test_completed_recent_window_is_visible_but_old_activity_is_hidden(self):
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 31 * 86400}.000001"
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["complete"], 1)
        self.assertEqual(status["backfill"]["pending"], 0)

    def test_seven_day_selection_hides_eight_day_activity(self):
        self.grant.history_days = 7
        self.grant.save()
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 8 * 86400}.000001"
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])

    def test_incomplete_scan_and_undelivered_page_remain_hidden(self):
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()
        row = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack",
            source_message_id=self.conversation.latest_synced_ts, source_author_id="UOTHER",
            operation="create", metadata={"backfill": True}, available_at=timezone.now(),
        )
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        row.status = "completed"
        row.save()
        self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_resume_does_not_publish_previous_consent_scan(self):
        self.grant.consented_at = timezone.now() + timedelta(seconds=1)
        self.grant.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["complete"], 0)
        self.assertEqual(status["backfill"]["pending"], 1)

    def test_paused_chat_stays_in_catalog_as_a_hidden_type_fence(self):
        self.conversation.status = "paused"
        self.conversation.save()
        item = self.catalog()[0]
        self.assertEqual(item["kind"], "im")
        self.assertFalse(item["ready_for_display"])

    def test_retired_registrations_are_deduplicated_owner_only_id_fences(self):
        old_id = str(uuid.uuid4())
        for number in range(2):
            SlackDmMirrorDelivery.objects.create(
                conversation=self.conversation, source_platform="buzz",
                source_message_id=f"registration-state:{number}", source_author_id="",
                operation="create", status="completed", available_at=timezone.now(),
                metadata={"registration_control": True, "channel_id": old_id,
                          "conversation_name": "old private name", "participant_pubkeys": [self.owner_key]},
            )
        entries = retired_catalog_payload(self.grant, self.owner_key, {str(self.conversation.mlai_channel_id)})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["channel_id"], old_id)
        self.assertFalse(entries[0]["ready_for_display"])
        self.assertNotIn("private name", str(entries))
        self.assertEqual(retired_catalog_payload(self.grant, "f" * 64, set()), [])
        self.assertEqual(retired_catalog_payload(self.grant, self.owner_key, {old_id, str(self.conversation.mlai_channel_id)}), [])

    def test_delayed_live_create_and_edit_obey_original_message_time(self):
        old_ts = f"{int(timezone.now().timestamp()) - 31 * 86400}.000001"
        row = SlackDmMirrorDelivery(
            conversation=self.conversation, source_platform="slack", source_message_id=old_ts,
            operation="create", metadata={"event_ts": f"{int(timezone.now().timestamp())}.000001"},
        )
        self.assertTrue(dm._backfill_delivery_is_outside_history_window(row))
        row.operation = "edit"
        self.assertTrue(dm._backfill_delivery_is_outside_history_window(row))
        row.operation = "delete"
        self.assertFalse(dm._backfill_delivery_is_outside_history_window(row))

    def test_published_chat_survives_routine_refresh_but_not_new_consent(self):
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        with transaction.atomic():
            dm._mark_conversation_history_due(self.conversation, reason="Routine refresh", reset_deliveries=False)
        cache.clear()
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.grant.consented_at = timezone.now() + timedelta(seconds=1)
        self.grant.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])

    def test_aged_out_reconciliation_candidate_is_not_inferred_deleted(self):
        now = timezone.now()
        self.grant.history_days = 7
        self.grant.save()
        self.conversation.grant = self.grant
        oldest = str(int(now.timestamp()) - 9 * 86400)
        target = f"{int(now.timestamp()) - 8 * 86400}.000001"
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack",
            source_message_id=dm.HISTORY_MAIN_STATE_ID, source_author_id="",
            operation="create", status="completed", available_at=now,
            metadata={dm.HISTORY_RECONCILE_EPOCH_KEY: "scan", "oldest": oldest},
        )
        row = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack",
            source_message_id=target, source_author_id="UOTHER",
            operation="create", status="completed", available_at=now,
            metadata={"history_reconcile_candidate": True,
                      dm.HISTORY_RECONCILE_EPOCH_KEY: "scan",
                      dm.HISTORY_RECONCILE_OLDEST_KEY: oldest},
        )
        from django.db import transaction
        with transaction.atomic():
            dm._reconcile_absent_slack_state_locked(self.conversation)
        self.assertFalse(SlackDmMirrorDelivery.objects.filter(operation="delete").exists())
        row.refresh_from_db()
        self.assertNotIn("history_reconcile_candidate", row.metadata)

    def test_optional_publication_cache_failure_preserves_durable_ready_import(self):
        with patch.object(cache, "get_many", side_effect=RuntimeError("cache unavailable")), patch.object(
            cache, "set_many", side_effect=RuntimeError("cache unavailable")
        ):
            self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_outside_window_rows_do_not_hold_status_progress_open(self):
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack",
            source_message_id="1700000000.000001", source_author_id="UOTHER",
            operation="create", status="dead", available_at=timezone.now(),
            metadata={"backfill": True, "history_outside_window": True},
        )
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["failed_messages"], 0)
        self.assertEqual(status["backfill"]["pending"], 0)
        self.assertTrue(status["channel_catalog"][0]["ready_for_display"])

    def test_source_limited_archive_cannot_publish_first_import(self):
        state = self.state
        state.verified_ranges = {"archive": self.archive_proof(classification="source_limited")}
        state.save(update_fields=["verified_ranges"])
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["complete"], 0)
        self.assertEqual(status["backfill"]["pending"], 1)
        state.verified_ranges = {"archive": self.archive_proof()}
        state.save(update_fields=["verified_ranges"])
        status = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
        self.assertEqual(status["backfill"]["complete"], 1)
        self.assertEqual(status["backfill"]["pending"], 0)
        self.assertTrue(status["channel_catalog"][0]["ready_for_display"])

    def test_retired_ids_from_previous_slack_identity_remain_owner_scoped(self):
        from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant, SlackDmMirrorConversation
        connection = ExternalServiceConnection.objects.create(
            user=self.user, provider="slack", external_account_id="TOLD", scopes=[],
        )
        previous = SlackDmMirrorGrant.objects.create(
            user=self.user, connection=connection, slack_workspace_id="TOLD",
            slack_user_id="UOLD", status="revoked", revoked_at=timezone.now(),
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=previous, slack_workspace_id="TOLD", slack_conversation_id="DOLD", status="paused",
        )
        old_id = str(uuid.uuid4())
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation, source_platform="buzz", source_message_id="registration-state:oldidentity",
            source_author_id="", operation="create", status="completed", available_at=timezone.now(),
            metadata={"registration_control": True, "channel_id": old_id},
        )
        self.assertIn(old_id, {entry["channel_id"] for entry in retired_catalog_payload(self.grant, self.owner_key, set())})

    def test_inactive_or_disconnected_status_returns_only_owner_id_fences(self):
        old_id = str(uuid.uuid4())
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="buzz", source_message_id="registration-state:old",
            source_author_id="", operation="create", status="completed", available_at=timezone.now(),
            metadata={"registration_control": True, "channel_id": old_id, "conversation_name": "private name"},
        )
        for grant_status, connection_status in (("paused", "connected"), ("revoked", "disconnected"), ("active", "disconnected")):
            with self.subTest(grant_status=grant_status, connection_status=connection_status):
                self.grant.status = grant_status
                self.grant.revoked_at = timezone.now() if grant_status == "revoked" else None
                self.grant.save()
                self.connection.status = connection_status
                self.connection.save()
                self.conversation.participant_buzz_pubkeys = []
                self.conversation.save()
                payload = dm.status_payload(self.user, authenticated_public_key=self.owner_key)
                entries = payload["channel_catalog"]
                self.assertEqual({entry["channel_id"] for entry in entries}, {old_id, str(self.conversation.mlai_channel_id)})
                for entry in entries:
                    self.assertFalse(entry["ready_for_display"])
                    self.assertEqual(set(entry), {"channel_id", "kind", "ready_for_display", "last_message_at", "history_oldest_ts"})
                self.assertNotIn("private name", str(entries))

    def test_status_catalog_rejects_revoked_or_foreign_device(self):
        from community_chat.models import CommunityChatDevice
        from django.contrib.auth import get_user_model
        self.assertTrue(dm.status_payload(self.user, authenticated_public_key=self.owner_key)["channel_catalog"])
        foreign = get_user_model().objects.create_user(email="foreign-fence-owner@example.com")
        self.assertEqual(dm.status_payload(foreign, authenticated_public_key=self.owner_key)["channel_catalog"], [])
        CommunityChatDevice.objects.filter(user=self.user).update(status="revoked", revoked_at=timezone.now())
        self.assertEqual(dm.status_payload(self.user, authenticated_public_key=self.owner_key)["channel_catalog"], [])

    def test_no_grant_status_has_no_fences_and_no_provider_io(self):
        from community_chat.models import CommunityChatDevice
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(email="no-grant-fence-owner@example.com")
        key = "e" * 64
        CommunityChatDevice.objects.create(user=user, public_key=key, status="verified", verified_at=timezone.now())
        with patch.object(dm, "WebClient") as provider:
            payload = dm.status_payload(user, authenticated_public_key=key)
        self.assertEqual(payload["status"], "not_connected")
        self.assertEqual(payload["channel_catalog"], [])
        provider.assert_not_called()

    def test_completed_empty_dm_requires_exact_owner_open_intent(self):
        from integrations.services.slack_chat_catalog import CATALOG_KEY, OWNER_OPENED_KEY, owner_open_intent
        self.conversation.latest_synced_ts = ""
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        marker = owner_open_intent(self.grant, self.owner_key)
        self.connection.provider_metadata = {**self.connection.provider_metadata,
            CATALOG_KEY: {self.conversation.slack_conversation_id: {OWNER_OPENED_KEY: marker}}}
        self.connection.save()
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.assertIsNone(self.catalog()[0]["last_message_at"])
        cache.clear()
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 31 * 86400}.000001"
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])

    def test_empty_open_intent_expires_on_device_consent_or_oauth_change(self):
        from integrations.services.slack_chat_catalog import CATALOG_KEY, OWNER_OPENED_KEY, owner_open_intent
        from integrations.services.slack_oauth_authority import SLACK_OAUTH_GENERATION_KEY
        self.conversation.latest_synced_ts = ""
        self.conversation.save()
        marker = owner_open_intent(self.grant, self.owner_key)
        metadata = {**self.connection.provider_metadata,
            CATALOG_KEY: {self.conversation.slack_conversation_id: {OWNER_OPENED_KEY: marker}}}
        self.connection.provider_metadata = metadata
        self.connection.save()
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        foreign_key = "e" * 64
        self.conversation.participant_buzz_pubkeys.append(foreign_key)
        self.conversation.save()
        entries = catalog_payload(catalog_conversations(self.grant.conversations.all()), foreign_key)
        self.assertFalse(entries[0]["ready_for_display"])
        self.connection.provider_metadata = {**metadata, SLACK_OAUTH_GENERATION_KEY: 1}
        self.connection.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        self.connection.provider_metadata = metadata
        self.connection.save()
        self.grant.consented_at = timezone.now() + timedelta(seconds=1)
        self.grant.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])

    def test_durable_publication_survives_cache_loss_and_background_scan(self):
        self.assertTrue(self.record_publication())
        self.state.refresh_from_db()
        publication = dict(self.state.verified_ranges["publication"])
        with transaction.atomic():
            dm._mark_conversation_history_due(self.conversation, reason="Automatic refresh", reset_deliveries=False)
        self.state.refresh_from_db()
        self.state.verified_ranges["archive"] = {"classification": "incomplete"}
        self.state.save(update_fields=["verified_ranges"])
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"], publication)
        self.assertTrue(self.record_publication())
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"], publication)

    def test_legacy_import_is_qualified_before_refresh_without_trusting_cache(self):
        cache.set(_publication_key(self.conversation), True, 86400)
        with transaction.atomic():
            dm._mark_conversation_history_due(self.conversation, reason="Automatic refresh", reset_deliveries=False)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"]["scope"], _publication_key(self.conversation))
        cache.clear()
        self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_head_page_retains_legacy_publication_before_enqueuing_fresh_messages(self):
        from integrations.services.message_sync.private_history import private_page
        from integrations.services.message_sync.scheduler import claim_job, schedule_job

        schedule_job(self.state, "head")
        timestamp = f"{int(timezone.now().timestamp()) - 30}.000001"
        with patch.object(dm, "_call_slack_with_grant_authority", return_value={
            "ok": True, "messages": [{"ts": timestamp, "user": "UOTHER", "text": "new message"}],
        }):
            private_page(claim_job(kinds=["head"]), self.state)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"]["scope"], _publication_key(self.conversation))
        self.assertEqual(self.state.verified_ranges["head"]["classification"], "accessible_range")
        self.assertTrue(self.conversation.deliveries.filter(
            source_message_id=timestamp, status="pending", metadata__backfill=True,
        ).exists())
        cache.clear()
        self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_explicit_current_coverage_request_refreshes_without_hiding_publication(self):
        from integrations.services.message_sync.private_coverage import request_current_coverage

        self.assertTrue(self.record_publication())
        self.state.refresh_from_db()
        published = self.state.verified_ranges["publication"]
        self.state.verified_ranges["archive"] = {"classification": "unknown"}
        self.state.save(update_fields=["verified_ranges"])
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.assertTrue(request_current_coverage(self.conversation, authority, set(SCOPES)))
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.history_backfilled_at)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"], published)
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.assertFalse(request_current_coverage(self.conversation, authority, set(SCOPES)))

    def test_recovery_scheduling_preserves_publication_and_operational_state(self):
        from integrations.services.message_sync.recovery import schedule_private_recoveries

        self.assertTrue(self.record_publication())
        self.state.refresh_from_db()
        published = self.state.verified_ranges["publication"]
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", status="dead",
            source_message_id=f"{int(timezone.now().timestamp()) - 30}.000001",
            source_author_id="UOTHER", operation="create", metadata={"backfill": True}, available_at=timezone.now(),
        )
        self.assertEqual(schedule_private_recoveries(), 1)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"], published)
        self.assertIn("scheduled_at", self.state.verified_ranges["recovery"])
        self.assertTrue(self.catalog()[0]["ready_for_display"])

    def test_pending_failed_and_dead_deliveries_cannot_mint_publication(self):
        row = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=self.conversation.latest_synced_ts,
            source_author_id="UOTHER", operation="create", metadata={"backfill": True}, available_at=timezone.now(),
        )
        for status in ("pending", "processing", "failed", "dead"):
            row.status = status
            row.save()
            cache.set(_publication_key(self.conversation), True, 86400)
            self.assertFalse(self.record_publication())
            self.assertFalse(self.catalog()[0]["ready_for_display"])
        self.state.refresh_from_db()
        self.assertNotIn("publication", self.state.verified_ranges)

    def test_empty_scan_commits_publication_only_with_qualified_source_coverage(self):
        self.state.verified_ranges = {}
        self.state.save()
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=dm.HISTORY_MAIN_STATE_ID,
            operation="create", status="completed", available_at=timezone.now(), metadata={
                "import_contract_version": 2, "participant_hash": self.conversation.participant_hash,
                "mlai_channel_id": str(self.conversation.mlai_channel_id), "observed_messages": False,
            },
        )
        with transaction.atomic():
            dm._finish_history_scan(self.conversation)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["archive"]["classification"], "empty_accessible_range")
        self.assertIn("publication", self.state.verified_ranges)

    def test_last_single_delivery_commits_publication_after_success(self):
        self.conversation.participant_identity_map = {"UOWNER": self.owner_key, "UOTHER": "b" * 64}
        self.conversation.participant_buzz_pubkeys.append("b" * 64)
        self.conversation.save()
        rows = [SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=f"{int(timezone.now().timestamp()) - 10}.{index:06d}",
            source_author_id="UOTHER", operation="create", status="processing", encrypted_text="Synthetic message",
            metadata={"backfill": True, "participant_hash": self.conversation.participant_hash}, available_at=timezone.now(),
        ) for index in range(2)]
        with patch.object(dm.BuzzBridgeClient, "deliver_private", return_value={"message_id": "e" * 64}):
            dm._deliver_private(rows[0])
            self.state.refresh_from_db()
            self.assertNotIn("publication", self.state.verified_ranges)
            dm._deliver_private(rows[1])
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"]["scope"], _publication_key(self.conversation))

    def test_last_batch_delivery_commits_publication(self):
        self.conversation.participant_identity_map = {"UOWNER": self.owner_key, "UOTHER": "b" * 64}
        self.conversation.participant_buzz_pubkeys.append("b" * 64)
        self.conversation.save()
        rows = [SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=f"{int(timezone.now().timestamp()) - 10}.{index:06d}",
            source_author_id="UOTHER", operation="create", status="processing", encrypted_text="Synthetic message",
            metadata={"backfill": True, "participant_hash": self.conversation.participant_hash}, available_at=timezone.now(),
        ) for index in range(2)]
        with patch.object(dm.BuzzBridgeClient, "deliver_private_batch", return_value=[{"message_id": "e" * 64}, {"message_id": "f" * 64}]):
            dm._deliver_private_batch(rows)
        self.state.refresh_from_db()
        self.assertEqual(self.state.verified_ranges["publication"]["scope"], _publication_key(self.conversation))

    def test_publication_scope_cannot_survive_consent_window_room_or_audience_change(self):
        self.assertTrue(self.record_publication())
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        mutations = [
            (self.grant, "consented_at", timezone.now() + timedelta(seconds=1)),
            (self.grant, "consent_version", "changed-consent"),
            (self.grant, "slack_user_id", "UANOTHER"),
            (self.grant, "history_days", 7),
            (self.conversation, "mlai_channel_id", uuid.uuid4()),
            (self.conversation, "participant_hash", "different-audience"),
            (self.conversation, "participant_slack_ids", ["UOWNER", "UOTHER", "UADDED"]),
            (self.conversation, "participant_buzz_pubkeys", [self.owner_key, "c" * 64]),
        ]
        for obj, field, replacement in mutations:
            with self.subTest(field=field):
                before = getattr(obj, field)
                setattr(obj, field, replacement)
                obj.save()
                self.assertFalse(self.catalog()[0]["ready_for_display"])
                setattr(obj, field, before)
                obj.save()

    def test_explicit_reset_and_source_retirement_erase_publication(self):
        self.assertTrue(self.record_publication())
        with transaction.atomic():
            dm._mark_conversation_history_due(self.conversation, reason="Explicit reset", reset_deliveries=True)
        self.state.refresh_from_db()
        self.assertNotIn("publication", self.state.verified_ranges)
        self.assertFalse(self.catalog()[0]["ready_for_display"])
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()
        self.assertTrue(self.record_publication())
        dm._retire_ineligible_conversation(self.grant.pk, self.conversation.slack_conversation_id,
                                         reason="Source access removed", reconcile_cleanup=False)
        self.state.refresh_from_db()
        self.assertNotIn("publication", self.state.verified_ranges)

    def test_source_limited_or_unqualified_scan_never_mints_publication(self):
        for proof in ({}, self.archive_proof(classification="source_limited"), self.archive_proof(channel_id=str(uuid.uuid4()))):
            self.state.verified_ranges = {"archive": proof}
            self.state.save()
            self.assertFalse(self.record_publication())
        self.state.refresh_from_db()
        self.assertNotIn("publication", self.state.verified_ranges)
