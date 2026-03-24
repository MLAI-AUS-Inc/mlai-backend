import logging

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


def notify_valley_run_created(run_id: str) -> bool:
    base_url = str(getattr(settings, "VALLEY_HARNESS_URL", "") or "").strip().rstrip("/")
    api_key = str(getattr(settings, "VALLEY_HARNESS_API_KEY", "") or "").strip()
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
