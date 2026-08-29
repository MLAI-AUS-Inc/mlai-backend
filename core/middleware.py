import logging
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.http import HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers

logger = logging.getLogger(__name__)
SENSITIVE_QUERY_PARAMETERS = {
    "access_token",
    "client_secret",
    "code",
    "id_token",
    "refresh_token",
    "state",
    "token",
}


class DesktopAuthCorsMiddleware:
    """Allow exact Tauri origins to call Community Chat without cookies."""

    _ALLOWED_ORIGINS = {"http://tauri.localhost", "tauri://localhost"}
    _ALLOWED_PATH_PREFIX = "/api/v1/community-chat/"
    _ALLOWED_METHODS = {"DELETE", "GET", "HEAD", "PATCH", "POST"}
    _ALLOWED_REQUEST_HEADERS = {"authorization", "content-type", "x-request-id"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
        if (
            not request.path.startswith(self._ALLOWED_PATH_PREFIX)
            or origin not in self._ALLOWED_ORIGINS
        ):
            return self.get_response(request)

        is_preflight = request.method == "OPTIONS" and bool(
            request.headers.get("Access-Control-Request-Method")
        )
        if is_preflight:
            requested_method = str(
                request.headers.get("Access-Control-Request-Method") or ""
            ).upper()
            requested_headers = {
                item.strip().lower()
                for item in str(
                    request.headers.get("Access-Control-Request-Headers") or ""
                ).split(",")
                if item.strip()
            }
            if requested_method not in self._ALLOWED_METHODS or not requested_headers.issubset(
                self._ALLOWED_REQUEST_HEADERS
            ):
                return HttpResponse(status=204)
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        # Fail closed if a deployment accidentally adds a Tauri origin to the
        # global credentialed django-cors-headers allowlist.
        if "Access-Control-Allow-Credentials" in response:
            del response["Access-Control-Allow-Credentials"]
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Methods"] = (
            "DELETE, GET, HEAD, PATCH, POST, OPTIONS"
        )
        response["Access-Control-Allow-Headers"] = (
            "authorization, content-type, x-request-id"
        )
        response["Access-Control-Expose-Headers"] = "X-Request-ID"
        response["Access-Control-Max-Age"] = "600"
        patch_vary_headers(response, ("Origin",))
        return response


def safe_request_path(request) -> str:
    """Return a log-safe request path with OAuth credentials redacted."""

    full_path = request.get_full_path()
    try:
        parsed = urlsplit(full_path)
        query = [
            (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_PARAMETERS else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    except (TypeError, ValueError):
        return str(getattr(request, "path", "") or "")
    return parsed.path + ("?" + urlencode(query) if query else "")


class RequestLoggingMiddleware:
    _REDACT_QUERY_PATH_PREFIXES = (
        "/api/v1/hackathons/hospital/sim-guess/status/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def _safe_path(cls, request):
        path = request.path
        if any(path.startswith(prefix) for prefix in cls._REDACT_QUERY_PATH_PREFIXES):
            return f"{path}?<redacted>" if request.META.get("QUERY_STRING") else path
        return safe_request_path(request)

    def __call__(self, request):
        started_at = time.monotonic()
        request_id = str(request.headers.get("X-Request-ID") or "").strip() or f"mlai-{uuid4().hex}"
        request.request_id = request_id
        worker_pid = os.getpid()
        safe_path = self._safe_path(request)
        logger.info(
            "request_started request_id=%s worker_pid=%s method=%s path=%s",
            request_id,
            worker_pid,
            request.method,
            safe_path,
        )

        try:
            response = self.get_response(request)
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000
            logger.exception(
                "request_failed request_id=%s worker_pid=%s method=%s path=%s duration_ms=%.2f exc_type=%s",
                request_id,
                worker_pid,
                request.method,
                safe_path,
                duration_ms,
                exc.__class__.__name__,
            )
            raise

        duration_ms = (time.monotonic() - started_at) * 1000
        response["X-Request-ID"] = request_id
        logger.info(
            "request_complete request_id=%s worker_pid=%s method=%s path=%s status=%s duration_ms=%.2f",
            request_id,
            worker_pid,
            request.method,
            safe_path,
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

    @staticmethod
    def _is_connection_interruption_error(exc: Exception) -> bool:
        error_parts = [str(exc)]
        if exc.__cause__:
            error_parts.append(str(exc.__cause__))
        if exc.__context__:
            error_parts.append(str(exc.__context__))
        message = " ".join(error_parts).lower()
        interruption_markers = (
            "terminating connection due to administrator command",
            "server closed the connection unexpectedly",
            "connection already closed",
            "connection is closed",
            "connection not open",
            "connection reset by peer",
            "ssl connection has been closed unexpectedly",
            "ssl syscall error",
            "adminshutdown",
        )
        return any(marker in message for marker in interruption_markers)

    @staticmethod
    def _json_503_response(
        *,
        request_id: str,
        message: str,
        error_code: str,
        error: str,
        retryable: bool = True,
    ) -> JsonResponse:
        response = JsonResponse(
            {
                "status": "error",
                "message": message,
                "error_code": error_code,
                "retryable": retryable,
                "error": error,
            },
            status=503,
        )
        if request_id:
            response["X-Request-ID"] = request_id
        return response

    def __call__(self, request):
        if not self._is_points_path(request.path):
            return self.get_response(request)

        if connection.vendor != "postgresql":
            return self.get_response(request)

        statement_timeout_ms = int(getattr(settings, "POINTS_STATEMENT_TIMEOUT_MS", 12000))
        lock_timeout_ms = int(getattr(settings, "POINTS_LOCK_TIMEOUT_MS", 5000))
        started_at = time.monotonic()

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
                    cursor.execute(f"SET LOCAL lock_timeout = {lock_timeout_ms}")
                return self.get_response(request)
        except OperationalError as exc:
            is_timeout_error = self._is_timeout_error(exc)
            is_connection_interruption_error = self._is_connection_interruption_error(exc)
            if not is_timeout_error and not is_connection_interruption_error:
                raise

            if is_connection_interruption_error:
                try:
                    connection.close()
                except Exception:
                    logger.exception("points_connection_close_failed")

            request_id = getattr(request, "request_id", "")
            duration_ms = (time.monotonic() - started_at) * 1000
            if is_timeout_error:
                message = "Points subsystem timed out"
                error_code = "points_timeout"
            else:
                message = "Points subsystem is temporarily unavailable"
                error_code = "database_connection_interrupted"
            logger.warning(
                "points_request_failed request_id=%s worker_pid=%s method=%s path=%s duration_ms=%.2f exc_type=%s error_code=%s retryable=%s error=%s",
                request_id,
                os.getpid(),
                request.method,
                safe_request_path(request),
                duration_ms,
                exc.__class__.__name__,
                error_code,
                True,
                exc,
            )
            return self._json_503_response(
                request_id=request_id,
                message=message,
                error_code=error_code,
                retryable=True,
                error=str(exc),
            )
