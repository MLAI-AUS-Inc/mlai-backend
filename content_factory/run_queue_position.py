"""Interactive queue class for founder-facing starts, and queue position for queued runs.

Two halves of one contract with content-factory:

* ``interactive_queue_class`` decides which starts ask content-factory for its
  ``interactive`` Celery queue: startup research (autofill) and work covered by
  the free first article (its first topic batch and the article itself). Every
  other start, and every nightly island refresh, keeps the default queue.
  content-factory only honours the hint while an interactive worker is
  heartbeating and otherwise falls back to ``default``, so sending it is safe
  before that pool is deployed.
* ``queue_position_payload`` turns the mirrored ``queue_name``/``queued_at`` of
  a queued run into the founder-facing position ("You're 3rd in line") and, once
  enough completed samples exist, an ETA.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max, Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from content_factory.billing import (
    CONTENT_FACTORY_ACTION_ARTICLE_GENERATION,
    CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
)

logger = logging.getLogger(__name__)

INTERACTIVE_QUEUE_CLASS = "interactive"
STARTUP_AUTOFILL_ACTION = "startup_autofill"
TOPIC_DISCOVERY_ACTION = "topic_discovery"
ENTITLED_BILLING_STATUS = "entitled"
FIRST_ARTICLE_ENTITLEMENT = "first_article"
# Gate actions an entitled first-article lineage is billed under (IF-01/IF-02).
ENTITLED_INTERACTIVE_ACTIONS = frozenset({
    CONTENT_FACTORY_ACTION_ARTICLE_GENERATION,
    CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
    TOPIC_DISCOVERY_ACTION,
})
# The nightly island refresh posts with this source. content-factory's
# RunIslandRefreshRequest forbids unknown fields, so it must never get a class.
ISLAND_REFRESH_REQUEST_SOURCE = "mlai_backend_scheduler"

QUEUED_STATUS = "queued"
COMPLETED_STATUS = "completed"
# Only runs queued this recently count as ahead of you; anything older is
# stuck rather than waiting, and must not inflate the line.
QUEUE_POSITION_WINDOW = timedelta(hours=2)
QUEUE_POSITION_MAX_CANDIDATES = 500
ETA_MIN_SAMPLES = 5
ETA_SAMPLE_WINDOW = timedelta(days=7)
ETA_MAX_SAMPLES = 50
# Status polls serialize every queued run every few seconds, so the median is
# cached per queue rather than re-aggregated on each poll.
ETA_MEDIAN_CACHE_PREFIX = "content-factory:queue-eta-median:"
ETA_MEDIAN_CACHE_SECONDS = 60


def interactive_queue_class(payload) -> Optional[str]:
    """Return ``"interactive"`` for founder-facing starts that should not wait behind batch work.

    Interactive starts are startup autofill, and runs billed under the free
    first-article entitlement (its first topic batch and the article, including
    that article's own restarts and revisions). Returns ``None`` for everything
    else, and always for island refresh payloads.
    """
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("workflow") == "island_refresh"
        or payload.get("request_source") == ISLAND_REFRESH_REQUEST_SOURCE
        or "island_research_brief" in payload
    ):
        return None
    action = str(payload.get("roo_points_action") or "").strip()
    if action == STARTUP_AUTOFILL_ACTION or payload.get("workflow") == "startup_autofill":
        return INTERACTIVE_QUEUE_CLASS
    if (
        str(payload.get("roo_points_billing_status") or "").strip() == ENTITLED_BILLING_STATUS
        and str(payload.get("roo_points_entitlement") or "").strip() == FIRST_ARTICLE_ENTITLEMENT
        and action in ENTITLED_INTERACTIVE_ACTIONS
    ):
        return INTERACTIVE_QUEUE_CLASS
    return None


def with_interactive_queue_class(payload):
    """Return the request body to POST: ``payload`` plus ``queue_class`` when it is interactive.

    The caller's dict is never mutated, so saved run requests and outbox
    envelopes keep their original identity. Non-dict payloads pass through.
    """
    queue_class = interactive_queue_class(payload)
    if queue_class is None:
        return payload
    return {**payload, "queue_class": queue_class}


def _parse_queued_at(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = parse_datetime(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def _queue_metadata(result) -> tuple[str, Optional[datetime]]:
    """Read the mirrored queue name and enqueue time from a run result."""
    result = result if isinstance(result, dict) else {}
    queue_name = str(result.get("queue_name") or "").strip()
    queued_at = _parse_queued_at(result.get("queued_at"))
    return queue_name, queued_at


def interactive_queue_slots() -> int:
    """Concurrent runs the queue serves at once, for the ETA. 0 turns the ETA off."""
    try:
        return max(0, int(getattr(settings, "CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS", 0) or 0))
    except (TypeError, ValueError):
        logger.warning("Invalid CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS; queue ETA is off.")
        return 0


def _cached_median_run_seconds(queue_name: str, *, now: datetime) -> Optional[float]:
    """``_median_run_seconds`` cached per queue for ``ETA_MEDIAN_CACHE_SECONDS``.

    The cached value is wrapped so "too few samples" (None) is cached too. A
    cache outage falls back to computing the median directly.
    """
    key = f"{ETA_MEDIAN_CACHE_PREFIX}{queue_name}"
    try:
        cached = cache.get(key)
    except Exception:
        logger.warning("Queue ETA median cache is unavailable; computing it directly.", exc_info=True)
        return _median_run_seconds(queue_name, now=now)
    if isinstance(cached, dict) and "median" in cached:
        return cached["median"]
    median = _median_run_seconds(queue_name, now=now)
    try:
        cache.set(key, {"median": median}, ETA_MEDIAN_CACHE_SECONDS)
    except Exception:
        logger.warning("Queue ETA median cache is unavailable; not caching it.", exc_info=True)
    return median


def _median_run_seconds(queue_name: str, *, now: datetime) -> Optional[float]:
    """Median working time of recently completed runs on ``queue_name``, or None below the sample floor."""
    from workflow_runs.models import ContentFactoryRun

    rows = (
        ContentFactoryRun.objects.filter(
            status=COMPLETED_STATUS,
            result__queue_name=queue_name,
            updated_at__gte=now - ETA_SAMPLE_WINDOW,
        )
        .annotate(first_started=Min("steps__started_at"), last_completed=Max("steps__completed_at"))
        .order_by("-updated_at")
        .values_list("first_started", "last_completed")[:ETA_MAX_SAMPLES]
    )
    durations = [
        (finished - started).total_seconds()
        for started, finished in rows
        if started and finished and finished > started
    ]
    if len(durations) < ETA_MIN_SAMPLES:
        return None
    return statistics.median(durations)


def queue_position_payload(run, *, now=None) -> dict:
    """Founder-facing place in line for a queued run; empty for any other run.

    While ``run.status`` is ``queued`` and content-factory has mirrored its
    ``queue_name`` and ``queued_at``, returns ``queuePosition`` (1-based),
    ``queueAhead``, ``queueEtaSeconds`` (null when the ETA is off, below the
    sample floor, or for the run at the front of the line) and
    ``queueObservedAt``. Runs ahead are other queued runs on the same queue with
    an earlier ``queued_at`` inside the last two hours.
    """
    if str(getattr(run, "status", "") or "") != QUEUED_STATUS:
        return {}
    queue_name, queued_at = _queue_metadata(getattr(run, "result", None))
    if not queue_name or queued_at is None:
        return {}

    from workflow_runs.models import ContentFactoryRun

    now = now or timezone.now()
    window_start = now - QUEUE_POSITION_WINDOW
    # A queued run's mirror is written at or after its enqueue, so updated_at
    # bounds queued_at from above: this prefilter keeps every candidate.
    candidates = (
        ContentFactoryRun.objects.filter(
            status=QUEUED_STATUS,
            result__queue_name=queue_name,
            updated_at__gte=window_start,
        )
        .exclude(run_id=getattr(run, "run_id", None))
        .values_list("result", flat=True)[:QUEUE_POSITION_MAX_CANDIDATES]
    )
    ahead = 0
    for result in candidates:
        _name, other_queued_at = _queue_metadata(result)
        if other_queued_at is not None and window_start <= other_queued_at < queued_at:
            ahead += 1

    # The front of the line has no wait to estimate: its ETA stays null (the
    # client then shows only the place in line), and no median is read for it.
    eta_seconds = None
    slots = interactive_queue_slots()
    if slots and ahead:
        median = _cached_median_run_seconds(queue_name, now=now)
        if median is not None:
            eta_seconds = int(math.ceil(ahead / slots) * median)

    return {
        "queuePosition": ahead + 1,
        "queueAhead": ahead,
        "queueEtaSeconds": eta_seconds,
        "queueObservedAt": now.isoformat(),
    }
