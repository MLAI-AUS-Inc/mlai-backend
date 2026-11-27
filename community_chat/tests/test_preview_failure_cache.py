"""Offline failure suppression, expiry and private preview scope regressions."""

from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from community_chat import link_previews as public, slack_file_previews as slack


class PublicPreviewFailureTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.dns = patch.object(public.socket, "getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.addCleanup(cache.clear)

    def test_repeated_slow_page_failure_is_suppressed_then_recovers_after_expiry(self):
        with (
            patch("time.time", return_value=1000) as clock,
            patch.object(public, "_fetch_limited", side_effect=[
                public.LinkPreviewError("could not reach provider"),
                ("https://example.com/", "text/html", b"<title>Recovered</title>"),
            ]) as fetch,
        ):
            for _ in range(3):
                with self.assertRaises(public.LinkPreviewError):
                    public.fetch_link_preview("https://example.com/")
            self.assertEqual(fetch.call_count, 1)
            clock.return_value = 1061
            self.assertEqual(public.fetch_link_preview("https://example.com/").title, "Recovered")
            self.assertEqual(public.fetch_link_preview("https://example.com/").title, "Recovered")
            self.assertEqual(fetch.call_count, 2)

    def test_image_failures_are_separate_from_page_and_other_url_caches(self):
        with patch.object(public, "_fetch_limited", side_effect=public.LinkPreviewError("failed")) as fetch:
            for method, url in (
                (public.fetch_preview_image, "https://example.com/a"),
                (public.fetch_preview_image, "https://example.com/a"),
                (public.fetch_link_preview, "https://example.com/a"),
                (public.fetch_preview_image, "https://example.com/b"),
            ):
                with self.assertRaises(public.LinkPreviewError):
                    method(url)
            self.assertEqual(fetch.call_count, 3)

    def test_network_timeout_is_cached_without_storing_exception_details(self):
        with patch.object(public.requests.Session, "get", side_effect=requests.Timeout("secret provider detail")) as fetch:
            for _ in range(2):
                with self.assertRaises(public.LinkPreviewError) as caught:
                    public.fetch_link_preview("https://example.com/")
                self.assertNotIn("secret provider detail", str(caught.exception))
            self.assertEqual(fetch.call_count, 1)

    def test_cached_failure_never_bypasses_public_network_validation(self):
        with patch.object(public, "_fetch_limited", side_effect=public.LinkPreviewError("failed")) as fetch:
            with self.assertRaises(public.LinkPreviewError):
                public.fetch_link_preview("https://example.com/")
            with patch.object(public.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
                with self.assertRaisesMessage(public.LinkPreviewError, "Private network"):
                    public.fetch_link_preview("https://example.com/")
            self.assertEqual(fetch.call_count, 1)


class SlackPreviewFailureTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_metadata_network_failure_counts_down_then_recovers(self):
        from unittest.mock import Mock
        read = Mock(side_effect=[TimeoutError(), {"ok": True, "file": {"id": "F123"}}])
        with patch("time.time", return_value=1000) as clock:
            with self.assertRaises(slack.SlackFilePreviewDeferred) as first:
                slack._slack_file_info("F123", read=read, cache_scope="grant-one")
            self.assertEqual(first.exception.retry_after, 15)
            clock.return_value = 1005
            with self.assertRaises(slack.SlackFilePreviewDeferred) as second:
                slack._slack_file_info("F123", read=read, cache_scope="grant-one")
            self.assertEqual(second.exception.retry_after, 10)
            self.assertEqual(read.call_count, 1)
            clock.return_value = 1016
            self.assertEqual(slack._slack_file_info("F123", read=read, cache_scope="grant-one"), {"id": "F123"})

    def test_metadata_failure_does_not_cross_owner_or_grant(self):
        from unittest.mock import Mock
        read = Mock(side_effect=TimeoutError())
        for scope in ("user:1:grant:1", "user:1:grant:1", "user:2:grant:2", "user:1:grant:3"):
            with self.assertRaises(slack.SlackFilePreviewDeferred):
                slack._slack_file_info("F123", read=read, cache_scope=scope)
        self.assertEqual(read.call_count, 3)

    def test_image_failure_checks_current_authority_and_is_scoped_to_grant(self):
        from contextlib import nullcontext
        def authorized(scope):
            return nullcontext(slack._AuthorizedSlackFile(
                data={"mimetype": "image/png", "url_private": "https://files.slack.com/files-pri/T/F.png"},
                access_token="synthetic", cache_scope=scope))
        with patch.object(slack, "_authorized_file", return_value=authorized("grant-one")) as auth, \
             patch.object(slack.requests.Session, "get", side_effect=requests.Timeout()) as fetch:
            for _ in range(2):
                with self.assertRaises(slack.SlackFilePreviewDeferred):
                    slack.fetch_slack_file_image("F123", user="owner")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(auth.call_count, 2)
            auth.return_value = authorized("grant-two")
            with self.assertRaises(slack.SlackFilePreviewDeferred):
                slack.fetch_slack_file_image("F123", user="owner")
            self.assertEqual(fetch.call_count, 2)
            auth.side_effect = slack.SlackFilePreviewError("revoked")
            with self.assertRaisesMessage(slack.SlackFilePreviewError, "revoked"):
                slack.fetch_slack_file_image("F123", user="owner")
            self.assertEqual(fetch.call_count, 2)


class _SlowResponse:
    """A streamed HTML response whose chunks are produced on demand."""

    def __init__(self, chunks, content_type="text/html; charset=utf-8"):
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.is_redirect = False
        self.is_permanent_redirect = False
        self.read_count = 0

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        for chunk in self.chunks:
            self.read_count += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        return None


class PublicPreviewSpeedTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.dns = patch.object(public.socket, "getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.addCleanup(cache.clear)

    def test_deterministic_failures_are_remembered_longer_than_transient_ones(self):
        with (
            patch("time.time", return_value=1000) as clock,
            patch.object(public, "_fetch_limited", return_value=(
                "https://example.com/report.pdf", "application/pdf", b"%PDF",
            )) as fetch,
        ):
            with self.assertRaisesMessage(public.LinkPreviewError, "did not return an HTML page"):
                public.fetch_link_preview("https://example.com/report.pdf")
            clock.return_value = 1000 + public.FAILURE_CACHE_SECONDS + 1
            with self.assertRaisesMessage(public.LinkPreviewError, "temporarily unavailable"):
                public.fetch_link_preview("https://example.com/report.pdf")
            self.assertEqual(fetch.call_count, 1)
            clock.return_value = 1000 + public.DETERMINISTIC_FAILURE_CACHE_SECONDS + 1
            with self.assertRaisesMessage(public.LinkPreviewError, "did not return an HTML page"):
                public.fetch_link_preview("https://example.com/report.pdf")
            self.assertEqual(fetch.call_count, 2)

    def test_unsupported_image_type_is_remembered_longer(self):
        with (
            patch("time.time", return_value=1000) as clock,
            patch.object(public, "_fetch_limited", return_value=(
                "https://example.com/a.svg", "image/svg+xml", b"<svg/>",
            )) as fetch,
        ):
            for offset in (0, public.FAILURE_CACHE_SECONDS + 1):
                clock.return_value = 1000 + offset
                with self.assertRaises(public.LinkPreviewError):
                    public.fetch_preview_image("https://example.com/a.svg")
            self.assertEqual(fetch.call_count, 1)

    def test_page_read_stops_after_the_document_head(self):
        response = _SlowResponse([
            b"<html><head><title>Fast</title>",
            b"<meta property='og:description' content='Head only'></he",
            b"ad><body>",
            AssertionError("the body must not be downloaded"),
        ])
        with patch.object(public.requests.Session, "get", return_value=response):
            preview = public.fetch_link_preview("https://example.com/")
        self.assertEqual(preview.title, "Fast")
        self.assertEqual(preview.description, "Head only")
        self.assertEqual(response.read_count, 3)

    def test_image_read_is_not_cut_at_a_head_marker(self):
        body = b"GIF89a</head>" + b"x" * 10
        response = _SlowResponse([body[:8], body[8:]], content_type="image/gif")
        with patch.object(public.requests.Session, "get", return_value=response):
            content_type, image = public.fetch_preview_image("https://example.com/a.gif")
        self.assertEqual((content_type, image), ("image/gif", body))

    def test_slow_drip_page_is_cut_off_at_the_total_deadline(self):
        clock = iter([0.0, 0.0, 1.0, public.PAGE_DEADLINE_SECONDS + 0.5, 99.0, 99.0])
        response = _SlowResponse([b"<html>", b"<p>", b"<p>", b"<p>"])
        with (
            patch.object(public.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(public.requests.Session, "get", return_value=response),
        ):
            with self.assertRaisesMessage(public.LinkPreviewError, "took too long"):
                public.fetch_link_preview("https://example.com/slow")
        self.assertLess(response.read_count, 4)

    def test_each_request_timeout_is_capped_by_the_remaining_deadline(self):
        redirect = _SlowResponse([])
        redirect.is_redirect = True
        redirect.headers = {"Location": "https://example.com/next"}
        final = _SlowResponse([b"<title>Done</title></head>"])
        clock = iter([0.0, 0.0, public.PAGE_DEADLINE_SECONDS - 1.5] + [public.PAGE_DEADLINE_SECONDS - 1.0] * 5)
        with (
            patch.object(public.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(public.requests.Session, "get", side_effect=[redirect, final]) as get,
        ):
            self.assertEqual(public.fetch_link_preview("https://example.com/").title, "Done")
        self.assertEqual(get.call_args_list[0].kwargs["timeout"], public.REQUEST_TIMEOUT)
        connect, read = get.call_args_list[1].kwargs["timeout"]
        self.assertLessEqual(connect, 1.5)
        self.assertLessEqual(read, 1.5)

    def test_deadline_already_spent_before_a_redirect_hop_fails_without_a_request(self):
        redirect = _SlowResponse([])
        redirect.is_redirect = True
        redirect.headers = {"Location": "https://example.com/next"}
        clock = iter([0.0, 0.0, public.PAGE_DEADLINE_SECONDS + 1, 99.0])
        with (
            patch.object(public.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(public.requests.Session, "get", side_effect=[redirect]) as get,
        ):
            with self.assertRaisesMessage(public.LinkPreviewError, "took too long"):
                public.fetch_link_preview("https://example.com/")
        self.assertEqual(get.call_count, 1)
