"""Database-free checks for private mention delivery and history repair."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from integrations.services import slack_dm_mirror as mirror
from integrations.services.community_bridge.formatting import sanitize_slack_text
from integrations.services.slack_private_mentions import (
    private_mention_repair,
    render_private_slack_mentions,
)


PROFILES = {
    "UALICE": {"display_name": "Alex Morgan"},
    "UOWNER": {"display_name": "Dr Taylor"},
}
RAW = "Call <tel:0400123456|0400123456>. Thanks <@UALICE> and <@UOWNER>."
RENDERED = (
    "Call <tel:0400123456|0400123456>. Thanks @Alex\u00a0Morgan and @Dr\u00a0Taylor."
)


class PrivateSlackMentionTests(SimpleTestCase):
    def test_private_queue_keeps_ids_until_authorized_profiles_are_available(self):
        self.assertEqual(mirror._slack_message_text({"text": RAW}), RAW)
        self.assertEqual(render_private_slack_mentions(RAW, PROFILES), RENDERED)
        self.assertEqual(sanitize_slack_text("<@UALICE>"), "@user")
        self.assertEqual(
            sanitize_slack_text(
                "<@UALICE|old-name>", user_name_resolver=lambda _: "Alex"
            ),
            "@Alex",
        )

    def test_live_create_and_edit_keep_entities_and_record_queue_format(self):
        source = {"ts": "1750000000.000001", "user": "UALICE", "text": RAW}
        for event in (source, {"subtype": "message_changed", "message": source}):
            normalized = mirror._normalize_private_slack_event({}, event)
            self.assertEqual(normalized["text"], RAW)
            self.assertTrue(normalized["metadata"]["slack_entities_preserved"])

    def test_missing_profiles_preserve_identity_and_ignore_untrusted_alias(self):
        text = "<@UMISSING|Dr Taylor> and <@UALICE|old-name>"
        self.assertEqual(
            render_private_slack_mentions(text, PROFILES),
            "<@UMISSING|Dr Taylor> and @Alex\u00a0Morgan",
        )
        self.assertEqual(render_private_slack_mentions("@user", PROFILES), "@user")

    def test_code_literals_are_not_mentions(self):
        for text in ("`<@UALICE>`", "```\n<@UALICE>\n```", "```\n<@UALICE>"):
            self.assertEqual(render_private_slack_mentions(text, PROFILES), text)

    def test_repair_is_idempotent_and_preserves_original_edit_revision(self):
        message = {"ts": "1750000000.000001", "edited": {"ts": "1750000090.000002"}}
        args = dict(completed=True, metadata={"destination_message_id": "target"})
        repair = private_mention_repair(message, RAW, PROFILES, **args)
        self.assertEqual(repair, private_mention_repair(message, RAW, PROFILES, **args))
        self.assertEqual(repair[1], "1750000090.000002")
        self.assertNotEqual(
            repair, private_mention_repair(message, RAW + " edited", PROFILES, **args)
        )

    def test_repair_skips_new_pending_invisible_unresolved_and_code_messages(self):
        for completed, metadata, text in (
            (False, {"destination_message_id": "target"}, RAW),
            (
                True,
                {"destination_message_id": "target", "mention_format_version": 1},
                RAW,
            ),
            (True, {}, RAW),
            (
                True,
                {"destination_message_id": "target", "permanent_failure": True},
                RAW,
            ),
            (True, {"destination_message_id": "target"}, "<@UMISSING>"),
            (True, {"destination_message_id": "target"}, "`<@UALICE>`"),
        ):
            self.assertIsNone(
                private_mention_repair(
                    {"ts": "1750000000.000001"},
                    text,
                    PROFILES,
                    completed=completed,
                    metadata=metadata,
                )
            )

    def fixture(self, operation="create"):
        grant = SimpleNamespace(status="active", revoked_at=None, save=MagicMock())
        conversation = SimpleNamespace(
            pk=12,
            grant_id=3,
            grant=grant,
            status="live",
            mlai_channel_id="private",
            participant_profiles=PROFILES,
            participant_hash="participants",
            participant_buzz_pubkeys=["owner"],
            slack_workspace_id="TTEST",
            slack_conversation_id="GPRIVATE",
            latest_synced_ts="",
            save=MagicMock(),
        )
        delivery = SimpleNamespace(
            pk=42,
            conversation_id=12,
            conversation=conversation,
            source_platform="slack",
            operation=operation,
            source_message_id="1750000000.000001",
            source_author_id="UALICE",
            encrypted_text=RAW,
            metadata={
                "slack_entities_preserved": True,
                "event_ts": "1750000000.000001",
                "target_source_message_id": "1750000000.000001",
            },
            created_at=timezone.now(),
            save=MagicMock(),
        )
        return grant, conversation, delivery

    def test_live_and_threaded_or_edited_delivery_resolve_names_before_body_erasure(
        self,
    ):
        for operation in ("create", "edit"):
            _, _, delivery = self.fixture(operation)
            with patch.object(
                mirror, "_history_delivery_author_pubkey", return_value="owner"
            ), patch.object(
                mirror, "_supersede_stale_slack_mutation_locked", return_value=False
            ), patch.object(
                mirror, "_private_destination_message_id", return_value="target"
            ), patch.object(
                mirror, "_assert_private_delivery_authorized_locked"
            ) as authorized, patch.object(
                mirror.BuzzBridgeClient,
                "deliver_private",
                return_value={"message_id": "target"},
            ) as send:
                mirror._deliver_to_mlai(delivery)
            self.assertEqual(send.call_args.kwargs["text"], RENDERED)
            self.assertEqual(
                send.call_args.kwargs["source_message_id"], "1750000000.000001"
            )
            self.assertEqual(delivery.encrypted_text, "")
            self.assertEqual(delivery.metadata["mention_format_version"], 1)
            authorized.assert_called_once_with(delivery)

    def test_batch_delivery_resolves_names_and_preserves_authorization_checks(self):
        grant, conversation, delivery = self.fixture()
        with ExitStack() as stack:
            stack.enter_context(patch.object(mirror.transaction, "atomic"))
            grants = stack.enter_context(
                patch.object(mirror.SlackDmMirrorGrant, "objects")
            )
            conversations = stack.enter_context(
                patch.object(mirror.SlackDmMirrorConversation, "objects")
            )
            rows = stack.enter_context(
                patch.object(mirror.SlackDmMirrorDelivery, "objects")
            )
            grants.select_related.return_value.filter.return_value.first.return_value = (
                grant
            )
            grants.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = (
                grant
            )
            conversations.select_for_update.return_value.filter.return_value.first.return_value = (
                conversation
            )
            rows.select_for_update.return_value.filter.return_value = [delivery]
            stack.enter_context(
                patch.object(mirror, "_refresh_slack_grant_token_if_due")
            )
            stack.enter_context(
                patch.object(
                    mirror, "_history_delivery_author_pubkey", return_value="owner"
                )
            )
            authorized = stack.enter_context(
                patch.object(mirror, "_assert_private_delivery_authorized_locked")
            )
            send = stack.enter_context(
                patch.object(
                    mirror.BuzzBridgeClient,
                    "deliver_private_batch",
                    return_value=[{"message_id": "target"}],
                )
            )
            mirror._deliver_private_batch([delivery])
        self.assertEqual(send.call_args.args[0][0]["text"], RENDERED)
        self.assertEqual(delivery.encrypted_text, "")
        self.assertEqual(delivery.metadata["mention_format_version"], 1)
        self.assertEqual(authorized.call_count, 2)

    def test_history_refresh_queues_targeted_repair_without_recreating_message(self):
        _, conversation, _ = self.fixture()
        original = SimpleNamespace(
            status="completed", metadata={"destination_message_id": "target"}
        )
        with patch.object(
            mirror.SlackDmMirrorDelivery, "objects"
        ) as rows, patch.object(
            mirror, "_all_history_group_import", return_value=False
        ), patch.object(
            mirror, "_upsert_history_delivery", return_value=original
        ) as upsert:
            rows.select_for_update.return_value.filter.return_value.filter.return_value.order_by.return_value.first.return_value = (
                None
            )
            mirror._enqueue_history_message(
                conversation,
                {"ts": "1750000000.000001", "user": "UALICE", "text": RAW},
                scan_authority=SimpleNamespace(epoch="scan"),
                held_until=timezone.now(),
            )
        self.assertEqual(upsert.call_count, 2)
        repair = upsert.call_args.kwargs
        self.assertEqual(repair["operation"], "edit")
        self.assertEqual(
            repair["metadata"]["target_source_message_id"], "1750000000.000001"
        )
        self.assertEqual(repair["metadata"]["history_scan_epoch"], "scan")
        self.assertEqual(repair["metadata"]["participant_hash"], "participants")
        self.assertTrue(repair["source_message_id"].startswith("mention-format-v1:"))

    def test_legacy_pending_body_stays_eligible_for_history_repair(self):
        _, _, delivery = self.fixture()
        delivery.metadata.pop("slack_entities_preserved")
        delivery.encrypted_text = "Thanks @user"
        with patch.object(
            mirror, "_history_delivery_author_pubkey", return_value="owner"
        ), patch.object(
            mirror, "_supersede_stale_slack_mutation_locked", return_value=False
        ), patch.object(
            mirror, "_assert_private_delivery_authorized_locked"
        ), patch.object(
            mirror.BuzzBridgeClient,
            "deliver_private",
            return_value={"message_id": "target"},
        ):
            mirror._deliver_to_mlai(delivery)
        self.assertEqual(delivery.metadata["mention_format_version"], 0)
        self.assertIsNotNone(
            private_mention_repair(
                {"ts": "1750000000.000001"},
                RAW,
                PROFILES,
                completed=True,
                metadata=delivery.metadata,
            )
        )

    def test_newer_slack_edit_supersedes_delayed_name_repair(self):
        _, _, delivery = self.fixture("edit")
        newer = SimpleNamespace(
            pk=7,
            operation="edit",
            created_at=timezone.now(),
            metadata={
                "event_ts": "1750000900.000001",
                "target_source_message_id": "1750000000.000001",
            },
        )
        with patch.object(
            mirror.SlackDmMirrorDelivery, "objects"
        ) as rows, patch.object(
            mirror, "_complete_superseded_dependency_locked"
        ) as complete:
            rows.filter.return_value.exclude.return_value = [newer]
            self.assertTrue(mirror._supersede_stale_slack_mutation_locked(delivery))
        complete.assert_called_once()

    def test_revoked_conversation_cannot_send_mention_repair(self):
        grant, _, delivery = self.fixture("edit")
        grant.revoked_at = timezone.now()
        with patch.object(mirror.BuzzBridgeClient, "deliver_private") as send:
            with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
                mirror._deliver_to_mlai(delivery)
        send.assert_not_called()
