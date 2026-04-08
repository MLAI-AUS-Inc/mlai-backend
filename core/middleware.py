import logging
import time
from uuid import uuid4

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = time.monotonic()
        request_id = str(request.headers.get("X-Request-ID") or "").strip() or f"mlai-{uuid4().hex}"
        request.request_id = request_id

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
