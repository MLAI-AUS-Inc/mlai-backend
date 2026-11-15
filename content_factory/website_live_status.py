"""The founder's live site is a status, not a lock (plan steps 7 and 8).

While ``ARTICLES_READY_ON_MERGE_ENABLED`` is on (the code default), a merged
articles setup is Ready, and articles can be written and published, on MLAI's
own proof: the build and exact render MLAI ran on the setup branch, carried to
the merged commit by the post-merge scan (or an accepted CI proof). Whether the
founder's host (Vercel, Netlify and so on) has deployed that commit is observed
here in the background, for every cohort, and reported as ``liveStatus``.

The watch is one ``live-status`` website operation per connection, generation
and verified source. It is checked on a backoff, records the same
``deployment-verify`` receipt a live-URL check records once the reviewed marker
shows on the public articles page, and after ``ALERT_AFTER`` without it moves
to ``needs_attention`` so the stuck-founder alert tells staff. It keeps being
checked after that and closes itself when the site goes live. Nothing here
changes what the founder can do.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

ACTION = "live-status"
# Before this, the owner is told MLAI is still checking; after it, the host help.
HELP_AFTER = timedelta(minutes=30)
# A watch this old without the live marker alerts staff (and only staff).
ALERT_AFTER = timedelta(hours=24)
# Minutes between checks, by number of failed checks; the last repeats.
CHECK_MINUTES = (5, 10, 15, 30, 60, 120)
# A live site is re-checked this often, so a host that stops deploying shows.
LIVE_RECHECK = timedelta(days=1)
SWEEP_LIMIT = 50
SWEEP_INTERVAL_SECONDS = 300
THROTTLE_KEY = "website_live_status:last_sweep"
# The code a watch that passed ALERT_AFTER is recorded under for staff.
NOT_LIVE_CODE = "live_route_unavailable"

LIVE_MESSAGE = "Your articles page is live on your site."
CHECKING_MESSAGE = "Your articles page is merged. We're checking your site for the latest change."
HOST_NAMES = {"vercel": "Vercel", "netlify": "Netlify", "cloudflare": "Cloudflare Pages", "github_pages": "GitHub Pages"}
HOST_HELP = {
    "vercel": "Your host hasn't deployed the latest change yet. Check your Vercel project is connected to this repository.",
    "netlify": "Your host hasn't deployed the latest change yet. Check your Netlify site is connected to this repository.",
    "cloudflare": "Your host hasn't deployed the latest change yet. Check your Cloudflare Pages project is connected to this repository.",
    "github_pages": "Your host hasn't deployed the latest change yet. Check GitHub Pages is turned on for this repository.",
    "unknown": "Your host hasn't deployed the latest change yet. Check your hosting is connected to this repository.",
}
STAFF_TOLD = " Our team has been told and will help."


def enabled() -> bool:
    """True unless ``ARTICLES_READY_ON_MERGE_ENABLED`` is switched off (code default on)."""
    return getattr(settings, "ARTICLES_READY_ON_MERGE_ENABLED", True) is not False


def watch_key(connection, source_sha=None) -> str:
    """The idempotency key of the one watch for this connection, generation and source."""
    return f"{connection.pk}:{ACTION}:{connection.generation}:{source_sha or connection.verified_sha}"


def watch_for(connection, source_sha=None):
    """The live-status watch for the connection's verified source, or ``None``."""
    from .website_models import WebsiteConnectionOperation
    if connection is None or not (source_sha or connection.verified_sha):
        return None
    return WebsiteConnectionOperation.objects.filter(idempotency_key=watch_key(connection, source_sha)).first()


def _stamp(value):
    if isinstance(value, datetime):
        return value
    try:
        stamp = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return stamp if not timezone.is_naive(stamp) else None


def host_from_headers(headers) -> str:
    """Name the host from response header names only; ``unknown`` when unsure."""
    observed = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    if observed.get("server") == "vercel" or "x-vercel-id" in observed:
        return "vercel"
    if "x-nf-request-id" in observed or observed.get("server") == "netlify":
        return "netlify"
    if "cf-ray" in observed and observed.get("server") == "cloudflare":
        return "cloudflare"
    if observed.get("server") == "github.com":
        return "github_pages"
    return "unknown"


def saved_host(connection) -> str:
    """The host a current deployment fingerprint names, else ``unknown``."""
    from .activation import mapping
    from .website_deployment import valid
    hint = mapping(mapping(getattr(connection, "capabilities", None)).get("deploymentFingerprint"))
    provider = hint.get("provider") if valid(hint, connection, connection.verified_sha) else None
    return provider if provider in HOST_HELP else "unknown"


