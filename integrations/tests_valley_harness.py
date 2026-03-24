from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.valley_harness import get_valley_harness_api_key, notify_valley_run_created


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
        mock_post.return_value = mock_response

        self.assertTrue(notify_valley_run_created("run-123"))

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
        mock_post.return_value = mock_response

        self.assertTrue(notify_valley_run_created("run-123"))

        self.assertEqual(mock_post.call_args.kwargs["headers"]["X-API-Key"], "mlai-key")

    @override_settings(
        VALLEY_HARNESS_URL="",
        VALLEY_HARNESS_API_KEY="",
        INTERNAL_API_KEY="",
        ROO_API_KEY="",
        MLAI_API_KEY="",
    )
    def test_notify_returns_false_when_not_configured(self):
        self.assertFalse(notify_valley_run_created("run-123"))
