"""Database-free availability, delivery and privacy-fence regressions."""

from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from integrations.services import slack_chat_catalog as catalog
from integrations.services import slack_chat_refresh as refresh
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_owner_inventory_api as inventory
from integrations.services.message_sync import presentation, publication
from integrations.services.slack_oauth_authority import SLACK_OAUTH_GENERATION_KEY


class SlackProgressivePresentationTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.key = "a" * 64
        self.grant = SimpleNamespace(
            pk=1, user_id=2, connection_id=3, history_days=30,
            consent_version=catalog.PRIVATE_CHANNEL_CONSENT,
            consented_at=self.now - timedelta(days=2), status="active", revoked_at=None,
            slack_workspace_id="TTEST", slack_user_id="UOWNER",
            connection=SimpleNamespace(provider_metadata={}), conversations=MagicMock(),
        )
        self.conversation = SimpleNamespace(
            pk=4, grant=self.grant, status="live", mlai_channel_id=uuid4(),
            slack_workspace_id="TTEST", slack_conversation_id="DTEST",
            participant_hash="current-audience", participant_buzz_pubkeys=[self.key],
            participant_slack_ids=["UOWNER", "UOTHER"], participant_profiles={},
            history_backfilled_at=None, latest_synced_ts=f"{int(self.now.timestamp()) - 60}.000001",
            _publication_scope=None, _presentation_scope=None, _presentation_source_ts=None,
            _import_pending=True, _import_verified=False, _import_limited=False,
            last_error="", deliveries=MagicMock(), save=MagicMock(),
        )
        self.state = SimpleNamespace(verified_ranges={"archive": {"classification": "incomplete"}}, save=MagicMock())
        self.objects = MagicMock()
        self.objects.select_for_update.return_value.filter.return_value.first.return_value = self.state
        patcher = patch.object(presentation.BridgeSyncState, "objects", self.objects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def delivery(self, **changes):
        values = dict(
            conversation_id=self.conversation.pk, source_platform="slack", operation="create",
            source_message_id=self.conversation.latest_synced_ts, status="completed", completed_at=self.now,
            metadata={"participant_hash": self.conversation.participant_hash,
                      "destination_channel_id": str(self.conversation.mlai_channel_id),
                      "destination_message_id": "e" * 64},
        )
        values.update(changes)
        return SimpleNamespace(**values)

    def record(self, deliveries=None):
        return presentation.record_presentation_locked(self.conversation, deliveries or [self.delivery()])

    def apply_receipt(self):
        receipt = self.state.verified_ranges["presentation"]
        self.conversation._presentation_scope = receipt["scope"]
        self.conversation._presentation_source_ts = receipt["source_message_ts"]

    def ready(self):
        return catalog.ready_for_display(self.conversation, now=self.now, public_key=self.key)

    def test_first_delivered_page_is_readable_before_archive_and_queue_complete(self):
        self.assertFalse(self.ready())
        self.assertTrue(self.record())
        self.apply_receipt()
        self.assertTrue(self.ready())
        self.assertFalse(catalog.history_complete(self.conversation))
        self.assertNotIn("publication", self.state.verified_ranges)
        self.assertEqual(self.state.verified_ranges["archive"]["classification"], "incomplete")
        with patch("integrations.services.slack_channel_mentions.roo_channel_targets", return_value=[]):
            entry = catalog.catalog_payload([self.conversation], self.key)[0]
        self.assertTrue(entry["ready_for_display"])
        self.assertFalse(entry["history_complete"])
        self.assertEqual(entry["slack_conversation_id"], "DTEST")

    def test_source_limited_accessible_messages_are_readable_without_claiming_complete(self):
        self.conversation._import_limited = True
        self.conversation.history_backfilled_at = self.now
        self.state.verified_ranges["archive"] = {"classification": "source_limited"}
        self.assertFalse(self.ready())  # No acknowledged messages means no invented content.
        self.assertTrue(self.record())
        self.apply_receipt()
        self.assertTrue(self.ready())
        self.assertFalse(catalog.history_complete(self.conversation))
        self.assertNotIn("publication", self.state.verified_ranges)

    def test_receipt_requires_actual_current_room_create_acknowledgement(self):
        mutations = [
            {"status": "pending"}, {"operation": "edit"}, {"source_platform": "buzz"},
            {"conversation_id": 99}, {"completed_at": None},
            {"completed_at": self.grant.consented_at - timedelta(seconds=1)},
            {"source_message_id": "history-state:main"},
            {"source_message_id": f"{int(self.now.timestamp()) - 31 * 86400}.000001"},
            {"source_message_id": f"{int(self.now.timestamp()) + 600}.000001"},
        ]
        for changes in mutations:
            with self.subTest(changes=changes):
                self.assertFalse(self.record([self.delivery(**changes)]))
        for key, value in [
            ("participant_hash", "old-audience"), ("destination_channel_id", str(uuid4())),
            ("destination_message_id", ""), ("history_outside_window", True),
            ("history_recovery_superseded", True), ("dependency_superseded", True),
        ]:
            row = self.delivery()
            row.metadata[key] = value
            with self.subTest(metadata=key):
                self.assertFalse(self.record([row]))
        self.state.save.assert_not_called()

    def test_consent_owner_source_room_and_device_changes_invalidate_partial_view(self):
        self.record()
        self.apply_receipt()
        mutations = [
            (self.grant, "user_id", 99), (self.grant, "connection_id", 99),
            (self.grant, "consented_at", self.now), (self.grant, "history_days", 7),
            (self.grant, "consent_version", "changed"), (self.grant, "slack_user_id", "UOTHER"),
            (self.grant, "status", "paused"), (self.grant, "revoked_at", self.now),
            (self.conversation, "slack_conversation_id", "DOTHER"),
            (self.conversation, "mlai_channel_id", uuid4()),
            (self.conversation, "participant_hash", "replacement"),
            (self.conversation, "participant_buzz_pubkeys", [self.key, "b" * 64]),
            (self.conversation, "participant_slack_ids", ["UOWNER", "UNEW"]),
            (self.conversation, "status", "paused"),
        ]
        for obj, key, value in mutations:
            old = getattr(obj, key)
            setattr(obj, key, value)
            with self.subTest(field=key):
                self.assertFalse(self.ready())
            setattr(obj, key, old)
        self.grant.connection.provider_metadata[SLACK_OAUTH_GENERATION_KEY] = 1
        self.assertFalse(self.ready())

    def test_old_or_malformed_receipt_never_shows_out_of_window_content(self):
        self.record()
        self.apply_receipt()
        for value in ("", "NaN", "Infinity", "bad", str(int(self.now.timestamp()) - 31 * 86400)):
            self.conversation._presentation_source_ts = value
            with self.subTest(value=value):
                self.assertFalse(self.ready())

    def test_other_device_cannot_read_partial_catalog(self):
        self.record()
        self.apply_receipt()
        self.assertEqual(catalog.catalog_payload([self.conversation], "b" * 64), [])

    def test_receipts_remain_monotonic_during_older_archive_delivery(self):
        self.record()
        saved = deepcopy(self.state.verified_ranges)
        self.state.save.reset_mock()
        older = self.delivery(source_message_id=f"{int(self.now.timestamp()) - 86400}.000001")
        self.assertTrue(self.record([older]))
        self.assertEqual(self.state.verified_ranges, saved)
        self.state.save.assert_not_called()

    def test_reset_removes_partial_receipt_even_without_complete_publication(self):
        self.record()
        publication.invalidate_publication_locked(self.conversation)
        self.assertNotIn("presentation", self.state.verified_ranges)
        self.assertIn("archive", self.state.verified_ranges)

    def test_stable_device_transition_retains_only_same_room_authorized_receipt(self):
        self.record()
        proof = presentation.presentation_for_transition(self.conversation)
        registration = SimpleNamespace(metadata={"private_audience": {"presentation_proof": proof}})
        self.conversation.participant_hash = "new-device-audience"
        self.conversation.participant_buzz_pubkeys.append("b" * 64)
        self.apply_receipt()
        self.assertFalse(self.ready())
        presentation.rebind_presentation_locked(self.conversation, registration)
        self.apply_receipt()
        self.assertTrue(self.ready())
        self.assertEqual(self.state.verified_ranges["presentation"]["published_at"], proof["published_at"])

    def test_transition_cannot_restore_proof_after_reset_or_consent_change(self):
        self.record()
        proof = presentation.presentation_for_transition(self.conversation)
        registration = SimpleNamespace(metadata={"private_audience": {"presentation_proof": proof}})
        self.grant.consented_at = self.now
        presentation.rebind_presentation_locked(self.conversation, registration)
        self.assertEqual(self.state.verified_ranges["presentation"], proof)
        self.state.verified_ranges.pop("presentation")
        presentation.rebind_presentation_locked(self.conversation, registration)
        self.assertNotIn("presentation", self.state.verified_ranges)

    def test_archive_source_limit_is_not_hidden_by_accessible_head(self):
        rows = self.conversation.deliveries
        rows.filter.return_value = rows
        rows.exclude.return_value = rows
        rows.aggregate.return_value = {
            "imported_messages": 2, "queued_messages": 0, "failed_messages": 0,
            "delivery_revision": self.now,
        }
        self.state.last_error_code = ""
        self.state.verified_ranges = {
            "archive": {"classification": "source_limited", "absence": "unknown",
                        "participant_hash": self.conversation.participant_hash,
                        "channel_id": str(self.conversation.mlai_channel_id)},
            "head": {"classification": "accessible_range"},
        }
        self.objects.filter.return_value.first.return_value = self.state
        result = refresh._refresh_status(self.conversation)
        self.assertEqual(result["source_coverage"]["classification"], "source_limited")
        self.assertEqual(result["delivery_revision"], self.now)
        revision_filter = rows.aggregate.call_args.kwargs["delivery_revision"].filter
        self.assertEqual(revision_filter.children, [("status", "completed")])

    def recovery(self, rows, receipts, *, device_authorized=True, before_id=None):
        self.grant.conversations.filter.return_value.first.return_value = self.conversation
        queryset = MagicMock()
        queryset.filter.return_value = queryset
        queryset.order_by.return_value = queryset
        queryset.__getitem__.return_value = rows
        device = SimpleNamespace(public_key=self.key)
        with patch.object(presentation.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            presentation.SlackDmMirrorDelivery, "objects", queryset,
        ), patch.object(dm, "_locked_history_write_context", return_value=(self.conversation, self.grant)), patch.object(
            dm, "_locked_active_verified_device", return_value=device if device_authorized else None,
        ), patch.object(dm, "_ensure_current_registration_row_locked", return_value=object()), patch.object(
            dm, "_registration_state", return_value=dm.REGISTRATION_STATE_ACTIVE,
        ), patch.object(dm, "_registration_channel_id", return_value=str(self.conversation.mlai_channel_id)), patch.object(
            dm.BuzzBridgeClient, "private_delivery_receipts", return_value=receipts,
        ) as relay, patch.object(dm, "_call_slack_with_grant_authority", side_effect=AssertionError("No Slack I/O")):
            result = presentation.recover_presentation(self.grant, object(), device, "DTEST", before_id=before_id)
        return result, relay, queryset

    def legacy_row(self, pk=12):
        row = self.delivery(pk=pk, save=MagicMock())
        row.metadata.pop("destination_channel_id")
        return row

    def test_upgrade_recovery_requires_fresh_acknowledgement_for_exact_room(self):
        row = self.legacy_row()
        result, relay, _ = self.recovery([row], {"12": {"message_id": "f" * 64}})
        self.assertEqual(result, (True, None))
        relay.assert_called_once_with(str(self.conversation.mlai_channel_id), ["12"])
        self.assertEqual(row.metadata["destination_channel_id"], str(self.conversation.mlai_channel_id))
        self.assertEqual(row.metadata["destination_message_id"], "f" * 64)
        self.apply_receipt()
        self.assertTrue(self.ready())
        self.assertNotIn("publication", self.state.verified_ranges)

    def test_old_room_receipt_without_current_acknowledgement_never_publishes(self):
        row = self.legacy_row()
        result, _, _ = self.recovery([row], {})
        self.assertEqual(result, (False, None))
        row.save.assert_not_called()
        self.assertNotIn("presentation", self.state.verified_ranges)

    def test_receipt_recovery_restores_missing_source_activity_without_import_time(self):
        row = self.legacy_row()
        original_source_ts = row.source_message_id
        self.conversation.latest_synced_ts = ""
        self.assertEqual(self.recovery([row], {"12": {"message_id": "f" * 64}})[0], (True, None))
        self.assertEqual(self.conversation.latest_synced_ts, original_source_ts)
        self.apply_receipt()
        self.assertTrue(self.ready())

    def test_receipt_recovery_advances_bounded_pages_past_unknown_ids(self):
        rows = [self.legacy_row(pk=index) for index in range(100, 80, -1)]
        result, relay, queryset = self.recovery(rows, {}, before_id=101)
        self.assertEqual(result, (False, 81))
        self.assertEqual(len(relay.call_args.args[1]), 20)
        self.assertIn(((), {"pk__lt": 101}), [(call.args, call.kwargs) for call in queryset.filter.call_args_list])

    def test_receipt_recovery_cannot_cross_revoked_device(self):
        result, relay, _ = self.recovery([self.legacy_row()], {}, device_authorized=False)
        self.assertEqual(result, (False, None))
        relay.assert_not_called()

    def test_empty_limited_slack_probe_returns_limitation_instead_of_no_messages(self):
        row = SimpleNamespace(slack_conversation_id="DTEST", kind="im", source_archived=False, source_activity_ts="")
        with patch.object(inventory, "_call_slack_with_grant_authority", side_effect=[
            {"channel": {"id": "DTEST", "is_im": True}},
            {"ok": True, "messages": [], "is_limited": True},
        ]), self.assertRaises(inventory.InventoryError) as caught:
            inventory._validate_open_source(self.grant, object(), row)
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("inventory_source_limited", 409))

    def test_limited_scan_waits_for_a_pending_live_first_message(self):
        self.conversation.history_backfilled_at = self.now
        self.conversation._import_limited = True
        self.conversation._import_pending = False
        rows = self.conversation.deliveries
        rows.filter.return_value = rows
        rows.exclude.return_value = rows
        with patch.object(catalog, "catalog_conversations") as query:
            query.return_value.first.return_value = self.conversation
            rows.exists.return_value = True
            self.assertFalse(catalog.source_limited_without_pending(self.grant, "DTEST"))
            rows.exists.return_value = False
            self.assertTrue(catalog.source_limited_without_pending(self.grant, "DTEST"))
