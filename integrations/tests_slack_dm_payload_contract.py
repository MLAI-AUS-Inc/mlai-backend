"""Exercise the real private-delivery payload without a database or migrations.

Run with Python's unittest runner. Persistence and provider I/O are mocked;
opening any database connection fails the test.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class SlackPrivatePayloadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
        import django

        django.setup()
        from integrations.services import slack_dm_mirror

        cls.service = slack_dm_mirror

    def setUp(self):
        self.start_patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("payload tests must never open a database"),
        )
        self.start_patch(
            f"{self.service.__name__}._history_delivery_author_pubkey",
            return_value="a" * 64,
        )
        self.start_patch(
            f"{self.service.__name__}._supersede_stale_slack_mutation_locked",
            return_value=False,
        )
        self.start_patch(
            f"{self.service.__name__}._private_destination_message_id",
            return_value="b" * 64,
        )
        self.start_patch(
            f"{self.service.__name__}._private_destination_operation_message_id",
            return_value="c" * 64,
        )
        self.authority = self.start_patch(
            f"{self.service.__name__}._assert_private_delivery_authorized_locked",
        )
        self.adapter = self.start_patch(
            f"{self.service.__name__}.BuzzBridgeClient.deliver_private",
            side_effect=self.accept_adapter_payload,
        )

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def accept_adapter_payload(self, **payload):
        # This is the bridge adapter's Slack source-reference contract. Queue
        # deduplication IDs such as slack-event:... are not Slack timestamps.
        self.assertRegex(payload["source_message_id"], r"^\d{10}\.\d{6}$")
        self.assertEqual(payload["delivery_id"], "42")
        return {"message_id": "d" * 64}

    def delivery(self, operation, source_id, *, target="1788650000.000123"):
        grant = SimpleNamespace(status="active", revoked_at=None, save=Mock())
        conversation = SimpleNamespace(
            status="live", mlai_channel_id="00000000-0000-4000-8000-000000000042",
            grant=grant, participant_hash="current-audience",
            participant_buzz_pubkeys=["a" * 64], participant_profiles={},
            slack_workspace_id="TTEST", slack_conversation_id="DTEST",
            latest_synced_ts="", save=Mock(),
        )
        metadata = {
            "participant_hash": "current-audience",
            "event_ts": "1788650001.000456",
            "reaction_object_id": "reaction:" + "f" * 64,
        }
        if target is not None:
            metadata["target_source_message_id"] = target
        return SimpleNamespace(
            pk=42, conversation=conversation, source_platform="slack",
            source_message_id=source_id, source_author_id="UTEST",
            operation=operation, metadata=metadata, encrypted_text="updated",
            save=Mock(),
        )

    def test_mutations_send_slack_reference_and_preserve_queue_identity(self):
        for operation in ("edit", "delete", "reaction_add", "reaction_remove"):
            with self.subTest(operation=operation):
                prefix = "reaction:" if operation.startswith("reaction") else "slack-event:"
                source_id = prefix + "e" * 64
                delivery = self.delivery(operation, source_id)
                self.service._deliver_to_mlai(delivery)
                self.assertEqual(self.adapter.call_args.kwargs["source_message_id"], "1788650000.000123")
                self.assertEqual(delivery.source_message_id, source_id)
                self.assertEqual(delivery.status, "completed")
                self.assertEqual(delivery.encrypted_text, "")
                self.authority.assert_called_with(delivery)

    def test_create_keeps_its_own_timestamp(self):
        delivery = self.delivery("create", "1788650002.000789")
        self.service._deliver_to_mlai(delivery)
        self.assertEqual(self.adapter.call_args.kwargs["source_message_id"], "1788650002.000789")

    def test_legacy_mutation_with_timestamp_queue_id_still_works(self):
        delivery = self.delivery("edit", "1788650002.000789", target=None)
        self.service._deliver_to_mlai(delivery)
        self.assertEqual(self.adapter.call_args.kwargs["source_message_id"], "1788650002.000789")

    def test_changed_audience_does_not_reach_adapter(self):
        delivery = self.delivery("edit", "slack-event:" + "e" * 64)
        delivery.metadata["participant_hash"] = "old-audience"
        with self.assertRaises(self.service.SlackDmMirrorAuthorizationError):
            self.service._deliver_to_mlai(delivery)
        self.adapter.assert_not_called()

    def test_revoked_grant_does_not_reach_adapter(self):
        delivery = self.delivery("edit", "slack-event:" + "e" * 64)
        delivery.conversation.grant.status = "revoked"
        with self.assertRaises(self.service.SlackDmMirrorAuthorizationError):
            self.service._deliver_to_mlai(delivery)
        self.adapter.assert_not_called()
