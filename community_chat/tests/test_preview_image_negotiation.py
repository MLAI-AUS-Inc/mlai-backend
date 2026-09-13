"""Exercise DRF dispatch with the exact Accept header shipped in iOS 1.0.0(19)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.slack_file_previews import (
    SlackFilePreviewError,
    slack_image_download_url,
)
from community_chat.views import LinkPreviewImageView


class PreviewImageNegotiationTests(SimpleTestCase):
    image_accept = "image/avif,image/webp,image/png,image/jpeg,image/gif"

    def request(self, *, authenticated=True, accept=None):
        request = APIRequestFactory().get(
            "/api/v1/community-chat/link-preview/image/?slack_file=F123",
            HTTP_ACCEPT=accept or self.image_accept,
        )
        if authenticated:
            force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return request

    def test_existing_ios_image_header_reaches_authenticated_download(self):
        for accept in (
            self.image_accept,
            self.image_accept + ",application/json;q=0.1",
        ):
            with self.subTest(accept=accept), patch(
                "community_chat.views.fetch_slack_file_image",
                return_value=("image/png", b"image-bytes"),
            ) as download:
                response = LinkPreviewImageView.as_view(throttle_classes=[])(
                    self.request(accept=accept)
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/png")
                self.assertEqual(response.content, b"image-bytes")
                self.assertIn("private", response["Cache-Control"])
                self.assertEqual(download.call_args.args, ("F123",))

    def test_image_accept_does_not_bypass_authentication(self):
        with patch("community_chat.views.fetch_slack_file_image") as download:
            response = LinkPreviewImageView.as_view(throttle_classes=[])(
                self.request(authenticated=False)
            )
            response.render()
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response["Content-Type"], "application/json")
            download.assert_not_called()

    def test_image_failure_remains_json_with_image_only_accept(self):
        with patch(
            "community_chat.views.fetch_slack_file_image",
            side_effect=SlackFilePreviewError("Not accessible"),
        ):
            response = LinkPreviewImageView.as_view(throttle_classes=[])(self.request())
            response.render()
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertEqual(response.data["error"], "preview_image_unavailable")


class SlackImageRenditionTests(SimpleTestCase):
    def test_preview_prefers_uncropped_display_size_and_fullscreen_uses_original(self):
        file = {
            "mimetype": "image/png",
            "thumb_1024": "https://files.slack.com/1024.jpg",
            "thumb_720": "https://files.slack.com/720.jpg",
            "url_private": "https://files.slack.com/original.png",
        }
        self.assertEqual(slack_image_download_url(file), file["thumb_1024"])
        self.assertEqual(
            slack_image_download_url(file, original=True), file["url_private"]
        )

    def test_animation_and_files_without_display_renditions_use_original(self):
        file = {
            "mimetype": "image/gif",
            "thumb_1024": "https://files.slack.com/still.jpg",
            "url_private": "https://files.slack.com/moving.gif",
        }
        self.assertEqual(slack_image_download_url(file), file["url_private"])
        self.assertEqual(
            slack_image_download_url({"url_private": "original"}), "original"
        )
