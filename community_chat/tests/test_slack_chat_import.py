"""Synthetic database regressions for the expanded Slack import."""

import uuid
from unittest.mock import MagicMock, patch

from django.utils import timezone
from rest_framework.test import APITestCase

from community_chat.tests.test_slack_dm_mirror import SlackDmMirrorOwnerTests
from integrations.models import SlackDmMirrorConversation
from integrations.services import slack_dm_mirror as mirror
from integrations.services.slack_chat_catalog import (
    PRIVATE_CHANNEL_CONSENT,
    PRIVATE_CHANNEL_SCOPES,
    conversation_kind,
)


class SlackChatImportTests(APITestCase):
    setUp = SlackDmMirrorOwnerTests.setUp

    def enable_private_channels(self):
        self.first_connection.scopes += list(PRIVATE_CHANNEL_SCOPES)
        self.first_connection.save(update_fields=("scopes", "updated_at"))
        return mirror.activate_connection(
            self.first_connection, include_private_channels=True
        )

    def test_new_connection_defaults_to_thirty_days(self):
        grant = self.enable_private_channels()
        self.assertEqual(grant.history_days, 30)
        self.assertEqual(grant.consent_version, PRIVATE_CHANNEL_CONSENT)
        self.assertIsNone(grant.last_discovery_at)

    @patch.object(mirror.BuzzBridgeClient, "unregister_private_conversation")
    def test_upgrading_live_connection_keeps_existing_registration_generation(
        self, unregister
    ):
        grant = mirror.activate_connection(self.first_connection)
        epoch = grant.consented_at
        self.first_connection.scopes += list(PRIVATE_CHANNEL_SCOPES)
        self.first_connection.save(update_fields=("scopes", "updated_at"))
        upgraded = mirror.activate_connection(
            self.first_connection, history_days=30, include_private_channels=True
        )
        self.assertEqual(upgraded.pk, grant.pk)
        self.assertEqual(upgraded.history_days, 30)
        self.assertEqual(upgraded.consented_at, epoch)
        unregister.assert_not_called()

    @patch.object(mirror.BuzzBridgeClient, "provision_private_conversation")
    @patch.object(mirror, "WebClient")
    def test_large_private_channel_is_named_and_visible_before_history_finishes(
        self, client_type, provision
    ):
        grant = self.enable_private_channels()
        members = ["UONE", "UTWO"] + [f"U{i}" for i in range(25)]
        client = MagicMock()
        client.users_conversations.return_value = {
            "channels": [{"id": "CPRIVATE", "name": "organisers", "is_private": True}],
            "response_metadata": {},
        }
        client.conversations_members.return_value = {
            "members": members,
            "response_metadata": {},
        }
        client.users_list.return_value = {
            "members": [
                {"id": member, "name": member, "profile": {}} for member in members
            ],
            "response_metadata": {},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {"id": user, "name": user, "profile": {}}
        }
        client.conversations_history.return_value = {
            "messages": [
                {
                    "ts": f"{int(timezone.now().timestamp()) - 60}.000100",
                    "user": "UTWO",
                    "text": "Private planning",
                }
            ],
            "response_metadata": {},
        }
        client_type.return_value = client
        provision.side_effect = lambda pubkeys, **kwargs: {
            "channel_id": str(uuid.uuid4()),
            "participant_pubkeys": pubkeys,
        }

        mirror.discover_conversations(grant)
        conversation = SlackDmMirrorConversation.objects.select_related(
            "grant__connection"
        ).get(grant=grant)
        self.assertEqual(conversation_kind(conversation), "private_channel")
        self.assertEqual(provision.call_args.kwargs["conversation_name"], "organisers")
        self.assertEqual(
            provision.call_args.kwargs["callback_author_pubkeys"], ["1" * 64]
        )
        self.assertEqual(len(conversation.participant_buzz_pubkeys), 2)
        self.assertNotIn("2" * 64, conversation.participant_buzz_pubkeys)
        self.assertIsNone(conversation.history_backfilled_at)
        payload = mirror.status_payload(self.first, authenticated_public_key="1" * 64)
        self.assertEqual(
            payload["channel_catalog"],
            [
                {
                    "channel_id": str(conversation.mlai_channel_id),
                    "kind": "private_channel",
                    "last_message_at": conversation.last_message_at.isoformat(),
                    "source_archived": False,
                }
            ],
        )
        self.assertEqual(
            mirror.status_payload(self.first, authenticated_public_key="2" * 64)[
                "channel_catalog"
            ],
            [],
        )
        self.assertEqual(mirror.process_due_history_backfills(), 1)
        self.assertTrue(
            conversation.deliveries.filter(source_author_id="UTWO").exists()
        )
        self.assertIn("oldest", client.conversations_history.call_args.kwargs)

    def test_private_channel_events_wait_for_explicit_consent(self):
        grant = mirror.activate_connection(self.first_connection)
        result = mirror.ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "event_id": "EvPrivate",
                "authorizations": [{"user_id": "UONE"}],
                "event": {
                    "type": "message",
                    "channel_type": "group",
                    "channel": "CPRIVATE",
                    "user": "UTWO",
                    "text": "Private planning",
                    "ts": "1788740100.000100",
                },
            }
        )
        self.assertEqual(result["staged"], 0)
        grant.connection.refresh_from_db()
        self.assertNotIn(
            mirror.PENDING_EVENT_CHECKPOINT_KEY, grant.connection.sync_cursor
        )
