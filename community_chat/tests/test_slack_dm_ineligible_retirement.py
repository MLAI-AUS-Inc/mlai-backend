from __future__ import annotations

import hashlib
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror
from integrations.services import slack_dm_registration_ledger as registration_ledger


SCOPES = [
    "im:read",
    "im:history",
    "im:write",
    "chat:write",
    "users:read",
    "reactions:read",
    "reactions:write",
    "files:read",
    "mpim:read",
    "mpim:history",
    "mpim:write",
]


class SlackDmIneligibleRetirementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="retirement@example.com"
        )
        self.owner_key = "1" * 64
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            status=ExternalServiceConnectionStatus.CONNECTED,
            access_token="xoxp-retirement",
            scopes=SCOPES,
            external_account_id="TRETIRE",
            provider_metadata={
                "team": {"id": "TRETIRE", "name": "Retirement"},
                "authed_user": {
                    "id": "UOWNER",
                    "scope": ",".join(SCOPES),
                },
            },
        )
        self.grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=self.connection,
            slack_workspace_id="TRETIRE",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="TRETIRE",
            slack_user_id="UOWNER",
            buzz_pubkey=self.owner_key,
            display_name="Owner",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="retirement-test",
            verified_at=timezone.now(),
        )

    def _create_live_conversation(
        self,
        slack_conversation_id: str,
        *,
        participant_slack_ids: list[str] | None = None,
    ) -> tuple[SlackDmMirrorConversation, uuid.UUID]:
        participant_slack_ids = participant_slack_ids or ["UOWNER", "UOTHER"]
        shadow_key = hashlib.sha256(slack_conversation_id.encode("utf-8")).hexdigest()
        participant_pubkeys = sorted([self.owner_key, shadow_key])
        participant_hash = hashlib.sha256(
            b"".join(bytes.fromhex(value) for value in participant_pubkeys)
        ).hexdigest()
        channel_id = uuid.uuid4()
        identity_map = {"UOWNER": self.owner_key}
        for participant_id in participant_slack_ids:
            if participant_id != "UOWNER":
                identity_map[participant_id] = shadow_key
        conversation = SlackDmMirrorConversation.objects.create(
            grant=self.grant,
            slack_workspace_id="TRETIRE",
            slack_conversation_id=slack_conversation_id,
            participant_slack_ids=participant_slack_ids,
            participant_buzz_pubkeys=participant_pubkeys,
            participant_identity_map=identity_map,
            participant_profiles={
                participant_id: {"display_name": participant_id}
                for participant_id in participant_slack_ids
            },
            participant_hash=participant_hash,
            mlai_channel_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
            oldest_synced_ts="1700000000.000001",
            latest_synced_ts="1700000100.000001",
            history_backfilled_at=timezone.now(),
            last_synced_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1700000000.000001",
            source_author_id="UOTHER",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="private pending body",
            available_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="a" * 64,
            source_author_id=self.owner_key,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="private completed body",
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=slack_dm_mirror.HISTORY_MAIN_STATE_ID,
            source_author_id="",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="private history cursor",
            available_at=timezone.now(),
        )
        return conversation, channel_id

    def _assert_retired(
        self,
        conversation: SlackDmMirrorConversation,
        *,
        reason: str,
    ) -> None:
        conversation.refresh_from_db()
        self.assertEqual(
            conversation.status,
            SlackDmMirrorConversationStatus.PAUSED,
        )
        self.assertIsNone(conversation.mlai_channel_id)
        self.assertEqual(conversation.participant_slack_ids, [])
        self.assertEqual(conversation.participant_buzz_pubkeys, [])
        self.assertEqual(conversation.participant_identity_map, {})
        self.assertEqual(conversation.participant_profiles, {})
        self.assertEqual(conversation.participant_hash, "")
        self.assertEqual(conversation.oldest_synced_ts, "")
        self.assertEqual(conversation.latest_synced_ts, "")
        self.assertIsNone(conversation.history_backfilled_at)
        self.assertIsNone(conversation.last_synced_at)
        self.assertEqual(conversation.last_error, reason)

        private_rows = conversation.deliveries.exclude(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX
        )
        self.assertFalse(
            private_rows.filter(
                source_message_id=slack_dm_mirror.HISTORY_MAIN_STATE_ID
            ).exists()
        )
        self.assertEqual(private_rows.count(), 2)
        for delivery in private_rows:
            self.assertEqual(delivery.encrypted_text, "")
            self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
            self.assertIsNone(delivery.completed_at)
            self.assertEqual(delivery.last_error, reason)

        registration = conversation.deliveries.get(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX
        )
        self.assertEqual(registration.encrypted_text, "")
        self.assertEqual(
            registration_ledger.registration_state(registration),
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_discovery_retires_live_conversation_that_becomes_slack_connect(
        self,
        web_client,
        unregister_private_conversation,
    ):
        conversation, channel_id = self._create_live_conversation("DCONNECT")
        web_client.return_value.conversations_list.return_value = {
            "channels": [
                {
                    "id": "DCONNECT",
                    "user": "UOTHER",
                    "is_ext_shared": True,
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
        self.assertEqual(slack_dm_mirror.discover_conversations(self.grant), 0)

        self._assert_retired(
            conversation,
            reason=slack_dm_mirror.SLACK_CONNECT_INELIGIBLE_REASON,
        )
        unregister_private_conversation.assert_called_once_with(str(channel_id))

        # A later ordinary event cannot enqueue ciphertext against the retired
        # row while discovery decides whether the Slack DM became eligible.
        result = slack_dm_mirror.ingest_slack_dm_event(
            {
                "team_id": "TRETIRE",
                "event": {
                    "type": "message",
                    "channel": "DCONNECT",
                    "channel_type": "im",
                    "ts": "1700000200.000001",
                    "user": "UOTHER",
                    "text": "must not be retained",
                },
            }
        )
        self.assertEqual(result, {"status": "discovery_queued"})
        self.assertFalse(
            conversation.deliveries.filter(
                source_message_id="1700000200.000001"
            ).exists()
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_external_shared_event_uses_the_same_retirement_boundary(
        self,
        unregister_private_conversation,
    ):
        conversation, channel_id = self._create_live_conversation("DEVENT")

        result = slack_dm_mirror.ingest_slack_dm_event(
            {
                "team_id": "TRETIRE",
                "event": {
                    "type": "message",
                    "channel": "DEVENT",
                    "is_ext_shared_channel": True,
                },
            }
        )

        self.assertEqual(result, {"status": "ignored"})
        self._assert_retired(
            conversation,
            reason=slack_dm_mirror.SLACK_CONNECT_INELIGIBLE_REASON,
        )
        unregister_private_conversation.assert_called_once_with(str(channel_id))

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_discovery_retires_owner_removed_and_oversized_group_dms(
        self,
        web_client,
        unregister_private_conversation,
    ):
        removed, removed_channel_id = self._create_live_conversation(
            "GREMOVED",
            participant_slack_ids=["UOWNER", "UONE"],
        )
        oversized, oversized_channel_id = self._create_live_conversation(
            "GOVERSIZED",
            participant_slack_ids=["UOWNER", "UONE"],
        )
        web_client.return_value.conversations_list.return_value = {
            "channels": [
                {
                    "id": "GREMOVED",
                    "is_mpim": True,
                    "members": ["UONE", "UTWO"],
                },
                {
                    "id": "GOVERSIZED",
                    "is_mpim": True,
                    "members": ["UOWNER", *[f"U{index}" for index in range(9)]],
                },
            ],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value.conversations_members.side_effect = [
            {
                "members": ["UONE", "UTWO"],
                "response_metadata": {"next_cursor": ""},
            },
            {
                "members": ["UOWNER", *[f"U{index}" for index in range(9)]],
                "response_metadata": {"next_cursor": ""},
            },
        ]

        self.assertEqual(slack_dm_mirror.discover_conversations(self.grant), 0)

        for conversation in (removed, oversized):
            self._assert_retired(
                conversation,
                reason=slack_dm_mirror.SLACK_PARTICIPANTS_INELIGIBLE_REASON,
            )
        self.assertCountEqual(
            [call.args[0] for call in unregister_private_conversation.call_args_list],
            [str(removed_channel_id), str(oversized_channel_id)],
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_complete_discovery_retires_archived_and_absent_live_mirrors(
        self,
        web_client,
        unregister_private_conversation,
    ):
        archived, archived_channel_id = self._create_live_conversation("DARCHIVED")
        absent, absent_channel_id = self._create_live_conversation("DABSENT")
        web_client.return_value.conversations_list.return_value = {
            "channels": [
                {
                    "id": "DARCHIVED",
                    "user": "UOTHER",
                    "is_archived": True,
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(slack_dm_mirror.discover_conversations(self.grant), 0)

        for conversation in (archived, absent):
            self._assert_retired(
                conversation,
                reason=slack_dm_mirror.SLACK_CONVERSATION_UNAVAILABLE_REASON,
            )
        self.assertCountEqual(
            [call.args[0] for call in unregister_private_conversation.call_args_list],
            [str(archived_channel_id), str(absent_channel_id)],
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_complete_discovery_retires_absent_failed_provisioning_row(
        self,
        web_client,
        unregister_private_conversation,
    ):
        conversation, channel_id = self._create_live_conversation("DFAILED")
        conversation.status = SlackDmMirrorConversationStatus.ERROR
        conversation.last_error = "provisioning timed out"
        conversation.save(update_fields=("status", "last_error", "updated_at"))
        web_client.return_value.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }

        self.assertEqual(slack_dm_mirror.discover_conversations(self.grant), 0)

        self._assert_retired(
            conversation,
            reason=slack_dm_mirror.SLACK_CONVERSATION_UNAVAILABLE_REASON,
        )
        unregister_private_conversation.assert_called_once_with(str(channel_id))
