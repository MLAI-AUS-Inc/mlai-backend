import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    DeviceBindingStatus,
)
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
)
from integrations.services.slack_dm_mirror import (
    activate_connection,
    ingest_slack_dm_event,
    process_ready_deliveries,
)


SCOPES = ["im:read", "im:history", "im:write", "chat:write", "users:read"]
OAUTH_SCOPES = SCOPES + [
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "team:read",
]


def _slack_connection(user, slack_user_id):
    return ExternalServiceConnection.objects.create(
        user=user,
        provider=ExternalServiceProvider.SLACK,
        access_token=f"xoxp-{slack_user_id}",
        scopes=SCOPES,
        external_account_id="TMLAI",
        account_label="MLAI",
        provider_metadata={
            "team": {"id": "TMLAI", "name": "MLAI"},
            "authed_user": {"id": slack_user_id, "scope": ",".join(SCOPES)},
        },
    )


class SlackDmMirrorApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="slack-link@example.com")
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Chrome",
            public_key="1" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        self.url = reverse("community_chat_slack")

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
    def test_link_returns_provider_bound_top_level_oauth_url_and_privacy_copy(self):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["authorization_url"].startswith("http"))
        self.assertIn("/integrations/connect/slack?", response.data["authorization_url"])
        self.assertIn("slack-dm-mirror-v2-owner", response.data["consent"]["version"])
        self.assertFalse(response.data["privacy"]["requires_both_participants"])
        self.assertTrue(response.data["privacy"]["owner_controlled"])
        self.assertFalse(response.data["privacy"]["included_in_roo"])

    @override_settings(
        COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au",
        SLACK_CLIENT_ID="client-id",
        SLACK_CLIENT_SECRET="client-secret",
        SLACK_OAUTH_REDIRECT_URI="https://api.mlai.au/integrations/callback/slack",
        SLACK_OAUTH_USER_SCOPES=OAUTH_SCOPES,
    )
    def test_link_ticket_survives_top_level_navigation_and_requests_user_dm_scopes(self):
        link_response = self.client.post(self.url, {}, format="json")
        link = urlparse(link_response.data["authorization_url"])

        self.client.credentials()
        oauth_response = self.client.get(f"{link.path}?{link.query}")

        self.assertEqual(oauth_response.status_code, 302)
        slack_url = urlparse(oauth_response["Location"])
        self.assertEqual(slack_url.netloc, "slack.com")
        scopes = set(parse_qs(slack_url.query)["user_scope"][0].split(","))
        self.assertTrue(set(SCOPES).issubset(scopes))

    @override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
    def test_existing_slack_connection_without_dm_scopes_is_reauthorized(self):
        connection = _slack_connection(self.user, "UREAUTH")
        connection.scopes = ["channels:read"]
        connection.save(update_fields=("scopes", "updated_at"))

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["needs_reauthorization"])
        self.assertIn("/integrations/connect/slack?", response.data["authorization_url"])

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_disconnect_revokes_and_clears_the_local_slack_token(self, web_client):
        connection = _slack_connection(self.user, "UREVOKE")
        SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UREVOKE",
            consented_at=timezone.now(),
        )

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)
        web_client.return_value.auth_revoke.assert_called_once_with()
        connection.refresh_from_db()
        self.assertEqual(connection.status, "disconnected")
        self.assertEqual(connection.access_token, "")