def _probe_host(domain) -> str:
    """Read the home page's response headers once to name the host; never raises."""
    from .website_contract import WebsiteAuthorityError
    from .website_live_fetch import fetch_live_route
    try:
        _, headers = fetch_live_route(f"https://{domain}/", domain, expected_status=200)
    except (WebsiteAuthorityError, OSError, ValueError):
        return "unknown"
    return host_from_headers(headers)


def summary(operation):
    """The persisted watch facts the journey projects, or ``None``."""
    if operation is None:
        return None
    from .activation import mapping
    receipt = mapping(operation.receipt)
    return {"state": operation.state, "startedAt": receipt.get("started_at"), "provider": receipt.get("hosting_provider") or "unknown",
        "checkedAt": receipt.get("checked_at"), "staffAlerted": operation.state == "needs_attention"}


def project(*, deployed, ready, watch=None, now=None):
    """The owner's ``liveStatus``: live, still checking, or waiting with host help.

    ``ready`` is MLAI's own proof that articles can be published. This never
    gates anything; it only says whether the site shows the merged change.
    """
    if deployed:
        return {"status": "live", "message": LIVE_MESSAGE, "provider": (watch or {}).get("provider") or None, "staffAlerted": False}
    if not ready or not enabled():
        return None
    now = now or timezone.now()
    watch = watch or {}
    started = _stamp(watch.get("startedAt"))
    provider = watch.get("provider") if watch.get("provider") in HOST_HELP else "unknown"
    if started is None or now - started < HELP_AFTER:
        return {"status": "checking", "message": CHECKING_MESSAGE, "provider": None, "staffAlerted": False}
    alerted = watch.get("staffAlerted") is True
    return {"status": "waiting", "message": HOST_HELP[provider] + (STAFF_TOLD if alerted else ""),
        "provider": provider if provider != "unknown" else None, "hostName": HOST_NAMES.get(provider),
        "since": started.isoformat(), "staffAlerted": alerted}


def _reserve(connection, *, now, source_sha=None):
    from .website_models import WebsiteConnectionOperation
    source = source_sha or connection.verified_sha
    operation, _ = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=watch_key(connection, source), defaults={
        "connection": connection, "generation": connection.generation, "action": ACTION, "state": "pending",
        "payload": {"source_sha": source}, "next_attempt_at": now,
        "receipt": {"status": "waiting_for_deployment", "source_sha": source, "started_at": now.isoformat(),
            "checks": 0, "repository_modified": False}})
    return operation


def _settle(operation, *, state, receipt, next_at):
    """Save one watch outcome; an alerted watch keeps its alert time for the stuck alert."""
    from .website_models import WebsiteConnectionOperation
    values = {"state": state, "receipt": receipt, "next_attempt_at": next_at}
    if not (operation.state == state == "needs_attention"):
        values["updated_at"] = timezone.now()
    WebsiteConnectionOperation.objects.filter(pk=operation.pk).update(**values)


def raise_staff_alert(connection, *, code, now=None, source_sha=None):
    """Move this source's watch to ``needs_attention`` so the stuck-founder alert tells staff.

    Used by the watch itself and by a preparation whose deployment wait passed
    its deadline. The owner is never sent anywhere; the watch keeps checking.
    """
    now = now or timezone.now()
    operation = _reserve(connection, now=now, source_sha=source_sha)
    if operation.state in {"needs_attention", "completed"}:
        return operation
    from .activation import mapping
    receipt = {**mapping(operation.receipt), "status": "needs_attention", "reason_code": code, "retryable": True,
        "alerted_at": now.isoformat()}
    _settle(operation, state="needs_attention", receipt=receipt, next_at=operation.next_attempt_at or now)
    logger.warning("website_live_status_alert connection=%s operation=%s code=%s", connection.pk, operation.pk, code)
    operation.refresh_from_db()
    return operation


def prepare_alert_source(op) -> str:
    """The watch key part of a preparation's own deadline alert, apart from any source watch."""
    return f"prepare-{op.pk}"


def close_prepare_alert(op):
    """Close a preparation's deadline alert once that preparation is Ready."""
    from .website_models import WebsiteConnectionOperation
    WebsiteConnectionOperation.objects.filter(idempotency_key=watch_key(op.connection, prepare_alert_source(op)),
        state="needs_attention").update(state="completed", next_attempt_at=None, updated_at=timezone.now())


