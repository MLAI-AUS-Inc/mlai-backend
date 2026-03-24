import logging

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


def get_valley_harness_api_key() -> str:
    for setting_name in ("VALLEY_HARNESS_API_KEY", "INTERNAL_API_KEY", "ROO_API_KEY", "MLAI_API_KEY"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value
    return ""


def notify_valley_run_created(run_id: str) -> bool:
    base_url = str(getattr(settings, "VALLEY_HARNESS_URL", "") or "").strip().rstrip("/")
    api_key = get_valley_harness_api_key()
    if not base_url or not api_key:
        return False

    try:
        response = requests.post(
            f"{base_url}/internal/runs",
            json={"run_id": run_id},
            headers={"X-API-Key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Failed to notify Valley harness for startup update run %s", run_id)
        return False
