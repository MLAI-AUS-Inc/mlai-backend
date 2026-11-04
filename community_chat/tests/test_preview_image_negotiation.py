"""Exercise DRF dispatch with the exact Accept header shipped in iOS 1.0.0(19)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings, RequestFactory
from django.http import HttpResponse
from corsheaders.middleware import CorsMiddleware
from core.middleware import DesktopAuthCorsMiddleware
from django.core.cache import cache
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.message_sync.slack_client import provider_interval
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.slack_file_previews import (
    SlackFilePreviewError,
    SlackFilePreviewDeferred,
    SlackFilePreview,
    _authorized_file,
    _slack_file_info,
    slack_image_download_url,
)
from community_chat.views import LinkPreviewImageView, LinkPreviewView


class PreviewImageNegotiationTests(SimpleTestCase):
    image_accept = "image/avif,image/webp,image/png,image/jpeg,image/gif"

    def test_preview_wait_logs_only_fixed_dependency_reason_and_keeps_retry_contract(self):
        for view, source, endpoint in (
            (LinkPreviewView, "fetch_slack_file_preview", "link_preview"),
            (LinkPreviewImageView, "fetch_slack_file_image", "link_preview_image"),
        ):
            with self.subTest(endpoint=endpoint), patch(
                "community_chat.views.resolve_slack_message_reference", return_value=None,
            ), patch(
                f"community_chat.views.{source}",
                side_effect=SlackFilePreviewDeferred(7, reason="shared_budget"),
            ), self.assertLogs("community_chat.views", level="INFO") as logged:
                response = view.as_view(throttle_classes=[])(self.request(accept="application/json"))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response["Retry-After"], "7")
            self.assertEqual(response.data["error"], "preview_pending")
            self.assertNotIn("reason", response.data)
            self.assertEqual(len(logged.output), 1)
            self.assertIn(f"endpoint={endpoint} dependency=slack reason=shared_budget", logged.output[0])
            self.assertNotIn("F123", logged.output[0])

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
                self.assertEqual(response["Cache-Control"], "private, no-store")
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

    def test_scheduling_delay_is_retryable_and_not_cached(self):
        with patch(
            "community_chat.views.fetch_slack_file_image",
            side_effect=SlackFilePreviewDeferred(3),
        ):
            response = LinkPreviewImageView.as_view(throttle_classes=[])(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "3")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["error"], "preview_pending")

    def test_metadata_delay_uses_the_same_retry_contract(self):
        with patch(
            "community_chat.views.resolve_slack_message_reference", return_value=None
        ), patch(
            "community_chat.views.fetch_slack_file_preview",
            side_effect=SlackFilePreviewDeferred(7),
        ):
            response = LinkPreviewView.as_view(throttle_classes=[])(
                self.request(accept="application/json")
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "7")

    def test_non_image_file_is_a_link_card_and_optional_thumbnail(self):
        for thumbnail in (False, True):
            preview = SlackFilePreview(
                "F123",
                "https://mlai.slack.com/files/U123/F123/report.pdf",
                "Report",
                "File",
                "Slack",
                "application/pdf",
                thumbnail,
            )
            with patch(
                "community_chat.views.resolve_slack_message_reference",
                return_value=None,
            ), patch(
                "community_chat.views.fetch_slack_file_preview", return_value=preview
            ):
                response = LinkPreviewView.as_view(throttle_classes=[])(
                    self.request(accept="application/json")
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(bool(response.data["image_url"]), thumbnail)
            self.assertTrue(response.data["image_is_thumbnail"])
            self.assertEqual(response["Cache-Control"], "private, no-store")


class SlackFileBudgetTests(SimpleTestCase):
    def setUp(self):
        from unittest.mock import Mock
        cache.clear()
        self.addCleanup(cache.clear)
        self.read = Mock(return_value={"ok": True, "file": {"id": "F123"}})

    def info(self):
        return _slack_file_info("F123", read=self.read, cache_scope="current-member")

    def test_wait_reason_is_fixed_and_survives_cached_transport_failure(self):
        from community_chat import slack_file_previews as previews
        for reason in ("private URL or token", {"untrusted": "value"}, ["transport"], None):
            self.assertEqual(SlackFilePreviewDeferred(reason=reason).reason, "metadata_pending")
        previews._remember_failure("synthetic-failure-key", SlackFilePreviewDeferred(15, reason="transport"))
        with self.assertRaises(SlackFilePreviewDeferred) as caught:
            previews._raise_cached_failure("synthetic-failure-key")
        self.assertEqual(caught.exception.reason, "transport")
        self.assertLessEqual(caught.exception.retry_after, 15)

    def test_provider_rate_limit_reason_is_retained_on_cache_hit(self):
        from slack_sdk.errors import SlackApiError
        self.read.side_effect = SlackApiError("synthetic", response={"error": "ratelimited"})
        for _ in range(2):
            with self.assertRaises(SlackFilePreviewDeferred) as caught:
                self.info()
            self.assertEqual(caught.exception.reason, "upstream_rate_limit")
        self.assertEqual(self.read.call_count, 1)

    def test_files_info_uses_shared_tier_four_budget(self):
        self.assertEqual(provider_interval("files.info"), 0.6)

    def test_deferred_metadata_is_not_cached_and_recovers(self):
        self.read.side_effect = [BudgetDeferred(3), {"ok": True, "file": {"id": "F123"}}]
        with self.assertRaises(SlackFilePreviewDeferred) as caught:
            self.info()
        self.assertEqual(caught.exception.reason, "shared_budget")
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertEqual(self.info()["id"], "F123")
        self.assertEqual(self.info()["id"], "F123")
        self.assertEqual(self.read.call_count, 2)

    def test_overlapping_metadata_reads_wait_for_one_provider_call(self):
        def read(**kwargs):
            with self.assertRaises(SlackFilePreviewDeferred):
                self.info()
            return {"ok": True, "file": {"id": "F123"}}
        self.read.side_effect = read
        self.assertEqual(self.info()["id"], "F123")
        self.assertEqual(self.read.call_count, 1)

    def test_network_timeout_releases_metadata_lock_after_cooldown(self):
        self.read.side_effect = [TimeoutError(), {"ok": True, "file": {"id": "F123"}}]
        with patch("time.time", return_value=1000) as clock:
            with self.assertRaises(SlackFilePreviewDeferred):
                self.info()
            clock.return_value = 1016
            self.assertEqual(self.info()["id"], "F123")


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

    def test_pdf_and_video_use_slack_thumbnails_without_requesting_original_media(self):
        for file in (
            {
                "mimetype": "application/pdf",
                "thumb_pdf": "https://files.slack.com/pdf.png",
            },
            {"mimetype": "video/mp4", "thumb_720": "https://files.slack.com/video.jpg"},
        ):
            file["url_private"] = "https://files.slack.com/original"
            self.assertNotEqual(slack_image_download_url(file), file["url_private"])


class PreviewRetryCorsTests(SimpleTestCase):
    @override_settings(
        CORS_ALLOWED_ORIGINS=["https://chat.mlai.au"], CORS_ALLOW_ALL_ORIGINS=False
    )
    def test_browser_and_desktop_can_observe_the_provider_retry_deadline(self):
        def response(_):
            result = HttpResponse(status=503)
            result["Retry-After"] = "60"
            return result

        middleware = DesktopAuthCorsMiddleware(CorsMiddleware(response))
        for origin in (
            "https://chat.mlai.au",
            "tauri://localhost",
            "http://tauri.localhost",
        ):
            request = RequestFactory().get(
                "/api/v1/community-chat/link-preview/image/", HTTP_ORIGIN=origin
            )
            result = middleware(request)
            self.assertIn("Retry-After", result["Access-Control-Expose-Headers"])
            self.assertEqual(result["Retry-After"], "60")
            self.assertEqual(result["Access-Control-Allow-Origin"], origin)
            if origin != "https://chat.mlai.au":
                self.assertNotIn("Access-Control-Allow-Credentials", result)
