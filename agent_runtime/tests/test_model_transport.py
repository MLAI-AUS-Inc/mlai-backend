"""Model egress tests use synthetic sockets; no DNS or HTTP service is used."""

import io
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from agent_runtime import model_proxy


@override_settings(AGENT_RUNTIME_MODEL_BASE_URL="https://provider.invalid/v1",
                   AGENT_RUNTIME_MODEL_API_KEY="synthetic-model-provider-key",
                   AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS=1)
class ModelTransportTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("agent_runtime.webhook_transport._bounded_resolve",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]))

    def test_provider_request_is_fixed_and_redirect_is_rejected(self):
        connection = SimpleNamespace(sock=None, request=MagicMock(), close=MagicMock(),
            getresponse=lambda: SimpleNamespace(status=307, read=lambda n: b""))
        with patch.object(model_proxy.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(model_proxy.ModelProxyError, "model_provider_rejected"):
                model_proxy.upstream_completion({"model": "allowed", "messages": []})
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/chat/completions"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-model-provider-key")
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called()

    def test_absolute_timer_interrupts_detached_connection_close_response(self):
        interrupted = threading.Event()
        fake_socket = SimpleNamespace(shutdown=MagicMock(side_effect=lambda how: interrupted.set()))
        def response():
            connection.sock = None  # http.client detaches Connection: close.
            return SimpleNamespace(status=200, read=lambda n: b"" if interrupted.wait(timeout=1) else b"unexpected")
        connection = SimpleNamespace(sock=fake_socket, request=MagicMock(), close=MagicMock(), getresponse=response)
        real_timer = threading.Timer
        with (patch.object(model_proxy.http.client, "HTTPSConnection", return_value=connection),
              patch.object(model_proxy.threading, "Timer", side_effect=lambda delay, callback: real_timer(0.01, callback))):
            with self.assertRaisesRegex(model_proxy.ModelProxyError, "model_provider_unavailable"):
                model_proxy.upstream_completion({"model": "allowed", "messages": []})
        fake_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        self.assertTrue(interrupted.is_set())

    def test_bounded_response_only_exposes_completion_fields(self):
        body = io.BytesIO(json.dumps({"choices": [], "usage": {"total_tokens": 1}, "secret_diagnostics": "removed"}).encode())
        connection = SimpleNamespace(sock=None, request=MagicMock(), close=MagicMock(),
            getresponse=lambda: SimpleNamespace(status=200, read=body.read))
        with patch.object(model_proxy.http.client, "HTTPSConnection", return_value=connection):
            result = model_proxy.upstream_completion({"model": "allowed", "messages": []})
        self.assertEqual(result, {"choices": [], "usage": {"total_tokens": 1}})