class SlackDmMirrorOwnerTests(APITestCase):
    def setUp(self):
        self.first = get_user_model().objects.create_user(email="first@example.com")
        self.second = get_user_model().objects.create_user(email="second@example.com")
        for user, public_key in ((self.first, "1" * 64), (self.second, "2" * 64)):
            CommunityChatDevice.objects.create(
                user=user,
                public_key=public_key,
                status=DeviceBindingStatus.VERIFIED,
                verified_at=timezone.now(),
            )
        self.first_connection = _slack_connection(self.first, "UONE")
        self.second_connection = _slack_connection(self.second, "UTWO")

    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation")
    @patch("integrations.services.slack_dm_mirror.BuzzBridgeClient.deliver_private")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_one_link_provisions_owner_only_mirror_and_backfills_in_timestamp_order(
        self,
        web_client,
        deliver_private,
        provision,
    ):
        first_client = MagicMock()
        first_client.conversations_list.return_value = {
            "channels": [{"id": "DONE", "user": "UTWO"}],
            "response_metadata": {"next_cursor": ""},
        }
        first_client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "name": user.lower(),
                "profile": {
                    "display_name": "First person" if user == "UONE" else "Second person",
                    "image_192": f"https://avatars.slack-edge.com/{user}.png",
                },
            }
        }
        first_client.conversations_history.return_value = {
            "messages": [
                {"ts": "1787900001.000200", "user": "UTWO", "text": "private second"},
                {"ts": "1787900000.000100", "user": "UONE", "text": "private first"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.return_value = first_client
        channel_id = str(uuid.uuid4())
        provision.side_effect = lambda pubkeys, **_: {
            "channel_id": channel_id,
            "participant_pubkeys": pubkeys,
        }

        activate_connection(self.first_connection)
        conversation = SlackDmMirrorConversation.objects.get(slack_conversation_id="DONE")
        self.assertEqual(conversation.status, SlackDmMirrorConversationStatus.LIVE)
        self.assertIsNotNone(conversation.mlai_channel_id)
        self.assertEqual(conversation.grant.slack_user_id, "UONE")
        identity_map = conversation.participant_identity_map
        self.assertEqual(identity_map["UONE"], "1" * 64)
        self.assertEqual(len(identity_map["UTWO"]), 64)
        self.assertNotEqual(identity_map["UTWO"], "2" * 64)
        self.assertEqual(
            conversation.participant_profiles["UTWO"]["display_name"],
            "Second person",
        )
        queued = list(SlackDmMirrorDelivery.objects.filter(conversation=conversation).order_by("id"))
        self.assertEqual([item.encrypted_text for item in queued], ["private first", "private second"])
        provision.assert_called_once_with(
            conversation.participant_buzz_pubkeys,
            conversation_name="Second person",
        )

        self.assertEqual(process_ready_deliveries(limit=10), 2)
        self.assertEqual(process_ready_deliveries(limit=10), 0)
        delivered_times = [
            call.kwargs["created_at"] for call in deliver_private.call_args_list
        ]
        self.assertEqual(delivered_times, [1787900000, 1787900001])
        self.assertEqual(
            [call.kwargs["source_author_display_name"] for call in deliver_private.call_args_list],
            ["First person", "Second person"],
        )
        self.assertEqual(
            deliver_private.call_args_list[1].kwargs["source_author_avatar_url"],
            "https://avatars.slack-edge.com/UTWO.png",
        )
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                encrypted_text__gt="",
            ).exists()
        )

    def test_live_slack_event_fans_out_to_each_independent_owner_mirror(self):
        first_grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
        )
        second_grant = SlackDmMirrorGrant.objects.create(
            user=self.second,
            connection=self.second_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UTWO",
            consented_at=timezone.now(),
        )
        for grant, identity_map in (
            (first_grant, {"UONE": "1" * 64, "UTWO": "3" * 64}),
            (second_grant, {"UONE": "4" * 64, "UTWO": "2" * 64}),
        ):
            SlackDmMirrorConversation.objects.create(
                grant=grant,
                slack_workspace_id="TMLAI",
                slack_conversation_id="DONE",
                participant_slack_ids=["UONE", "UTWO"],
                participant_buzz_pubkeys=sorted(identity_map.values()),
                participant_identity_map=identity_map,
                mlai_channel_id=uuid.uuid4(),
                status=SlackDmMirrorConversationStatus.LIVE,
            )

        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "event": {
                    "channel": "DONE",
                    "ts": "1787900100.000100",
                    "user": "UONE",
                    "text": "visible in each owner's private copy",
                },
            }
        )

        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(len(result["delivery_ids"]), 2)
        self.assertEqual(SlackDmMirrorDelivery.objects.count(), 2)

    def test_unknown_dm_event_queues_owner_grants_for_immediate_rediscovery(self):
        grant = SlackDmMirrorGrant.objects.create(
            user=self.first,
            connection=self.first_connection,
            slack_workspace_id="TMLAI",
            slack_user_id="UONE",
            consented_at=timezone.now(),
            last_discovery_at=timezone.now(),
        )

        result = ingest_slack_dm_event(
            {
                "team_id": "TMLAI",
                "event": {
                    "channel": "DNEW",
                    "ts": "1787900200.000100",
                    "user": "UTWO",
                    "text": "first message in a newly opened DM",
                },
            }
        )

        self.assertEqual(result["status"], "discovery_queued")
        grant.refresh_from_db()
        self.assertIsNone(grant.last_discovery_at)
