"""Source sync: one coalescing source check per website that chases GitHub's head (plan step B13b).

Before this, every push queued its own ``source-reverify`` check. In a push
storm each older check failed with ``website_source_changed`` once GitHub's
branch moved, a missed webhook left a website on an old head until someone
clicked, and a push that changed the articles setup needed a rescan that ran
as the founder: no ``authorized_by`` or too few Roo points stopped it on
``roo_points_required`` forever.

With ``ARTICLES_SOURCE_SYNC_ENABLED`` (a staged rollout, default off):

- **Coalesce.** A new push check supersedes the older pending push checks of
  the same connection generation, and waits a 60-second quiet period, so five
  pushes in two minutes settle on one verified head. Same-source refreshes
  (``purpose`` set) are left alone.
- **Converge.** A check that finds GitHub's branch moved on reads the head at
  once and records it as a push, so the newest commit always gets a check,
  even when that push's webhook never arrived.
- **Service rescan.** The rescan a changed setup needs runs as MLAI's own
  ``source_sync`` scheduler principal (D5: MLAI pays). It is the same
  read-only scan request as before (no setup, preview or components), keeps
  every installation and connection check of ``authority_guard``, is free
  (``roo_points_cost`` 0, ``billing_status`` free) and carries no founder
  admission, so it can never charge or refund a founder.
- **Daily cap.** At most ``ARTICLES_SOURCE_SYNC_DAILY_SCAN_CAP`` service
  rescans per connection in any 24 hours; a capped check waits and retries.
- **Hourly sweep.** Every connected or paused website's head is read at most
  once an hour; a head no push recorded is handled as that push.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SERVICE = "source_sync"
QUIET_PERIOD = timedelta(seconds=60)
DAY = timedelta(hours=24)
SWEEP_INTERVAL_SECONDS = 3600
SWEEP_LIMIT = 20
SWEEP_KEY = "website-source-sync:head:{connection_id}"
# Receipt marker of a check whose rescan ran as the service, counted for the cap.
SERVICE_RESCAN_KEY = "service_rescan"


def enabled() -> bool:
    """ARTICLES_SOURCE_SYNC_ENABLED, default off."""
    return getattr(settings, "ARTICLES_SOURCE_SYNC_ENABLED", False) is True


def daily_cap() -> int:
    """Service rescans one connection may run in any 24 hours."""
    value = getattr(settings, "ARTICLES_SOURCE_SYNC_DAILY_SCAN_CAP", 6)
    return value if type(value) is int and value >= 0 else 6


def _push_checks(connection, generation):
    from .website_models import WebsiteConnectionOperation
    return (WebsiteConnectionOperation.objects.filter(connection=connection, generation=generation,
        action="source-reverify", state="pending").exclude(payload__has_key="purpose"))


def coalesce(connection, op, *, now=None) -> int:
    """Inside the push lock: supersede older pending push checks and start ``op``'s quiet period.

    Returns how many older checks were superseded. A superseded check never
    retries; its rescan, if one is in flight, finishes as ordinary history.
    """
    if not enabled():
        return 0
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    older = _push_checks(connection, op.generation).exclude(pk=op.pk).exclude(payload__source_sha=op.payload.get("source_sha"))
    superseded = 0
    for row in older.select_for_update():
        row.state = "cancelled"
        row.next_attempt_at = None
        row.receipt = {**(row.receipt or {}), "status": "superseded", "retryable": False,
            "superseded_by_source_sha": op.payload.get("source_sha"), "superseded_by_operation_id": str(op.pk)}
        row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
        superseded += 1
    WebsiteConnectionOperation.objects.filter(pk=op.pk, state="pending").update(
        next_attempt_at=now + QUIET_PERIOD, updated_at=now)
    return superseded


def service_rescans_today(connection, *, now, exclude_pk=None) -> int:
    """Service rescans this connection started in the last 24 hours."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, action="source-reverify",
        created_at__gte=now - DAY, **{f"receipt__{SERVICE_RESCAN_KEY}": True})
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    return rows.count()


def cap_reached(connection, *, now, exclude_pk=None) -> bool:
    """Whether one more service rescan would pass the per-connection daily cap."""
    return service_rescans_today(connection, now=now, exclude_pk=exclude_pk) >= daily_cap()


