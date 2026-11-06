from datetime import timedelta
from unittest.mock import patch
import uuid

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.link_previews import LinkPreviewError, _validated_public_url
from community_chat.models import CommunityChatEmailCodeChallenge
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorGrant, SlackChannelMembership,
)


class _FakeResponse:
    def __init__(self, *, body, content_type="text/html; charset=utf-8"):
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.is_redirect = False
        self.is_permanent_redirect = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.body

    def close(self):
        return None


def _public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(SLACK_BRIDGE_BOT_TOKEN="xoxb-test", MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TMLAI", COMMUNITY_CHAT_SIGNUP_ENABLED=False)
class CommunityChatLinkPreviewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="link-preview@example.com",
            first_name="Link",
            last_name="Preview",
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
            public_key=_public_key(71),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}"
        )

    def member(self):
        connection = ExternalServiceConnection.objects.create(user=self.user, provider="slack", status="connected",
            access_token="xoxp-owner", scopes=["files:read", "channels:read", "channels:history", "im:read"],
            external_account_id="TMLAI", provider_metadata={"team": {"id": "TMLAI"}, "authed_user": {"id": "UOWNER"}})
        grant = SlackDmMirrorGrant.objects.create(user=self.user, connection=connection,
            slack_workspace_id="TMLAI", slack_user_id="UOWNER", consented_at=timezone.now()-timedelta(minutes=1))
        SlackChannelMembership.objects.create(grant=grant, slack_workspace_id="TMLAI", slack_channel_id="CRANDOM",
            state="joined", source="event", observed_at=timezone.now())
        return grant

    def provider(self, mock, file):
        def read(authority, connection, method, **kwargs):
            if method == "files_info":
                return file
            if method == "conversations_info":
                channel = kwargs["channel"]
                return {"ok": True, "channel": {"id": channel, "is_channel": channel.startswith("C"),
                    "is_im": channel.startswith("D"), "is_private": channel.startswith("D"), "is_member": True}}
            raise AssertionError(method)
        mock.side_effect = read

    def tearDown(self):
        cache.clear()

    def test_private_network_urls_are_rejected(self):
        with self.assertRaises(LinkPreviewError):
            _validated_public_url("http://127.0.0.1/admin/")

    def test_preview_endpoint_returns_bounded_open_graph_metadata(self):
        html = b"""
            <html><head>
              <meta property="og:title" content="Human brain cells predict tokens">
              <meta property="og:description" content="A short article description">
              <meta property="og:site_name" content="Parasma">
              <meta property="og:image" content="/preview.jpg">
            </head></html>
        """
        public_dns = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
        with patch(
            "community_chat.link_previews.socket.getaddrinfo",
            return_value=public_dns,
        ), patch(
            "community_chat.link_previews.requests.Session.get",
            return_value=_FakeResponse(body=html),
        ):
            response = self.client.get(
                reverse("community_chat_link_preview"),
                {"url": "https://example.com/article"},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Human brain cells predict tokens")
        self.assertEqual(response.data["description"], "A short article description")
        self.assertEqual(response.data["site_name"], "Parasma")
        self.assertIn(
            reverse("community_chat_link_preview_image"),
            response.data["image_url"],
        )
        self.assertIn("example.com%2Fpreview.jpg", response.data["image_url"])

    def test_preview_endpoint_requires_a_chat_session(self):
        response = APIClient().get(
            reverse("community_chat_link_preview"),
            {"url": "https://example.com/article"},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("integrations.services.slack_dm_mirror._invoke_slack_with_locked_connection")
    def test_slack_image_in_a_mapped_public_channel_returns_proxied_preview(
        self,
        get_client,
    ):
        self.member()
        CommunityBridgeChannel.objects.create(audience="community", slack_visibility="public", mirror_decision="mirror",
            slack_workspace_id="TMLAI",
            slack_channel_id="CRANDOM",
            slack_channel_name="random-and-memes",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id="buzz-random",
        )
        self.provider(get_client, {
            "ok": True,
            "file": {
                "id": "F0BRPQD104F", "team_id": "TMLAI",
                "title": "image.png",
                "mimetype": "image/png",
                "channels": ["CRANDOM"],
                "permalink": "https://mlai-aus.slack.com/files/U123/F0BRPQD104F/image.png",
                "url_private_download": "https://files.slack.com/files-pri/T-F/image.png",
            },
        })

        response = self.client.get(
            reverse("community_chat_link_preview"),
            {
                "url": "https://mlai-aus.slack.com/files/U123/F0BRPQD104F/image.png"
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "image.png")
        self.assertEqual(response.data["site_name"], "MLAI Slack")
        self.assertIn("slack_file=F0BRPQD104F", response.data["image_url"])
        self.assertNotIn("xoxb", str(response.data))

    @patch("community_chat.slack_file_previews.requests.Session.get")
    @patch("integrations.services.slack_dm_mirror._invoke_slack_with_locked_connection")
    def test_slack_image_proxy_downloads_with_server_side_credentials(
        self,
        get_client,
        session_get,
    ):
        self.member()
        CommunityBridgeChannel.objects.create(audience="community", slack_visibility="public", mirror_decision="mirror",
            slack_workspace_id="TMLAI",
            slack_channel_id="CRANDOM",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id="buzz-random",
        )
        self.provider(get_client, {
            "ok": True,
            "file": {
                "id": "F0BRPQD104F", "team_id": "TMLAI",
                "title": "image.png",
                "mimetype": "image/png",
                "channels": ["CRANDOM"],
                "url_private_download": "https://files.slack.com/files-pri/T-F/image.png",
            },
        })
        session_get.return_value = _FakeResponse(
            body=b"\x89PNG\r\n\x1a\npreview",
            content_type="image/png",
        )

        response = self.client.get(
            reverse("community_chat_link_preview_image"),
            {"slack_file": "F0BRPQD104F"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\npreview")
        self.assertEqual(
            session_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer xoxp-owner",
        )

    @patch("integrations.services.slack_dm_mirror._invoke_slack_with_locked_connection")
    def test_slack_image_outside_mapped_channels_is_rejected(self, get_client):
        self.provider(get_client, {
            "ok": True,
            "file": {
                "id": "F0BRPQD104F", "team_id": "TMLAI",
                "title": "private.png",
                "mimetype": "image/png",
                "channels": ["CUNMAPPED"],
                "url_private_download": "https://files.slack.com/files-pri/T-F/image.png",
            },
        })

        response = self.client.get(
            reverse("community_chat_link_preview"),
            {
                "url": "https://mlai-aus.slack.com/files/U123/F0BRPQD104F/private.png"
            },
        )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"], "preview_unavailable")

    @patch("community_chat.slack_file_previews.requests.Session.get")
    @patch("integrations.services.slack_dm_mirror._invoke_slack_with_locked_connection")
    def test_slack_dm_owner_can_render_an_image_from_their_private_mirror(
        self,
        get_client,
        session_get,
    ):
        grant = self.member()
        SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TMLAI",
            slack_conversation_id="DPRIVATE",
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        private_file = {
            "id": "F0PRIVATE01",
            "title": "private-image.png",
            "mimetype": "image/png",
            "team_id": "TMLAI",
            "ims": ["DPRIVATE"],
            "shares": {"private": {"DPRIVATE": [{}]}},
            "permalink": "https://mlai-aus.slack.com/files/UOWNER/F0PRIVATE01/private-image.png",
            "url_private_download": "https://files.slack.com/files-pri/T-F/private-image.png",
        }
        slack_response = {"ok": True, "file": private_file}
        self.provider(get_client, slack_response)
        session_get.return_value = _FakeResponse(
            body=b"\x89PNG\r\n\x1a\nprivate-preview",
            content_type="image/png",
        )

        preview = self.client.get(
            reverse("community_chat_link_preview"),
            {
                "url": "https://mlai-aus.slack.com/files/UOWNER/F0PRIVATE01/private-image.png"
            },
        )
        image = self.client.get(
            reverse("community_chat_link_preview_image"),
            {"slack_file": "F0PRIVATE01"},
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertIn("slack_file=F0PRIVATE01", preview.data["image_url"])
        self.assertEqual(image.status_code, status.HTTP_200_OK)
        self.assertEqual(image.content, b"\x89PNG\r\n\x1a\nprivate-preview")
        self.assertEqual(
            session_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer xoxp-owner",
        )
