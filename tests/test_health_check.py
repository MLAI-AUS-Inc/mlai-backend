from unittest.mock import MagicMock, patch

from django.test import TestCase


class HealthCheckTests(TestCase):
    @patch("mlai.views.MigrationExecutor")
    def test_health_check_returns_ok_when_no_pending_migrations(self, mock_executor_cls):
        mock_executor = MagicMock()
        mock_executor.loader.graph.leaf_nodes.return_value = [("core", "0041")]
        mock_executor.migration_plan.return_value = []
        mock_executor_cls.return_value = mock_executor

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("mlai.views.MigrationExecutor")
    def test_health_check_returns_not_ready_when_migrations_pending(self, mock_executor_cls):
        mock_executor = MagicMock()
        mock_executor.loader.graph.leaf_nodes.return_value = [("core", "0041")]
        mock_executor.migration_plan.return_value = [("core", "0041")]
        mock_executor_cls.return_value = mock_executor

        response = self.client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
