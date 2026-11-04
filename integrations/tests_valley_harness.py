from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from integrations.services.valley_harness import get_valley_harness_api_key, notify_valley_run_created
from integrations.services.valley_harness import VALLEY_WORKER_UNAVAILABLE_MESSAGE
from datetime import timedelta
from types import SimpleNamespace
from django.utils import timezone
from vibe_raising.views import _queued_valley_warning, _valley_dispatch_failure_payload


@override_settings(VALLEY_HARNESS_URL="http://valley.local", VALLEY_HARNESS_API_KEY="synthetic-key")
class ValleyWorkerAvailabilityTests(SimpleTestCase):
    @patch("integrations.services.valley_harness.requests.post")
    def test_worker_unavailable_is_preserved_as_a_safe_retryable_error(self, post):
        response = requests.Response()
        response.status_code = 503
        response._content = b'{"detail":{"code":"valley_worker_unavailable","message":"synthetic-upstream-detail"}}'
        post.side_effect = requests.HTTPError("unavailable", response=response)
        result = notify_valley_run_created("run-123")
        self.assertFalse(result)
        self.assertEqual(result.failure_kind, "worker_unavailable")
        self.assertEqual(result.detail, VALLEY_WORKER_UNAVAILABLE_MESSAGE)
        payload = _valley_dispatch_failure_payload(SimpleNamespace(run_id="run-123"), result)
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["message"], VALLEY_WORKER_UNAVAILABLE_MESSAGE)
        self.assertEqual(payload["valleyDispatch"]["failureKind"], "worker_unavailable")

    @patch("integrations.services.valley_harness.requests.post")
    def test_unrelated_503_keeps_existing_failure_classification(self, post):
        response = requests.Response()
        response.status_code = 503
        response._content = b"upstream unavailable"
        post.side_effect = requests.HTTPError("unavailable", response=response)
        self.assertEqual(notify_valley_run_created("run-123").failure_kind, "http_status")

    def test_queued_warning_explains_dispatch_failure_without_changing_run_state(self):
        run = SimpleNamespace(status="queued", updated_at=timezone.now(), result={"_valley_meta": {
            "dispatch_status": "failed", "last_dispatch_error_kind": "worker_unavailable"}})
        self.assertEqual(_queued_valley_warning(run), VALLEY_WORKER_UNAVAILABLE_MESSAGE)
        self.assertEqual(run.status, "queued")

    def test_old_acknowledged_queue_is_not_presented_as_started_work(self):
        run = SimpleNamespace(status="queued", updated_at=timezone.now() - timedelta(hours=2), result={"_valley_meta": {"dispatch_status": "queued"}})
        self.assertIn("has not started reading", _queued_valley_warning(run))
        run.updated_at = timezone.now()
        self.assertIsNone(_queued_valley_warning(run))
        run.status = "running"
        run.updated_at -= timedelta(hours=2)
        self.assertIsNone(_queued_valley_warning(run))


class ValleyHarnessServiceTests(SimpleTestCase):
    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="valley-key",
        INTERNAL_API_KEY="internal-key",
        ROO_API_KEY="roo-key",
        MLAI_API_KEY="mlai-key",
    )
    @patch("integrations.services.valley_harness.requests.post")
    def test_notify_uses_dedicated_valley_key_when_present(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_post.return_value = mock_response

        result = notify_valley_run_created("run-123")

        self.assertTrue(result)
        self.assertTrue(result.ok)
        self.assertEqual(mock_post.call_args.kwargs["headers"]["X-API-Key"], "valley-key")

    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="",
        INTERNAL_API_KEY="internal-key",
        ROO_API_KEY="roo-key",
        MLAI_API_KEY="mlai-key",
    )
    def test_get_valley_harness_api_key_falls_back_to_internal_key(self):
        self.assertEqual(get_valley_harness_api_key(), "internal-key")

    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="mlai-key",
    )
    @patch("integrations.services.valley_harness.requests.post")
    def test_notify_falls_back_to_mlai_api_key(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_post.return_value = mock_response

        result = notify_valley_run_created("run-123")

        self.assertTrue(result)
        self.assertTrue(result.ok)
        self.assertEqual(mock_post.call_args.kwargs["headers"]["X-API-Key"], "mlai-key")

    @override_settings(
        VALLEY_HARNESS_URL="",
        VALLEY_HARNESS_API_KEY="",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="",
    )
    def test_notify_returns_false_when_not_configured(self):
        with self.assertLogs("integrations.services.valley_harness", level="WARNING") as captured:
            self.assertFalse(notify_valley_run_created("run-123"))

        self.assertIn("VALLEY_HARNESS_URL is not configured", captured.output[0])

    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="",
    )
    def test_notify_logs_when_api_key_is_missing(self):
        with self.assertLogs("integrations.services.valley_harness", level="WARNING") as captured:
            self.assertFalse(notify_valley_run_created("run-123"))

        self.assertIn("no service API key is configured", captured.output[0])

    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="valley-key",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="",
    )
    @patch("integrations.services.valley_harness.requests.post")
    def test_notify_logs_http_status_on_request_failure(self, mock_post):
        response = MagicMock()
        response.status_code = 403
        response.text = "forbidden"
        mock_post.side_effect = requests.HTTPError("forbidden", response=response)

        with self.assertLogs("integrations.services.valley_harness", level="WARNING") as captured:
            result = notify_valley_run_created("run-123")

        self.assertFalse(result)
        self.assertEqual(result.failure_kind, "http_status")
        self.assertIn("status=403", captured.output[0])
        self.assertIn("failure_kind=http_status", captured.output[0])
        self.assertIn("api_key_source=VALLEY_HARNESS_API_KEY", captured.output[0])

    @override_settings(
        VALLEY_HARNESS_URL="http://valley.local",
        VALLEY_HARNESS_API_KEY="valley-key",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="",
    )
    @patch("integrations.services.valley_harness.requests.post")
    def test_notify_returns_dns_failure_kind(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("Failed to resolve 'valley.local'")

        result = notify_valley_run_created("run-123")

        self.assertFalse(result)
        self.assertEqual(result.failure_kind, "dns")
        self.assertIn("Failed to resolve", result.detail)
