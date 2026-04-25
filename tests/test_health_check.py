import json
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from django.db import OperationalError
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from core.middleware import PointsEndpointTimeoutMiddleware, RequestLoggingMiddleware


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
        self.assertEqual(response.json()["pending_migration_labels"], ["core.0041"])

    @patch("mlai.views.connections")
    def test_health_live_does_not_touch_database(self, mock_connections):
        mock_connections.__getitem__.side_effect = AssertionError("health_live should not hit the DB")

        response = self.client.get("/healthz/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("mlai.views.MigrationExecutor")
    @patch("mlai.views.connections")
    def test_health_ready_returns_ok_when_database_ping_succeeds(self, mock_connections, mock_executor_cls):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_connections.__getitem__.return_value = mock_connection
        mock_executor = MagicMock()
        mock_executor.loader.graph.leaf_nodes.return_value = [("core", "0041")]
        mock_executor.migration_plan.return_value = []
        mock_executor_cls.return_value = mock_executor

        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        mock_cursor.execute.assert_called_once_with("SELECT 1")

    @patch("mlai.views.MigrationExecutor")
    @patch("mlai.views.connections")
    def test_health_ready_returns_503_when_migrations_pending(self, mock_connections, mock_executor_cls):
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_connections.__getitem__.return_value = mock_connection
        mock_executor = MagicMock()
        mock_executor.loader.graph.leaf_nodes.return_value = [("founder_tools", "0002")]
        mock_executor.migration_plan.return_value = [("founder_tools", "0002")]
        mock_executor_cls.return_value = mock_executor

        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(response.json()["pending_migrations"], 1)
        self.assertEqual(response.json()["pending_migration_labels"], ["founder_tools.0002"])
        mock_cursor.execute.assert_called_once_with("SELECT 1")

    @patch("mlai.views.connections")
    def test_health_ready_returns_503_when_database_ping_fails(self, mock_connections):
        mock_connection = MagicMock()
        mock_connection.cursor.side_effect = RuntimeError("db unavailable")
        mock_connections.__getitem__.return_value = mock_connection

        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")

    def test_health_points_returns_ok(self):
        response = self.client.get("/healthz/points")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["subsystem"], "points")

    @patch("mlai.views.connections")
    def test_health_points_returns_503_when_database_ping_fails(self, mock_connections):
        mock_connection = MagicMock()
        mock_connection.cursor.side_effect = RuntimeError("db unavailable")
        mock_connections.__getitem__.return_value = mock_connection

        response = self.client.get("/healthz/points")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["subsystem"], "points")


class RequestLoggingMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("core.middleware.logger")
    def test_request_logging_logs_start_and_finish_with_same_request_id(self, mock_logger):
        middleware = RequestLoggingMiddleware(lambda request: HttpResponse("ok"))
        request = self.factory.get("/healthz/ready", HTTP_X_REQUEST_ID="mlai-test-request")

        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Request-ID"], "mlai-test-request")
        self.assertEqual(mock_logger.info.call_count, 2)
        start_log = mock_logger.info.call_args_list[0]
        finish_log = mock_logger.info.call_args_list[1]
        self.assertEqual(start_log.args[0], "request_started request_id=%s worker_pid=%s method=%s path=%s")
        self.assertEqual(start_log.args[1], "mlai-test-request")
        self.assertIsInstance(start_log.args[2], int)
        self.assertEqual(
            finish_log.args[0],
            "request_complete request_id=%s worker_pid=%s method=%s path=%s status=%s duration_ms=%.2f",
        )
        self.assertEqual(finish_log.args[1], "mlai-test-request")
        self.assertIsInstance(finish_log.args[2], int)


class PointsEndpointTimeoutMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("core.middleware.transaction.atomic", return_value=nullcontext())
    @patch("core.middleware.connection")
    def test_points_timeout_returns_503_with_request_id(self, mock_connection, _mock_atomic):
        mock_connection.vendor = "postgresql"
        mock_connection.cursor.return_value.__enter__.return_value = MagicMock()
        middleware = PointsEndpointTimeoutMiddleware(
            lambda request: (_ for _ in ()).throw(
                OperationalError("canceling statement due to statement timeout")
            )
        )
        request = self.factory.get("/api/v1/points/coworking/availability/")
        request.request_id = "mlai-timeout-request"

        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["X-Request-ID"], "mlai-timeout-request")
        self.assertEqual(json.loads(response.content)["message"], "Points subsystem timed out")
