"""Daily cleanup hook for terminal Roo-Founder Tools link requests."""

from __future__ import annotations

import logging
import secrets

from django.core.cache import cache
from django.utils import timezone

from core.slack_founder_links import purge_stale_slack_founder_link_requests


logger = logging.getLogger(__name__)
CACHE_PREFIX = "roo:slack-founder-link-retention:v1"


def run_scheduled_slack_founder_link_request_cleanup(*, now=None) -> dict:
    """Purge stale requests once per Melbourne-local day."""
    current_time = now or timezone.now()
    local_day = timezone.localtime(current_time).date().isoformat()
    done_key = f"{CACHE_PREFIX}:done:{local_day}"
    lock_key = f"{CACHE_PREFIX}:lock:{local_day}"
    owner = secrets.token_urlsafe(16)

    if cache.get(done_key):
        return {
            "status": "skipped",
            "reason": "already_completed",
            "date": local_day,
        }
    if not cache.add(lock_key, owner, timeout=60 * 60):
        return {
            "status": "skipped",
            "reason": "already_running",
            "date": local_day,
        }

    try:
        deleted = purge_stale_slack_founder_link_requests(now=current_time)
        cache.set(done_key, True, timeout=3 * 24 * 60 * 60)
        logger.info(
            "slack_founder_link_retention status=completed date=%s deleted=%s",
            local_day,
            deleted,
        )
        return {"status": "completed", "date": local_day, "deleted": deleted}
    except Exception:
        logger.exception("Scheduled Slack-Founder link request cleanup failed")
        raise
    finally:
        if cache.get(lock_key) == owner:
            cache.delete(lock_key)
