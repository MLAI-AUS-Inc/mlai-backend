from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from django.conf import settings

from integrations import http_client as requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValleyHarnessResult:
    ok: bool
    payload: dict[str, Any] | None = None
    failure_kind: str = ""
    detail: str = ""
    status_code: int | None = None
    api_key_source: str = ""
    url: str = ""

    def __bool__(self) -> bool:
        return self.ok

    def sanitized_detail(self) -> str:
        return _sanitize_detail(self.detail)


def _get_valley_harness_api_key_with_source() -> tuple[str, str]:
    for setting_name in ("VALLEY_HARNESS_API_KEY", "INTERNAL_API_KEY", "ROO_API_KEY", "MLAI_API_KEY"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value, setting_name
    return "", ""


def get_valley_harness_api_key() -> str:
    return _get_valley_harness_api_key_with_source()[0]


def _sanitize_detail(value: Any, *, max_length: int = 300) -> str:
    detail = str(value or "").strip().replace("\n", " ")
    return detail[:max_length]


def _failure_kind_for_exception(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        message = str(exc).lower()
        if any(marker in message for marker in ("failed to resolve", "name resolution", "getaddrinfo", "nodename")):
            return "dns"
        return "connection"
    if isinstance(exc, requests.HTTPError):
        return "http_status"
    return "request_error"


def _post_to_valley_harness(path: str, *, payload: dict | None = None) -> ValleyHarnessResult:
    base_url = str(getattr(settings, "VALLEY_HARNESS_URL", "") or "").strip().rstrip("/")
    api_key, api_key_source = _get_valley_harness_api_key_with_source()
    if not base_url:
        logger.warning(
            "Skipping Valley harness request for %s because VALLEY_HARNESS_URL is not configured",
            path,
        )
        return ValleyHarnessResult(ok=False, failure_kind="missing_config", detail="VALLEY_HARNESS_URL is not configured")
    if not api_key:
        logger.warning(
            "Skipping Valley harness request for %s because no service API key is configured",
            path,
        )
        return ValleyHarnessResult(ok=False, failure_kind="missing_key", detail="No service API key is configured")

    endpoint = f"{base_url}{path}"
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=(3, 10),
        )
        response.raise_for_status()
        response_payload = response.json() if response.content else {}
        return ValleyHarnessResult(
            ok=True,
            payload=response_payload if isinstance(response_payload, dict) else {},
            status_code=response.status_code,
            api_key_source=api_key_source,
            url=endpoint,
        )
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        response_text = str(getattr(response, "text", "") or "").strip()
        response_excerpt = response_text[:200] if response_text else ""
        failure_kind = _failure_kind_for_exception(exc)
        detail = _sanitize_detail(response_excerpt or exc)
        logger.warning(
            "Failed Valley harness request (path=%s, url=%s, api_key_source=%s, status=%s, failure_kind=%s, detail=%r)",
            path,
            endpoint,
            api_key_source,
            status_code,
            failure_kind,
            detail,
        )
        return ValleyHarnessResult(
            ok=False,
            failure_kind=failure_kind,
            detail=detail,
            status_code=status_code,
            api_key_source=api_key_source,
            url=endpoint,
        )


def notify_valley_run_created(run_id: str) -> ValleyHarnessResult:
    result = _post_to_valley_harness("/internal/runs", payload={"run_id": run_id})
    if not result:
        return result

    response_payload = result.payload or {}
    logger.info(
        "Notified Valley harness for startup update run %s",
        run_id,
        extra={
            "run_id": run_id,
            "job_id": response_payload.get("job_id"),
            "status": response_payload.get("status"),
        },
    )
    return result


def cancel_valley_run(run_id: str) -> dict:
    result = _post_to_valley_harness(f"/internal/runs/{run_id}/cancel", payload=None)
    if not result:
        return {
            "run_id": run_id,
            "revoke_requested": False,
            "revoke_succeeded": False,
            "revoked_job_ids": [],
            "missing_job_ids": [],
        }

    response_payload = result.payload or {}
    logger.info(
        "Requested Valley cancellation for startup update run %s",
        run_id,
        extra={
            "run_id": run_id,
            "status": response_payload.get("status"),
            "revoked_job_ids": response_payload.get("revoked_job_ids") or [],
            "missing_job_ids": response_payload.get("missing_job_ids") or [],
        },
    )
    return {
        "run_id": run_id,
        "revoke_requested": True,
        "revoke_succeeded": True,
        "revoked_job_ids": list(response_payload.get("revoked_job_ids") or []),
        "missing_job_ids": list(response_payload.get("missing_job_ids") or []),
    }
