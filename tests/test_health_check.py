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

    @patch("mlai.views.connections")
    def test_health_live_does_not_touch_database(self, mock_connections):
        mock_connections.__getitem__.side_effect = AssertionError("health_live should not hit the DB")

        response = self.client.get("/healthz/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("mlai.views.connections")
    def test_health_ready_returns_ok_when_database_ping_succeeds(self, mock_connections):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_connections.__getitem__.return_value = mock_connection

        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        mock_cursor.execute.assert_called_once_with("SELECT 1")

    @patch("mlai.views.connections")
    def test_health_ready_returns_503_when_database_ping_fails(self, mock_connections):
        mock_connection = MagicMock()
        mock_connection.cursor.side_effect = RuntimeError("db unavailable")
        mock_connections.__getitem__.return_value = mock_connection

        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
