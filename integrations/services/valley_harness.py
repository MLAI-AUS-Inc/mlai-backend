from __future__ import annotations

import logging

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


def _get_valley_harness_api_key_with_source() -> tuple[str, str]:
    for setting_name in ("VALLEY_HARNESS_API_KEY", "INTERNAL_API_KEY", "ROO_API_KEY", "MLAI_API_KEY"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value, setting_name
    return "", ""


def get_valley_harness_api_key() -> str:
    return _get_valley_harness_api_key_with_source()[0]


def _post_to_valley_harness(path: str, *, payload: dict | None = None) -> dict | None:
    base_url = str(getattr(settings, "VALLEY_HARNESS_URL", "") or "").strip().rstrip("/")
    api_key, api_key_source = _get_valley_harness_api_key_with_source()
    if not base_url:
        logger.warning(
            "Skipping Valley harness request for %s because VALLEY_HARNESS_URL is not configured",
            path,
        )
        return None
    if not api_key:
        logger.warning(
            "Skipping Valley harness request for %s because no service API key is configured",
            path,
        )
        return None

    endpoint = f"{base_url}{path}"
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        response_payload = response.json() if response.content else {}
        return response_payload if isinstance(response_payload, dict) else {}
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        response_text = str(getattr(response, "text", "") or "").strip()
        response_excerpt = response_text[:200] if response_text else ""
        logger.exception(
            "Failed Valley harness request (path=%s, url=%s, api_key_source=%s, status=%s, response_excerpt=%r)",
            path,
            endpoint,
            api_key_source,
            status_code,
            response_excerpt,
        )
        return None


def notify_valley_run_created(run_id: str) -> bool:
    response_payload = _post_to_valley_harness("/internal/runs", payload={"run_id": run_id})
    if response_payload is None:
        return False

    logger.info(
        "Notified Valley harness for startup update run %s",
        run_id,
        extra={
            "run_id": run_id,
            "job_id": response_payload.get("job_id"),
            "status": response_payload.get("status"),
        },
    )
    return True


def cancel_valley_run(run_id: str) -> dict:
    response_payload = _post_to_valley_harness(f"/internal/runs/{run_id}/cancel", payload=None)
    if response_payload is None:
        return {
            "run_id": run_id,
            "revoke_requested": False,
            "revoke_succeeded": False,
            "revoked_job_ids": [],
            "missing_job_ids": [],
        }

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
