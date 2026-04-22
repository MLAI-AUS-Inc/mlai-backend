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

    def test_long_read_timeout_is_capped_to_worker_timeout_budget(self):
        with patch("integrations.http_client.requests.request", return_value=_FakeResponse()) as mock_request:
            http_client.post("https://example.test/resource", timeout=3600)

        _, _, kwargs = mock_request.mock_calls[0]
        self.assertEqual(kwargs["timeout"], (3, 30))

    def test_explicit_split_timeout_is_preserved(self):
        with patch("integrations.http_client.requests.request", return_value=_FakeResponse()) as mock_request:
            http_client.post("https://example.test/resource", timeout=(2, 7), headers={"X-Test": "1"})

        _, _, kwargs = mock_request.mock_calls[0]
        self.assertEqual(kwargs["timeout"], (2.0, 7.0))
        self.assertEqual(kwargs["headers"]["X-Test"], "1")
        self.assertEqual(kwargs["headers"]["Connection"], "close")
