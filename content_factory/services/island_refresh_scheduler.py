"""Daily dispatcher for per-organization content-island refreshes.

Ticked every scheduler-loop pass (see ``run_scheduled_discovery``). Each seedable
organization gets at most one content-factory island-refresh run per org-local
date, after the configured local hour. The ``ContentIslandRefreshDispatch``
unique constraint is the idempotency record, so once today's row exists a tick is
a cheap existence check per org.

The refresh is free (no Roo charge) and sends no notification, so unlike the
discovery loops it can serve cold-start web orgs that have no Slack owner and no
automations - they are exactly the orgs whose graph would otherwise never grow.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from content_factory.models import (
    ContentIsland,
    ContentIslandRefreshDispatch,
    ContentIslandRefreshDispatchStatus,
    OrganizationContentConfig,
    SemanticCluster,
)
from integrations.services.article_generation import (
    _build_content_factory_headers,
    _get_content_factory_base_url,
    _post_content_factory_queue_request,
)
from organizations.models import Organization

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


def _has_pillar_strategy(config) -> bool:
    strategy = getattr(config, "pillar_strategy", None)
    if isinstance(strategy, dict):
        pillars = strategy.get("pillars")
        if isinstance(pillars, list):
            return bool(pillars)
    return bool(strategy)


def _eligible_configs() -> list:
    """Every org that has something to cluster: islands, a pillar strategy, or clusters."""
    configs = list(
        OrganizationContentConfig.objects.select_related("organization").order_by("organization_id")
    )
    island_org_ids = set(
        ContentIsland.objects.values_list("organization_id", flat=True).distinct()
    )
    cluster_org_ids = set(
        SemanticCluster.objects.values_list("organization_id", flat=True).distinct()
    )
    return [
        config
        for config in configs
        if config.organization_id
        and (
            config.organization_id in island_org_ids
            or config.organization_id in cluster_org_ids
            or _has_pillar_strategy(config)
        )
    ]


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
    domain = str(getattr(organization, "domain", "") or "").strip()
    if not domain:
        return {"status": "failed", "error": "Organization has no domain."}

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
    """Dispatch due island refreshes. Safe to tick every loop; never raises per-org."""
    if not island_refresh_scheduler_enabled():
        return {"status": "disabled", "dispatched": 0}

    now = now or timezone.now()
    stuck_failed = _fail_stuck_dispatches(now)
    local_hour = int(getattr(settings, "CONTENT_ISLANDS_REFRESH_LOCAL_HOUR", 6) or 0)
    max_per_tick = int(getattr(settings, "CONTENT_ISLANDS_REFRESH_MAX_PER_TICK", 3) or 0)

    dispatched = existing = not_due = failed = deferred = 0
    results: list = []
    for config in _eligible_configs():
        organization = config.organization
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
        "results": results,
    }


def refresh_islands_for_domain(domain, *, local_date=None, include_expansion=True, force=False) -> dict:
    """Manual/pilot dispatch for one org.

    Bypasses the kill switch and the local-hour gate - invoking it is the explicit
    operator intent. ``force`` additionally re-dispatches when today's dispatch row
    already exists.
    """
    organization = Organization.objects.filter(domain__iexact=str(domain or "").strip()).first()
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
