import uuid
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from slack_sdk.errors import SlackApiError

from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    DeviceBindingStatus,
)
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDeletionRequest,
    CommunityBridgeDeletionRequestStatus,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.external_connectors import (
    build_community_chat_slack_authorization_url,
)


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au",
    SLACK_CLIENT_ID="123.456",
    SLACK_CLIENT_SECRET="slack-client-secret",
    SLACK_OAUTH_REDIRECT_URI="https://api.mlai.au/integrations/callback/slack",
    SLACK_OAUTH_USER_SCOPES=[
        "channels:read",
        "channels:history",
        "groups:read",
        "groups:history",
        "team:read",
        "users:read",
        "chat:write",
    ],
)
class CommunityChatSlackDeletionTests(TestCase):
    def setUp(self):
        self.public_key = "9" * 64
        self.user = get_user_model().objects.create_user(
            email="slack-delete@example.com",
            first_name="Slack",
            last_name="Author",
            slack_id="U123",
        )
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Chrome",
            public_key=self.public_key,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.credentials = issue_account_session(self.user, challenge)
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            installation_id=challenge.installation_id,
            client_id=challenge.client_id,
            platform=challenge.platform,
            name=challenge.device_name,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T-MLAI",
            slack_channel_id="C-GENERAL",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            destination_channel_name="general",
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="T-MLAI",
            slack_user_id="U123",
            buzz_pubkey=self.public_key,
            display_name="Slack Author",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED
            ),
            verification_reference="test-proof",
            verified_at=timezone.now(),
        )
        self.buzz_event_id = "1" * 64
        self.link = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.1000",
            source_author_id="U123",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=self.buzz_event_id,
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            access_token="xoxp-user-token",
            scopes=["chat:write", "channels:read"],
            external_account_id="T-MLAI",
            account_label="MLAI",
            status=ExternalServiceConnectionStatus.CONNECTED,
            provider_metadata={
                "token_source": "authed_user",
                "authed_user": {"id": "U123"},
            },
        )
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}"
        )
        self.url = reverse("community_chat_delete_slack_origin")

    @patch(
        "integrations.services.community_bridge.deletion.SlackBridgeClient.delete_message_as_user",
        return_value={
            "ok": True,
            "channel": "C-GENERAL",
            "message_id": "1710000000.1000",
        },
    )
    def test_owned_message_uses_user_token_and_is_idempotent(self, mock_delete):
        idempotency_key = uuid.uuid4()
        payload = {
            "buzz_event_id": self.buzz_event_id,
            "idempotency_key": str(idempotency_key),
        }

        first = self.client.post(self.url, payload, format="json")
        second = self.client.post(self.url, payload, format="json")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.data["status"], "succeeded")
        self.assertEqual(first.data["request_id"], second.data["request_id"])
        mock_delete.assert_called_once_with(
            access_token="xoxp-user-token",
            channel_id="C-GENERAL",
            message_id="1710000000.1000",
        )
        self.link.refresh_from_db()
        self.assertIsNone(self.link.destination_deleted_at)
        request_row = CommunityBridgeDeletionRequest.objects.get()
        self.assertEqual(
            request_row.status,
            CommunityBridgeDeletionRequestStatus.SUCCEEDED,
        )

    @patch(
        "integrations.services.community_bridge.deletion.SlackBridgeClient.delete_message_as_user",
        side_effect=SlackApiError(
            "Slack could not find the message",
            {"ok": False, "error": "message_not_found"},
        ),
    )
    def test_missing_slack_message_leaves_buzz_deletion_for_bridge_repair(
        self, mock_delete
    ):
        response = self.client.post(
            self.url,
            {
                "buzz_event_id": self.buzz_event_id,
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "already_deleted")
        mock_delete.assert_called_once()
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.source_deleted_at)
        self.assertIsNone(self.link.destination_deleted_at)

    @patch(
        "integrations.services.community_bridge.deletion.SlackBridgeClient.delete_message_as_user"
    )
    def test_missing_chat_write_scope_requires_reauthorization(self, mock_delete):
        self.connection.scopes = ["channels:read"]
        self.connection.save(update_fields=["scopes", "updated_at"])

        response = self.client.post(
            self.url,
            {
                "buzz_event_id": self.buzz_event_id,
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "slack_reauthorization_required")
        self.assertIn("slack.com/oauth/v2/authorize", response.data["connect_url"])
        self.assertIn("chat%3Awrite", response.data["connect_url"])
        mock_delete.assert_not_called()

    @patch(
        "integrations.services.community_bridge.deletion.SlackBridgeClient.delete_message_as_user"
    )
    def test_another_account_cannot_delete_the_message(self, mock_delete):
        other = get_user_model().objects.create_user(email="other@example.com")
        other_challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=other,
            email_digest="c" * 64,
            code_digest="d" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Firefox",
            public_key="8" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        other_credentials = issue_account_session(other, other_challenge)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {other_credentials.access_token}"
        )

        response = client.post(
            self.url,
            {
                "buzz_event_id": self.buzz_event_id,
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"], "not_message_owner")
        mock_delete.assert_not_called()

    @patch(
        "integrations.services.external_connectors._exchange_slack_code",
        return_value={
            "ok": True,
            "team": {"id": "T-MLAI", "name": "MLAI"},
            "authed_user": {
                "id": "U123",
                "access_token": "test-refreshed-user-token",
                "scope": "channels:read,chat:write",
                "token_type": "user",
            },
        },
    )
    def test_chat_oauth_state_restores_user_without_main_site_session(
        self, mock_exchange
    ):
        authorization_url = build_community_chat_slack_authorization_url(self.user)
        state = parse_qs(urlparse(authorization_url).query)["state"][0]

        response = APIClient().get(
            reverse("connector_callback", args=["slack"]),
            {"code": "slack-oauth-code", "state": state},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "https://chat.mlai.au/?slack_connected=true",
        )
        mock_exchange.assert_called_once_with("slack-oauth-code")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.access_token, "test-refreshed-user-token")
        self.assertIn("chat:write", self.connection.scopes)
