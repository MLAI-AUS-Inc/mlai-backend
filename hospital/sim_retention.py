"""Daily scheduler hook for Health Hack conversation retention."""

from __future__ import annotations

from io import StringIO
import logging
import secrets

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone


logger = logging.getLogger(__name__)
CACHE_PREFIX = "health-hack:conversation-retention:v1"


def run_scheduled_sim_conversation_cleanup(*, now=None) -> dict:
    """Run cleanup once per local day, even though the scheduler ticks minutely."""

    now = now or timezone.now()
    local_day = timezone.localtime(now).date().isoformat()
    done_key = f"{CACHE_PREFIX}:done:{local_day}"
    lock_key = f"{CACHE_PREFIX}:lock:{local_day}"
    owner = secrets.token_urlsafe(16)
    if cache.get(done_key):
        return {"status": "skipped", "reason": "already_completed", "date": local_day}
    if not cache.add(lock_key, owner, timeout=60 * 60):
        return {"status": "skipped", "reason": "already_running", "date": local_day}

    output = StringIO()
    try:
        call_command("cleanup_sim_conversations", stdout=output)
        cache.set(done_key, True, timeout=3 * 24 * 60 * 60)
        return {"status": "completed", "date": local_day}
    except Exception:
        logger.exception("Scheduled Health Hack conversation cleanup failed")
        raise
    finally:
        # The one-hour TTL is the crash safety net; ownership prevents a late
        # process from deleting a replacement lock.
        if cache.get(lock_key) == owner:
            cache.delete(lock_key)