def observe(config, *, now=None) -> str:
    """Check one website once when its watch is due. Returns what happened, never raises for the host.

    ``skipped`` (not Ready on MLAI's proof, or nothing to watch), ``not_due``,
    ``live`` or ``waiting``.
    """
    from .activation import durable_integration_evidence, live_deployment_verified, mapping
    from .website_contract import WebsiteAuthorityError
    from .website_live_verification import applicable, build_proof
    now = now or timezone.now()
    connection = getattr(config, "website_connection", None)
    if connection is None or connection.state != "connected" or not connection.verified_sha:
        return "skipped"
    if durable_integration_evidence(config, connection, now=now).get("verified") is not True:
        return "skipped"
    target = connection.targets.filter(generation=connection.generation, target_key=config.default_publish_target_id).first()
    if target is None or not applicable(target) or not build_proof(target):
        return "skipped"
    operation = watch_for(connection)
    if operation is not None and operation.next_attempt_at and operation.next_attempt_at > now:
        return "not_due"
    if live_deployment_verified(connection, target, now=now):
        if operation is not None and operation.state != "completed":
            receipt = {**mapping(operation.receipt), "status": "live", "live_at": now.isoformat()}
            _settle(operation, state="completed", receipt=receipt, next_at=now + LIVE_RECHECK)
        return "live"
    operation = operation or _reserve(connection, now=now)
    receipt = dict(mapping(operation.receipt))
    if operation.state == "completed":
        # It was live and is not any more (a newer marker, or the receipt aged out):
        # watch again from now.
        receipt.update(status="waiting_for_deployment", started_at=now.isoformat(), checks=0)
        receipt.pop("live_at", None)
        operation.state = "pending"
    from .website_verification import verify_live_url_deployment
    try:
        verify_live_url_deployment(config, target, renew=False)
    except WebsiteAuthorityError as exc:
        checks = int(receipt.get("checks") or 0) + 1
        if not receipt.get("hosting_provider") or receipt["hosting_provider"] == "unknown":
            provider = saved_host(connection)
            receipt["hosting_provider"] = provider if provider != "unknown" else _probe_host(connection.organization.domain)
        receipt.update(checks=checks, checked_at=now.isoformat(), last_code=exc.code)
        started = _stamp(receipt.get("started_at")) or now
        state = operation.state if operation.state in {"pending", "needs_attention"} else "pending"
        if state == "pending" and now - started >= ALERT_AFTER:
            state = "needs_attention"
            receipt.update(status="needs_attention", reason_code=NOT_LIVE_CODE, retryable=True, alerted_at=now.isoformat())
            logger.warning("website_live_status_alert connection=%s operation=%s code=%s", connection.pk, operation.pk, exc.code)
        _settle(operation, state=state, receipt=receipt,
            next_at=now + timedelta(minutes=CHECK_MINUTES[min(checks - 1, len(CHECK_MINUTES) - 1)]))
        return "waiting"
    receipt.update(status="live", live_at=now.isoformat(), checked_at=now.isoformat())
    _settle(operation, state="completed", receipt=receipt, next_at=now + LIVE_RECHECK)
    logger.info("website_live_status_live connection=%s operation=%s", connection.pk, operation.pk)
    return "live"


def run_live_status_sweep(now=None, limit=None) -> dict:
    """Check every Ready website whose live status is due, at most one bounded pass every 5 minutes."""
    if not enabled():
        return {"status": "disabled"}
    from django.core.cache import cache
    now = now or timezone.now()
    if not cache.add(THROTTLE_KEY, now.isoformat(), SWEEP_INTERVAL_SECONDS):
        return {"status": "throttled"}
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnectionOperation
    from django.db.models import Exists, OuterRef
    from django.db.models.fields.json import KT
    not_due = WebsiteConnectionOperation.objects.alias(source=KT("payload__source_sha")).filter(
        connection_id=OuterRef("website_connection_id"), generation=OuterRef("website_connection__generation"), action=ACTION,
        source=OuterRef("website_connection__verified_sha"), next_attempt_at__gt=now)
    configs = (OrganizationContentConfig.objects.select_related("website_connection__organization")
        .filter(website_connection__state="connected", website_connection__capabilities__publishingReady=True)
        .exclude(default_publish_target_id="").exclude(website_connection__verified_sha="")
        .exclude(Exists(not_due)).order_by("-website_connection__last_verified_at")[: limit or SWEEP_LIMIT])
    counts = {"live": 0, "waiting": 0, "skipped": 0, "not_due": 0, "errors": 0}
    for config in configs:
        try:
            counts[observe(config, now=now)] += 1
        except Exception:  # noqa: BLE001 - one website must never stop the next
            logger.warning("website_live_status_failed config=%s", config.pk, exc_info=True)
            counts["errors"] += 1
    return {"status": "completed", **counts}
