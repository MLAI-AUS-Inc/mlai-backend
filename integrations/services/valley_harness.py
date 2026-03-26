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


def notify_valley_run_created(run_id: str) -> bool:
    base_url = str(getattr(settings, "VALLEY_HARNESS_URL", "") or "").strip().rstrip("/")
    api_key, api_key_source = _get_valley_harness_api_key_with_source()
    if not base_url:
        logger.warning(
            "Skipping Valley harness notification for startup update run %s because VALLEY_HARNESS_URL is not configured",
            run_id,
        )
        return False
    if not api_key:
        logger.warning(
            "Skipping Valley harness notification for startup update run %s because no service API key is configured",
            run_id,
        )
        return False

    endpoint = f"{base_url}/internal/runs"
    try:
        response = requests.post(
            endpoint,
            json={"run_id": run_id},
            headers={"X-API-Key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        logger.info(
            "Notified Valley harness for startup update run %s (url=%s, api_key_source=%s, status=%s)",
            run_id,
            endpoint,
            api_key_source,
            response.status_code,
        )
        return True
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        response_text = str(getattr(response, "text", "") or "").strip()
        response_excerpt = response_text[:200] if response_text else ""
        logger.exception(
            "Failed to notify Valley harness for startup update run %s (url=%s, api_key_source=%s, status=%s, response_excerpt=%r)",
            run_id,
            endpoint,
            api_key_source,
            status_code,
            response_excerpt,
        )
        return False
