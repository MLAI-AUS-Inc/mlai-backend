"""Selected-window publication and stale registration regressions."""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.test import TransactionTestCase
from django.utils import timezone
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import SlackDmMirrorDelivery
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_chat_catalog import (
    catalog_conversations, catalog_payload, retired_catalog_payload,
)


class SlackImportReadinessTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.conversation.participant_buzz_pubkeys = [self.owner_key]
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 60}.000001"
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save()

    def catalog(self):
        return catalog_payload(catalog_conversations(self.grant.conversations.all()), self.owner_key)

    def test_completed_recent_window_is_visible_but_old_activity_is_hidden(self):
        self.assertTrue(self.catalog()[0]["ready_for_display"])
        self.conversation.latest_synced_ts = f"{int(timezone.now().timestamp()) - 31 * 86400}.000001"
        self.conversation.save()
        self.assertFalse(self.catalog()[0]["ready_for_display"])

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
        self.assertEqual(retired_catalog_payload(self.grant, self.owner_key, {old_id}), [])

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
        self.conversation.history_backfilled_at = None
        self.conversation.save()
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
        with patch("integrations.services.slack_chat_catalog.cache.get_many", side_effect=RuntimeError("cache unavailable")), patch(
            "integrations.services.slack_chat_catalog.cache.set_many", side_effect=RuntimeError("cache unavailable")
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
        from integrations.models import BridgeSyncState
        BridgeSyncState.objects.create(
            private_conversation=self.conversation, workspace_id="TIOAUTH",
            source_channel_id="DIOAUTH",
            verified_ranges={"archive": {"classification": "source_limited"}},
        )
        self.assertFalse(self.catalog()[0]["ready_for_display"])

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
        self.assertEqual(retired_catalog_payload(self.grant, self.owner_key, set())[0]["channel_id"], old_id)
