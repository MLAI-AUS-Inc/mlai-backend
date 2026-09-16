"""Durable, rotating discovery leases stored beside existing list checkpoints.

Lock order matches consent revocation: user, all grants, connection. No Slack
request runs while claiming; every later provider call/write checks this lease.
"""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, FloatField, Max, Q
from django.db.models.functions import Cast
from django.utils import timezone

from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant
from .scheduler import BudgetDeferred, LeaseLost

KEY = "message_sync_discovery"
_claim = ContextVar("message_sync_discovery_claim", default=None)


@dataclass(frozen=True)
class DiscoveryLease:
    grant_id: int
    connection_id: int
    user_id: int
    token: str


@contextmanager
def discovery_context(lease):
    token = _claim.set(lease)
    try:
        yield
    finally:
        _claim.reset(token)


def guard_discovery(grant, connection):
    """Validate inside the existing locked consent transaction, if claimed."""
    lease = _claim.get()
    if lease is None:
        return
    value = (connection.sync_cursor or {}).get(KEY) or {}
    if (grant.pk != lease.grant_id or connection.pk != lease.connection_id
            or value.get("token") != lease.token
            or float(value.get("expires") or 0) <= timezone.now().timestamp()):
        raise LeaseLost("discovery_lease_lost")


def _lock(lease, *, skip_locked=False):
    user = get_user_model().objects.select_for_update(skip_locked=skip_locked).filter(pk=lease.user_id).first()
    if user is None:
        return None, None
    grants = list(SlackDmMirrorGrant.objects.select_for_update().filter(user_id=lease.user_id).order_by("id"))
    grant = next((row for row in grants if row.pk == lease.grant_id), None)
    if grant is None or grant.status != "active" or grant.revoked_at is not None:
        return None, None
    connection = ExternalServiceConnection.objects.select_for_update().filter(
        pk=lease.connection_id, user_id=lease.user_id, provider="slack",
    ).first()
    if connection is None or grant.connection_id != connection.pk:
        return None, None
    return grant, connection


def claim_discovery(interval_seconds, *, lease_seconds=120):
    """One list page per turn; incomplete first-page grants cannot starve others."""
    now = timezone.now()
    clock = now.timestamp()
    active = SlackDmMirrorGrant.objects.filter(status="active", revoked_at__isnull=True, connection__isnull=False)
    due = active.filter(Q(last_discovery_at__isnull=True) | Q(last_discovery_at__lt=now - timedelta(seconds=interval_seconds)))
    for field in ("due", "expires"):
        path = f"connection__sync_cursor__{KEY}__{field}"
        due = due.filter(Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__lte": clock}))
    served = f"connection__sync_cursor__{KEY}__served"
    workspaces = active.filter(slack_workspace_id__in=due.values("slack_workspace_id")).values(
        "slack_workspace_id",
    ).annotate(served=Max(Cast(served, FloatField()))).order_by(F("served").asc(nulls_first=True), "slack_workspace_id")
    for workspace in workspaces:
        # Materialize only opaque candidate IDs before starting lock transactions.
        # A server-side cursor held across those transactions can deadlock its
        # connection while being finalized after an early successful return.
        candidates = due.filter(slack_workspace_id=workspace["slack_workspace_id"]).order_by(
            F(served).asc(nulls_first=True), "id",
        ).values_list("pk", "connection_id", "user_id")
        for grant_id, connection_id, user_id in candidates:
            lease = DiscoveryLease(grant_id, connection_id, user_id, uuid.uuid4().hex)
            with transaction.atomic():
                grant, connection = _lock(lease, skip_locked=True)
                if connection is None:
                    continue
                previous = (connection.sync_cursor or {}).get(KEY) or {}
                if max(float(previous.get("due") or 0), float(previous.get("expires") or 0)) > clock:
                    continue
                if grant.last_discovery_at is not None and grant.last_discovery_at >= now - timedelta(seconds=interval_seconds):
                    continue
                connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: {
                    "token": lease.token, "expires": clock + lease_seconds,
                    "served": clock, "due": clock,
                }}
                connection.save(update_fields=["sync_cursor", "updated_at"])
                return lease
    return None


def finish_discovery(lease, *, delay_seconds=5, error_code=""):
    """Release only this claim; preserve list cursor and other integration state."""
    with transaction.atomic(), discovery_context(lease):
        grant, connection = _lock(lease)
        if connection is None:
            return
        guard_discovery(grant, connection)
        value = dict((connection.sync_cursor or {}).get(KEY) or {})
        value.update(token="", expires=0, due=timezone.now().timestamp() + max(1, delay_seconds), error=error_code[:100])
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: value}
        connection.save(update_fields=["sync_cursor", "updated_at"])


def discover_once(interval_seconds):
    """Run one resumable discovery page under a durable generation fence."""
    from integrations.services import slack_dm_mirror as dm
    lease = claim_discovery(interval_seconds)
    if lease is None:
        return False
    delay, error = 5, ""
    try:
        grant = SlackDmMirrorGrant.objects.select_related("connection").get(pk=lease.grant_id)
        with discovery_context(lease):
            dm.discover_conversations(grant)
    except LeaseLost:
        return False
    except BudgetDeferred as exc:
        # Local admission pacing is not a provider failure. Keep the exact
        # shared-budget delay instead of adding thirty seconds to every page.
        error, delay = type(exc).__name__, exc.retry_after
    except Exception as exc:
        error = type(exc).__name__
        delay = max(30, dm._slack_retry_after_seconds(exc))
        if dm._is_slack_auth_error(exc):
            user = get_user_model().objects.filter(pk=lease.user_id).first()
            if user is not None:
                dm.revoke_user_grant(user)
    finally:
        try:
            finish_discovery(lease, delay_seconds=delay, error_code=error)
        except LeaseLost:
            pass
    return not error
