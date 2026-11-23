"""Durable, read-only baseline builds for explicitly connected repositories."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from . import repo_health
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_source_sync import read_head

logger = logging.getLogger(__name__)
ACTION = "repo-health"
LEASE = timedelta(minutes=2)
CALLBACK_WAIT = timedelta(minutes=30)
RECONCILE_AFTER = timedelta(hours=6)


class ProbeError(ValueError):
    """A safe machine-readable reason for refusing or deferring a probe."""

    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.status = status


def _identity(site):
    return {
        "website_connection_id": str(site.pk), "connection_generation": site.generation,
        "organization_id": site.organization_id, "domain": site.organization.domain,
        "repository_id": site.repository_id, "github_repo": site.github_repo,
        "installation_id": site.installation_id, "branch": site.branch, "app_root": site.app_root,
    }


def queue(site, sha=""):
    """Reserve one free baseline for a connection scope and head. Empty SHA resolves GitHub first."""
    if not repo_health.enabled() or site.state != "connected" or not site.repository_id:
        return
    if sha and (not repo_health._sha(sha) or sha == "0" * 40):
        return
    with transaction.atomic():
        current = WebsiteConnection.objects.select_for_update().select_related("organization").filter(
            pk=site.pk, generation=site.generation, state="connected").first()
        if not current or not _selected(current):
            return
        identity = _identity(current)
        scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        key = f"repo-health:{scope}:{sha or 'head'}"
        previous = WebsiteConnectionOperation.objects.filter(idempotency_key=key).first()
        # A branch can return to a previous commit. Reserve a fresh identity only
        # if its earlier execution was fenced or its settled result was displaced.
        if previous:
            displaced = (sha and previous.state == "completed" and
                         repo_health.stored(current).get("build", {}).get("commit_sha") != sha)
            if previous.state != "superseded" and not displaced:
                return
            previous.state, previous.idempotency_key = "superseded", f"repo-health:retired:{previous.pk}"
            previous.save(update_fields=("state", "idempotency_key", "updated_at"))
        WebsiteConnectionOperation.objects.create(idempotency_key=key, action=ACTION,
            generation=current.generation, connection=current,
            payload={**identity, "expected_source_sha": sha, "dispatch_nonce": uuid4().hex})


def _selected(site):
    return site.state == "connected" and OrganizationContentConfig.objects.filter(
        organization_id=site.organization_id, website_connection_id=site.pk).exists()


def _matches(site, op):
    stored = op.payload
    return _selected(site) and all(stored.get(k) == v for k, v in _identity(site).items())


def _wire(op):
    return {"probe_id": str(op.pk), **op.payload}


def _reschedule(op, now, *, delay, reason):
    WebsiteConnectionOperation.objects.filter(pk=op.pk, attempts=op.attempts, state="running").update(
        state="pending", next_attempt_at=now + delay, receipt={"status": reason})


def advance(identifier, now):
    """Claim one dispatch. No database lock is held during GitHub or worker network calls."""
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
        if not op or op.state not in {"pending", "running"} or op.next_attempt_at and op.next_attempt_at > now:
            return
        op.state, op.attempts, op.next_attempt_at = "running", op.attempts + 1, now + LEASE
        op.save(update_fields=("state", "attempts", "next_attempt_at", "updated_at"))
    site = WebsiteConnection.objects.select_related("organization").get(pk=op.connection_id)
    working = WebsiteConnectionOperation.objects.filter(pk=op.pk, state="running", attempts=op.attempts)
    if not _matches(site, op):
        working.update(state="superseded", next_attempt_at=None)
        return
    head = read_head(site)
    expected = op.payload["expected_source_sha"]
    if head and head != expected:
        if not expected:
            # Initial connection (or lazy enrollment) also checks the host and sitemap.
            # A missing optional grant or a provider outage only affects the observations.
            try:
                repo_health.refresh_host(site, now=now)
            except Exception as exc:
                logger.warning("repo_health_host_probe_unavailable connection=%s type=%s", site.pk, type(exc).__name__)
        with transaction.atomic():
            queue(site, head)
            working.update(state="superseded" if expected else "completed", next_attempt_at=None)
        return
    remote = _content_factory_remote_config()
    if now - op.created_at >= RECONCILE_AFTER:
        if working.update(state="needs_attention", next_attempt_at=None, receipt={"status": "reconciliation_required",
                "owner": "staff", "probe_id": str(op.pk), "head": expected}):
            from .founder_journey_alerts import queue_founder_journey_alert
            queue_founder_journey_alert(organization=site.organization, alert_reason="repo_health_probe_stalled",
                                        slot=str(op.pk), details={"website_connection_id": str(site.pk)})
        return
    if head and remote["enabled"] and remote["api_key_configured"]:
        try:
            response = http_client.post(f"{remote['base_url']}/api/repo-health/probes",
                                        headers=_content_factory_headers(), json=_wire(op), timeout=(3, 15))
            if response.status_code in {200, 202}:
                _reschedule(op, now, delay=CALLBACK_WAIT, reason="awaiting_result")
                return
        except Exception as exc:
            logger.warning("repo_health_dispatch_unavailable operation=%s type=%s", op.pk, type(exc).__name__)
    delay = timedelta(minutes=min(60, 2 ** min(op.attempts, 6)))
    _reschedule(op, now, delay=delay, reason="waiting_for_provider")


def sweep(now=None, limit=10):
    """Deliver due probes or renew missing callbacks without charging a founder."""
    from django.db.models import Exists, F, OuterRef, Q
    from django.conf import settings
    from .founder_journey_alerts import alerts_since
    shadow = getattr(settings, "ARTICLES_REPO_HEALTH_SHADOW_ENABLED", False)
    if not (shadow or repo_health.enabled()):
        return {"processed": 0}
    since = alerts_since()
    now = now or timezone.now()
    already_queued = WebsiteConnectionOperation.objects.filter(
        connection_id=OuterRef("pk"), generation=OuterRef("generation"), action=ACTION)
    unseen = WebsiteConnection.objects.filter(state="connected", updated_at__gte=since, repository_id__isnull=False,
        selected_configs__organization_id=F("organization_id")).filter(~Exists(already_queued))
    due = WebsiteConnectionOperation.objects.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        action=ACTION, created_at__gte=since, state__in=("pending", "running"))
    if shadow:
        return {"processed": 0, "eligible": unseen.count(), "due": due.count()}
    for site in unseen.select_related("organization").order_by("created_at")[:limit]:
        queue(site)
    ids = list(due.order_by("created_at").values_list("pk", flat=True)[:limit])
    for identifier in ids:
        advance(identifier, now)
    return {"processed": len(ids)}


def _check_wire(payload, site, op):
    if not _matches(site, op):
        raise ProbeError("repo_health_probe_superseded")
    expected = _wire(op)
    # Reject booleans as numeric IDs and require every field, including an empty app_root.
    if any(type(payload.get(k)) is not type(v) or payload[k] != v for k, v in expected.items()):
        raise ProbeError("repo_health_probe_mismatch")
    if not op.payload["expected_source_sha"] or op.state not in {"running", "pending", "completed", "needs_attention"}:
        raise ProbeError("repo_health_probe_superseded")


def receive(payload, *, complete):
    """Authenticate a specific current probe or settle the first matching build result."""
    if not repo_health.enabled():
        raise ProbeError("repo_health_disabled", 404)
    try:
        identifier = UUID(payload["probe_id"])
    except (AttributeError, TypeError, ValueError, KeyError):
        raise ProbeError("repo_health_probe_invalid", 400) from None
    op = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
        pk=identifier, action=ACTION).first()
    if not op:
        raise ProbeError("repo_health_probe_unknown")
    _check_wire(payload, op.connection, op)
    sha = op.payload["expected_source_sha"]
    stamp = None
    if complete:
        result = payload.get("result")
        if not repo_health.valid_build_result(result) or result.get("commit_sha") != sha:
            raise ProbeError("repo_health_build_result_invalid", 400)
        # Build output can contain private environment values. Only classification is needed here.
        result = {key: result[key] for key in ("schema", "status", "code", "blocking", "commit_sha")}
        result["log_tail"] = []
        stamp = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    observed_before = repo_health.current_head(op.connection)
    head = read_head(op.connection)
    if not head:
        raise ProbeError("repo_health_source_unavailable", 503)
    if head != sha:
        raise ProbeError("repo_health_probe_superseded")
    with transaction.atomic():
        site = WebsiteConnection.objects.select_for_update().select_related("organization").get(pk=op.connection_id)
        op = WebsiteConnectionOperation.objects.select_for_update().get(pk=identifier)
        _check_wire(payload, site, op)
        observed_now = repo_health.current_head(site)
        if observed_now != observed_before and observed_now != sha:
            raise ProbeError("repo_health_probe_superseded")
        if op.state == "completed":
            if complete and op.receipt.get("result_sha256") == stamp:
                return {"accepted": True}
            raise ProbeError("repo_health_probe_settled")
        if not complete and op.state == "needs_attention":
            raise ProbeError("repo_health_probe_held")
        if not complete:
            return {"authorized": True}
        repo_health.record_build_probe(site, result)
        op.state, op.next_attempt_at = "completed", None
        op.receipt = {"status": result["status"], "result_sha256": stamp}
        op.save(update_fields=("state", "receipt", "next_attempt_at", "updated_at"))
        return {"accepted": True}
