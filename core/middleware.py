import logging
import time
from uuid import uuid4

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = time.monotonic()
        request_id = str(request.headers.get("X-Request-ID") or "").strip() or f"mlai-{uuid4().hex}"
        request.request_id = request_id
        logger.info(
            "request_started request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.get_full_path(),
        )

        try:
            response = self.get_response(request)
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000
            logger.exception(
                "request_failed request_id=%s method=%s path=%s duration_ms=%.2f exc_type=%s",
                request_id,
                request.method,
                request.get_full_path(),
                duration_ms,
                exc.__class__.__name__,
            )
            raise

        duration_ms = (time.monotonic() - started_at) * 1000
        response["X-Request-ID"] = request_id
        logger.info(
            "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
            request_id,
            request.method,
            request.get_full_path(),
            response.status_code,
            duration_ms,
        )
        return response


class PointsEndpointTimeoutMiddleware:
    """Apply scoped DB timeouts to points endpoints and fail fast on timeouts."""

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _is_points_path(path: str) -> bool:
        return path.startswith("/api/v1/points/") or path == "/healthz/points"

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        message = str(exc).lower()
        timeout_markers = (
            "statement timeout",
            "lock timeout",
            "canceling statement due to",
        )
        return any(marker in message for marker in timeout_markers)

    def __call__(self, request):
        if not self._is_points_path(request.path):
            return self.get_response(request)

        if connection.vendor != "postgresql":
            return self.get_response(request)

        statement_timeout_ms = int(getattr(settings, "POINTS_STATEMENT_TIMEOUT_MS", 12000))
        lock_timeout_ms = int(getattr(settings, "POINTS_LOCK_TIMEOUT_MS", 5000))

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
                    cursor.execute(f"SET LOCAL lock_timeout = {lock_timeout_ms}")
                return self.get_response(request)
        except OperationalError as exc:
            if not self._is_timeout_error(exc):
                raise

            request_id = getattr(request, "request_id", "")
            logger.warning(
                "points_request_timed_out request_id=%s method=%s path=%s exc_type=%s error=%s",
                request_id,
                request.method,
                request.get_full_path(),
                exc.__class__.__name__,
                exc,
            )
            response = JsonResponse(
                {
                    "status": "error",
                    "message": "Points subsystem timed out",
                    "error": str(exc),
                },
                status=503,
            )
            if request_id:
                response["X-Request-ID"] = request_id
            return response
