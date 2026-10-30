"""Daily dispatcher for per-organization content-island refreshes.

Ticked every scheduler-loop pass (see ``run_scheduled_discovery``). Each seedable
organization gets at most one content-factory island-refresh run per org-local
date, after the configured local hour. The ``ContentIslandRefreshDispatch``
unique constraint is the idempotency record, so once today's row exists a tick is
a cheap existence check per org.

The refresh is free (no Roo charge) and sends no notification, so unlike the
discovery loops it serves web orgs that have no Slack owner and no automations.

A refresh reclusters the org's researched keywords, and content-factory fails it
with "No researched keywords" when its load step finds none. So only seedable
orgs (islands, clusters or a pillar strategy) with at least one researched
keyword are dispatched; seedable orgs without keywords are counted as
``skipped_no_keywords`` and never dispatched, failed or denied. A cold-start org
(a pillar strategy, no islands yet) still gets its first islands: the
scheduler seeds them from its pillars itself, which was the refresh's one useful
effect for such an org, and counts them as ``seeded``. It gets its first
keywords from topic discovery (its first topic batch) and is refreshed from the
next due tick after that. Orgs with keywords but nothing seedable stay
ineligible, as before. The dispatch is organization scoped (no website
connection), so an org with keywords and no connection still refreshes. During
scheduled founder workshops (``ARTICLES_WORKSHOP_WINDOWS``) nothing is
dispatched.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from content_factory.models import (
    ContentIsland,
    ContentIslandRefreshDispatch,
    ContentIslandRefreshDispatchStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
    SemanticCluster,
)
from integrations.services.article_generation import (
    _build_content_factory_headers,
    _get_content_factory_base_url,
    _post_content_factory_queue_request,
)
from organizations.models import Organization
from .island_refresh_domain import validate_refresh_domain

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_TIMEZONE = "Australia/Melbourne"
ISLAND_REFRESH_REQUEST_SOURCE = "mlai_backend_scheduler"
ISLAND_REFRESH_ENDPOINT_PATH = "/api/runs/island-refresh"

# Ledger.reference_id / ContentFactoryJob.client_request_id are varchar(100) and
# sqlite will not complain, so the key is clamped here rather than at write time.
IDEMPOTENCY_KEY_MAX_LENGTH = 100
IDEMPOTENCY_KEY_PREFIX = "island-refresh:"

# content-factory reports success by syncing islands back through the bulk view,
# which marks the dispatch completed. Anything still queued well past a refresh's
# runtime lost its callback and must stop looking in-flight.
STUCK_DISPATCH_AGE = timedelta(hours=3)

# Bound the per-tick detail list so the scheduler's stdout stays readable even
# with many orgs; counters remain exact.
MAX_RESULT_ROWS = 20

# A seedable org without keywords is logged once per org-local day, not on
# every 60-second tick.
NO_KEYWORDS_LOG_KEY_PREFIX = "island-refresh:skipped-no-keywords:"
NO_KEYWORDS_LOG_TTL_SECONDS = 26 * 60 * 60


def island_refresh_scheduler_enabled() -> bool:
    return bool(getattr(settings, "CONTENT_ISLANDS_SCHEDULER_ENABLED", False))


def _zone(name) -> ZoneInfo:
    candidate = str(name or "").strip()
    if candidate:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Invalid island refresh timezone %r; using default.", candidate)
    return ZoneInfo(DEFAULT_REFRESH_TIMEZONE)


def build_island_refresh_idempotency_key(domain, local_date) -> str:
    key = f"{IDEMPOTENCY_KEY_PREFIX}{domain}:{local_date.isoformat()}"
    if len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        return key
    tail = f":{local_date.isoformat()}"
    room = IDEMPOTENCY_KEY_MAX_LENGTH - len(IDEMPOTENCY_KEY_PREFIX) - len(tail)
    return f"{IDEMPOTENCY_KEY_PREFIX}{str(domain)[:room]}{tail}"


# Backend bookkeeping (founder-journey alert receipts, offboarded owner) and the
# startup's own profile details (founders, research marker) are not strategy: a
# config holding only these keys has nothing to cluster. A new startup records
# its founder in startup_profile_details, and an offboarded org keeps only
# previous_owner, so neither becomes refresh-eligible on that alone.
_PRIVATE_STRATEGY_KEYS = frozenset({"ops_alerts", "previous_owner", "startup_profile_details"})


def _has_pillar_strategy(config) -> bool:
    strategy = getattr(config, "pillar_strategy", None)
    if isinstance(strategy, dict):
        pillars = strategy.get("pillars")
        if isinstance(pillars, list):
            return bool(pillars)
        return any(key not in _PRIVATE_STRATEGY_KEYS for key in strategy)
    return bool(strategy)


def _seedable(config, *, island_org_ids, cluster_org_ids) -> bool:
    """The pre-keyword rule: islands, clusters or a pillar strategy to cluster around."""
    return bool(
        config.organization_id in island_org_ids
        or config.organization_id in cluster_org_ids
        or _has_pillar_strategy(config)
    )


def has_researched_keywords(config) -> bool:
    """True when content-factory's refresh would load at least one keyword row.

    content-factory's ``_island_refresh_load_context`` reads the org's keyword
    rows from ``GET /api/seo/keywords/`` (``SEOKeywordListView``): every
    ``ResearchedKeyword`` of the organization, unfiltered by status. Islands,
    clusters and pillar strategy are not read as keywords, so they do not count.
    """
    organization_id = getattr(config, "organization_id", None)
    if not organization_id:
        return False
    return ResearchedKeyword.objects.filter(organization_id=organization_id).exists()


def _partition_configs() -> tuple[list, list]:
    """Split seedable orgs into (eligible, skipped_no_keywords).

    Eligible orgs have at least one researched keyword (see
    ``has_researched_keywords``). Seedable orgs without one are skipped: their
    refresh could only complete if DataForSEO expansion happened to write the
    first keywords, and otherwise fails with "No researched keywords".
    """
    configs = list(
        OrganizationContentConfig.objects.select_related("organization").order_by("organization_id")
    )
    island_org_ids = set(
        ContentIsland.objects.values_list("organization_id", flat=True).distinct()
    )
    cluster_org_ids = set(
        SemanticCluster.objects.values_list("organization_id", flat=True).distinct()
    )
    seedable = [
        config
        for config in configs
        if config.organization_id
        and _seedable(config, island_org_ids=island_org_ids, cluster_org_ids=cluster_org_ids)
    ]
    keyword_org_ids = set(
        ResearchedKeyword.objects.filter(
            organization_id__in=[config.organization_id for config in seedable]
        )
        .values_list("organization_id", flat=True)
        .distinct()
    )
    eligible = [config for config in seedable if config.organization_id in keyword_org_ids]
    skipped = [config for config in seedable if config.organization_id not in keyword_org_ids]
    return eligible, skipped


def _eligible_configs() -> list:
    """Every seedable org with researched keywords to recluster."""
    return _partition_configs()[0]


def _seed_cold_start_islands(organization) -> int:
    """Seed an org's first islands from its pillars, as a refresh's load step would."""
    from content_factory.content_islands import seed_islands_from_bootstrap_pillars
    try:
        with transaction.atomic():
            if ContentIsland.objects.filter(organization=organization).exists():
                return 0
            return len(seed_islands_from_bootstrap_pillars(organization) or [])
    except Exception:
        logger.exception("Island seeding failed for %s.", organization.domain)
        return 0


def _log_skipped_no_keywords(config, *, now) -> None:
    organization = config.organization
    local_date = now.astimezone(_zone(config.default_timezone)).date()
    key = f"{NO_KEYWORDS_LOG_KEY_PREFIX}{config.organization_id}:{local_date.isoformat()}"
    try:
        first_today = cache.add(key, 1, NO_KEYWORDS_LOG_TTL_SECONDS)
    except Exception:
        logger.warning("Island refresh skip log dedupe is unavailable.", exc_info=True)
        first_today = True
    if first_today:
        logger.info(
            "island_refresh_skipped reason=no_researched_keywords organization_id=%s domain=%s local_date=%s",
            config.organization_id,
            getattr(organization, "domain", ""),
            local_date.isoformat(),
        )


def _workshop_windows() -> list:
    """Parse ``ARTICLES_WORKSHOP_WINDOWS`` into aware (start, end) pairs, dropping bad entries."""
    raw = getattr(settings, "ARTICLES_WORKSHOP_WINDOWS", None) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            logger.warning("ARTICLES_WORKSHOP_WINDOWS is not valid JSON; no workshop windows apply.")
            return []
    if not isinstance(raw, list):
        logger.warning("ARTICLES_WORKSHOP_WINDOWS must be a JSON list; no workshop windows apply.")
        return []
    windows = []
    for entry in raw:
        start = end = None
        if isinstance(entry, dict):
            try:
                start = parse_datetime(str(entry.get("start") or ""))
                end = parse_datetime(str(entry.get("end") or ""))
            except ValueError:
                start = end = None
        if start is None or end is None:
            logger.warning("Ignoring malformed ARTICLES_WORKSHOP_WINDOWS entry %r.", entry)
            continue
        if timezone.is_naive(start):
            start = start.replace(tzinfo=datetime_timezone.utc)
        if timezone.is_naive(end):
            end = end.replace(tzinfo=datetime_timezone.utc)
        if end <= start:
            logger.warning("Ignoring ARTICLES_WORKSHOP_WINDOWS entry that ends before it starts: %r.", entry)
            continue
        windows.append((start, end))
    return windows


def active_workshop_window(now):
    """The (start, end) workshop window containing ``now``, or None."""
    for start, end in _workshop_windows():
        if start <= now < end:
            return start, end
    return None


def _fail_stuck_dispatches(now) -> int:
    cutoff = now - STUCK_DISPATCH_AGE
    failed = 0
    for dispatch in ContentIslandRefreshDispatch.objects.filter(
        status=ContentIslandRefreshDispatchStatus.QUEUED,
        updated_at__lt=cutoff,
    ):
        dispatch.status = ContentIslandRefreshDispatchStatus.FAILED
        dispatch.last_error = "content-factory never reported the island refresh back"
        dispatch.save(update_fields=["status", "last_error", "updated_at"])
        failed += 1
    return failed


def dispatch_island_refresh(organization, *, local_date, include_expansion=True) -> dict:
    """Queue one island refresh in content-factory. Returns a result dict; never raises."""
    try:
        domain = validate_refresh_domain(getattr(organization, "domain", None))
    except ValueError:
        return {"status": "failed", "error": "A valid organization domain is required."}

    idempotency_key = build_island_refresh_idempotency_key(domain, local_date)
    dispatch, _created = ContentIslandRefreshDispatch.objects.update_or_create(
        organization=organization,
        local_date=local_date,
        defaults={
            "status": ContentIslandRefreshDispatchStatus.QUEUED,
            "idempotency_key": idempotency_key,
            "last_error": "",
        },
    )

    payload = {
        "domain": domain,
        "client_request_id": idempotency_key,
        "request_source": ISLAND_REFRESH_REQUEST_SOURCE,
        "include_expansion": bool(include_expansion),
    }
    endpoint = f"{_get_content_factory_base_url()}{ISLAND_REFRESH_ENDPOINT_PATH}"
    try:
        response = _post_content_factory_queue_request(
            endpoint,
            payload=payload,
            headers=_build_content_factory_headers(),
            operation="island_refresh",
            domain=domain,
        )
    except Exception as exc:
        logger.warning("Island refresh dispatch to content-factory failed for %s: %r", domain, exc)
        return _mark_dispatch_failed(dispatch, str(exc))

    if response.status_code not in {200, 201, 202}:
        return _mark_dispatch_failed(
            dispatch, f"content-factory returned {response.status_code}: {response.text}"
        )

    try:
        body = response.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    run_id = str(body.get("run_id") or body.get("job_id") or "").strip()[:64]
    dispatch.content_factory_run_id = run_id
    dispatch.save(update_fields=["content_factory_run_id", "updated_at"])

    return {
        "status": "dispatched",
        "domain": domain,
        "local_date": local_date.isoformat(),
        "run_id": run_id,
        "idempotency_key": idempotency_key,
    }


def _mark_dispatch_failed(dispatch, error) -> dict:
    dispatch.status = ContentIslandRefreshDispatchStatus.FAILED
    dispatch.last_error = str(error)[:2000]
    dispatch.save(update_fields=["status", "last_error", "updated_at"])
    return {
        "status": "failed",
        "domain": dispatch.organization.domain,
        "local_date": dispatch.local_date.isoformat(),
        "error": str(error),
    }


def run_island_refresh_scheduler(*, now=None) -> dict:
    """Dispatch due island refreshes. Safe to tick every loop; never raises per-org.

    Returns ``{"status": "workshop_window", "dispatched": 0, ...}`` and does
    nothing else while a configured workshop window is open.
    """
    if not island_refresh_scheduler_enabled():
        return {"status": "disabled", "dispatched": 0}

    now = now or timezone.now()
    window = active_workshop_window(now)
    if window is not None:
        return {
            "status": "workshop_window",
            "dispatched": 0,
            "window_start": window[0].isoformat(),
            "window_end": window[1].isoformat(),
        }
    stuck_failed = _fail_stuck_dispatches(now)
    local_hour = int(getattr(settings, "CONTENT_ISLANDS_REFRESH_LOCAL_HOUR", 6) or 0)
    max_per_tick = int(getattr(settings, "CONTENT_ISLANDS_REFRESH_MAX_PER_TICK", 3) or 0)

    dispatched = existing = not_due = failed = deferred = skipped_no_keywords = seeded = 0
    results: list = []
    eligible, skipped = _partition_configs()
    keyword_org_ids = {config.organization_id for config in eligible}
    for config in sorted([*eligible, *skipped], key=lambda item: item.organization_id):
        organization = config.organization
        from integrations.services.daily_research_policy import pause_if_unanswered
        if pause_if_unanswered(organization, now=now):
            continue
        local_now = now.astimezone(_zone(config.default_timezone))
        local_date = local_now.date()
        if local_now.hour < local_hour:
            not_due += 1
            continue
        if ContentIslandRefreshDispatch.objects.filter(
            organization=organization, local_date=local_date
        ).exists():
            existing += 1
            continue
        if organization.pk not in keyword_org_ids:
            # content-factory fails a refresh with NO_KEYWORDS when nothing has
            # been researched, so these orgs failed every night (29 Oct 2026
            # audit). The run's one useful effect for a cold-start org was
            # seeding its first islands from the pillars; do that here and
            # dispatch once research has produced keywords to cluster.
            _log_skipped_no_keywords(config, now=now)
            seeded += _seed_cold_start_islands(organization)
            skipped_no_keywords += 1
            continue
        if max_per_tick and dispatched >= max_per_tick:
            deferred += 1
            continue
        try:
            result = dispatch_island_refresh(organization, local_date=local_date)
        except Exception as exc:
            logger.exception("Island refresh scheduling failed for %s.", organization.domain)
            result = {
                "status": "failed",
                "domain": organization.domain,
                "local_date": local_date.isoformat(),
                "error": str(exc),
            }
        if result.get("status") == "dispatched":
            dispatched += 1
        else:
            failed += 1
        if len(results) < MAX_RESULT_ROWS:
            results.append(result)

    return {
        "status": "ok",
        "dispatched": dispatched,
        "existing": existing,
        "not_due": not_due,
        "deferred": deferred,
        "failed": failed,
        "stuck_failed": stuck_failed,
        "skipped_no_keywords": skipped_no_keywords,
        "seeded": seeded,
        "results": results,
    }


def refresh_islands_for_domain(domain, *, local_date=None, include_expansion=True, force=False) -> dict:
    """Manual/pilot dispatch for one org.

    Bypasses the kill switch and the local-hour gate - invoking it is the explicit
    operator intent. ``force`` additionally re-dispatches when today's dispatch row
    already exists.
    """
    try:
        domain = validate_refresh_domain(domain)
    except ValueError:
        return {"status": "failed", "error": "A valid organization domain is required."}
    organization = Organization.objects.filter(domain__iexact=domain).first()
    if organization is None:
        return {"status": "failed", "error": f"Unknown organization domain: {domain!r}"}
    if local_date is None:
        tz_name = (
            OrganizationContentConfig.objects.filter(organization=organization)
            .values_list("default_timezone", flat=True)
            .first()
        )
        local_date = timezone.now().astimezone(_zone(tz_name)).date()
    if not force and ContentIslandRefreshDispatch.objects.filter(
        organization=organization, local_date=local_date
    ).exists():
        return {
            "status": "existing",
            "domain": organization.domain,
            "local_date": local_date.isoformat(),
        }
    return dispatch_island_refresh(
        organization, local_date=local_date, include_expansion=include_expansion
    )
