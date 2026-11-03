"""Cheap request counters for the P2 staff alert rules (IF-P2-19).

The global alert rules (founder rate limits, sign-in refresh failures, GitHub
callback failures) have no run to hang a receipt on, so they read counts kept
here in 10-minute cache buckets. Counting costs one cache increment and never
touches the database. Nothing is counted while ``ARTICLES_STAFF_ALERT_RULES_ENABLED``
is off, so this module changes nothing on deploy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

BUCKET_SECONDS = 600
# Long enough for the 7-day refresh-failure baseline plus a day of slack.
BUCKET_TTL_SECONDS = 8 * 24 * 60 * 60
KEY_PREFIX = "staff_signals"

FOUNDER_THROTTLED = "founder_throttled"
REFRESH_FAILED = "refresh_failed"
MY_STARTUP_PREFIX = "/api/v1/my-startup/"
REFRESH_PATH = "/api/v1/community-chat/auth/session/refresh/"


def enabled() -> bool:
    """``ARTICLES_STAFF_ALERT_RULES_ENABLED``; off by default."""
    return bool(getattr(settings, "ARTICLES_STAFF_ALERT_RULES_ENABLED", False))


def _bucket(moment: datetime) -> int:
    return int(moment.timestamp()) // BUCKET_SECONDS


def bucket_key(name: str, bucket: int) -> str:
    """The cache key of one 10-minute bucket of ``name``."""
    return f"{KEY_PREFIX}:{name}:{bucket}"


def count(name: str, *, now: datetime | None = None, amount: int = 1) -> None:
    """Add ``amount`` to the current 10-minute bucket of ``name``. Never raises.

    Does nothing while the staff alert rules are off. An existing bucket costs
    one cache increment; the first event in a bucket adds it with its TTL.
    """
    if not enabled() or not isinstance(name, str) or not name:
        return
    key = bucket_key(name, _bucket(now or timezone.now()))
    try:
        cache.incr(key, amount)
        return
    except ValueError:
        pass
    except Exception:  # noqa: BLE001 - a cache outage must never break a request
        logger.warning("staff_signal_count_failed name=%s", name, exc_info=True)
        return
    try:
        if not cache.add(key, amount, BUCKET_TTL_SECONDS):
            cache.incr(key, amount)
    except Exception:  # noqa: BLE001 - a cache outage must never break a request
        logger.warning("staff_signal_count_failed name=%s", name, exc_info=True)


def total(name: str, *, window: timedelta, now: datetime | None = None) -> int:
    """The count of ``name`` over the buckets that overlap the last ``window``."""
    end = _bucket(now or timezone.now())
    buckets = max(1, -(-int(window.total_seconds()) // BUCKET_SECONDS))
    keys = [bucket_key(name, bucket) for bucket in range(end - buckets + 1, end + 1)]
    try:
        values = cache.get_many(keys)
    except Exception:  # noqa: BLE001 - a cache outage reads as no events
        logger.warning("staff_signal_read_failed name=%s", name, exc_info=True)
        return 0
    return sum(value for value in values.values() if isinstance(value, int))


def signal_for(path: str, status_code: int) -> str | None:
    """The signal a response counts as, or None. A pure function of path and status."""
    if not isinstance(path, str):
        return None
    if status_code == 429 and path.startswith(MY_STARTUP_PREFIX):
        return FOUNDER_THROTTLED
    if path == REFRESH_PATH and 400 <= status_code < 500:
        return REFRESH_FAILED
    return None


class ThrottleSignalMiddleware:
    """Counts founder 429s on My Startup and sign-in refresh failures. No database access."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if enabled():
            signal = signal_for(getattr(request, "path", ""), getattr(response, "status_code", 0))
            if signal is not None:
                count(signal)
        return response