def service_scan_payload(binding, website, *, request_id, owner=None) -> dict:
    """The read-only rescan request MLAI's service sends: no setup, preview, components or charge."""
    from .billing import build_roo_points_authorization_payload
    from .contract import CONTENT_FACTORY_REQUEST_SOURCE
    actor = ""
    if owner is not None:
        # Attribution only; consent and payment come from the service, not the founder.
        from .vibe_marketing_views import founder_actor_id_for_user
        actor = founder_actor_id_for_user(owner)
    payload = {**binding, "client_request_id": request_id, "force_refresh": True,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE, "slack_user_id": actor,
        "scan_purpose": "setup", "scaffold_if_missing": False, "auto_setup_preview": False,
        "generate_components": False, "source_sync_service": SERVICE}
    payload.update(build_roo_points_authorization_payload(domain=website.organization.domain, action="repo_scan",
        cost_points=0, required_points=0, current_balance=None, billing_status="free"))
    return payload


def reserve_service_dispatch(payload, website):
    """Reserve the rescan under the ``source_sync`` scheduler principal, never a founder."""
    from .dispatch_admission import reserve_scheduler_dispatch
    return reserve_scheduler_dispatch(payload=payload, workflow="repo_scan", organization=website.organization,
        service=SERVICE)


def read_head(connection) -> str:
    """GitHub's current head of the selected branch, or "" when it cannot be read."""
    from urllib.parse import quote
    from .website_contract import SHA_PATTERN
    if not connection.github_repo or not connection.branch:
        return ""
    try:
        from integrations.services.github_connections import read_bound_repository
        response = read_bound_repository(connection,
            f"https://api.github.com/repos/{connection.github_repo}/commits/{quote(connection.branch, safe='')}")
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.info("website_source_sync_head_unavailable connection=%s error_type=%s", connection.pk, type(exc).__name__)
        return ""
    sha = str(body.get("sha") or "") if isinstance(body, dict) else ""
    return sha.lower() if SHA_PATTERN.fullmatch(sha) and set(sha) != {"0"} else ""


def sync_head(connection) -> dict:
    """Read GitHub's head once and, if no push recorded it, handle it as that push.

    Runs outside every lock. The push handler re-checks state, repository and
    branch under its own lock and records each head once, so a webhook that
    arrives at the same time changes nothing.
    """
    from .website_connections import observed_source_sha
    if connection.state not in {"connected", "paused"} or not isinstance(connection.repository_id, int):
        return {"status": "skipped"}
    head = read_head(connection)
    if not head:
        return {"status": "unavailable"}
    if head in {observed_source_sha(connection), connection.verified_sha}:
        return {"status": "current", "head": head}
    from .website_reconciliation import handle_website_github_event
    result = handle_website_github_event("push", {"repository": {"id": connection.repository_id},
        "ref": f"refs/heads/{connection.branch}", "after": head, "source_sync": True})
    logger.info("website_source_sync_head_recorded connection=%s head=%s", connection.pk, head)
    return {"status": "recorded", "head": head, "push": result}


def after_superseded(op) -> None:
    """A check that found the branch moved on makes sure the newer head has its own check."""
    if not enabled():
        return
    from .website_models import WebsiteConnection
    connection = WebsiteConnection.objects.select_related("organization").filter(pk=op.connection_id).first()
    if connection is None or connection.generation != op.generation:
        return
    if _push_checks(connection, connection.generation).exclude(pk=op.pk).exists():
        return  # The newer push already queued its check.
    try:
        sync_head(connection)
    except Exception:
        logger.exception("website_source_sync_converge_failed connection=%s operation=%s", connection.pk, op.pk)


def sweep(*, now=None, limit=SWEEP_LIMIT, connection_id=None) -> dict:
    """Hourly head read for every connected or paused website; heals missed push webhooks."""
    if not enabled():
        return {"status": "disabled"}
    from django.core.cache import cache
    from .website_models import WebsiteConnection
    rows = (WebsiteConnection.objects.select_related("organization")
        .filter(state__in=["connected", "paused"], repository_id__isnull=False).exclude(branch=""))
    if connection_id:
        rows = rows.filter(pk=connection_id)
    read, recorded = 0, 0
    for connection in rows.order_by("updated_at")[:max(1, limit) * 5]:
        if read >= limit:
            break
        if _push_checks(connection, connection.generation).exists():
            continue  # A check is already chasing this website.
        if not cache.add(SWEEP_KEY.format(connection_id=connection.pk), "1", SWEEP_INTERVAL_SECONDS):
            continue
        read += 1
        try:
            outcome = sync_head(connection)
        except Exception:
            logger.exception("website_source_sync_sweep_failed connection=%s", connection.pk)
            continue
        recorded += int(outcome.get("status") == "recorded")
    return {"status": "completed", "read": read, "recorded": recorded}
