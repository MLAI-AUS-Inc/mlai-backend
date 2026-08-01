from unittest.mock import patch

from django.test import SimpleTestCase

from integrations import http_client


class _FakeResponse:
    content = b'{"ok": true}'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True


class HTTPClientTests(SimpleTestCase):
    def test_numeric_timeout_becomes_split_timeout_with_bounded_connect(self):
        response = _FakeResponse()

        with patch("integrations.http_client.requests.request", return_value=response) as mock_request:
            http_client.get("https://example.test/resource", timeout=20)

        _, _, kwargs = mock_request.mock_calls[0]
        self.assertEqual(kwargs["timeout"], (3, 20.0))
        self.assertEqual(kwargs["headers"]["Connection"], "close")
        self.assertTrue(response.closed)

    def test_long_read_timeout_is_capped_to_max_read_timeout(self):
        with patch("integrations.http_client.requests.request", return_value=_FakeResponse()) as mock_request:
            http_client.post("https://example.test/resource", timeout=3600)

        _, _, kwargs = mock_request.mock_calls[0]
        self.assertEqual(kwargs["timeout"], (3, http_client.MAX_READ_TIMEOUT_SECONDS))
        # Ceiling raised from 30 to 90 for long-running Content Factory calls
        # (PR #243); pin it so the next change is a conscious one.
        self.assertEqual(http_client.MAX_READ_TIMEOUT_SECONDS, 90)

    def test_explicit_split_timeout_is_preserved(self):
        with patch("integrations.http_client.requests.request", return_value=_FakeResponse()) as mock_request:
            http_client.post("https://example.test/resource", timeout=(2, 7), headers={"X-Test": "1"})

        _, _, kwargs = mock_request.mock_calls[0]
        self.assertEqual(kwargs["timeout"], (2.0, 7.0))
        self.assertEqual(kwargs["headers"]["X-Test"], "1")
        self.assertEqual(kwargs["headers"]["Connection"], "close")
