from datetime import timedelta
from unittest.mock import patch
import uuid

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.link_previews import LinkPreviewError, _validated_public_url
from community_chat.models import CommunityChatEmailCodeChallenge


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
