"""Fair account unread sweeps that continue while every client is closed."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, FloatField, Max, Q
from django.db.models.functions import Cast
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant
from .inbox import enabled
from .scheduler import BudgetDeferred, LeaseLost

KEY = "message_sync_read_state_v1"
_claim = ContextVar(KEY, default=None)


@dataclass(frozen=True)
class ReadStateLease:
    """One account turn, fenced against replacement and consent changes."""
    grant_id: int
    connection_id: int
    user_id: int
    token: str
    previous_served: float | None
    after: str


@contextmanager
def read_state_context(lease):
    """Apply the worker lease to every existing Slack authority fence."""
    token = _claim.set(lease)
    try:
        yield
    finally:
        _claim.reset(token)


def guard_read_state(grant, connection):
    """Reject expired workers before provider I/O or cached snapshot writes."""
    lease = _claim.get()
    if lease is None:
        return
    value = (connection.sync_cursor or {}).get(KEY) or {}
    if (grant.pk != lease.grant_id or connection.pk != lease.connection_id
            or value.get("token") != lease.token
            or float(value.get("expires") or 0) <= timezone.now().timestamp()):
        raise LeaseLost("read_state_lease_lost")


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
    return (grant, connection) if grant.connection_id == lease.connection_id else (None, None)


def claim_read_state():
    """Rotate workspaces, then owners; keep a durable source-ID continuation."""
    clock = timezone.now().timestamp()
    active = SlackDmMirrorGrant.objects.filter(
        status="active", revoked_at__isnull=True,
        connection__status__in=("connected", "syncing"),
    )
    due = active
    for field in ("due", "expires"):
        path = f"connection__sync_cursor__{KEY}__{field}"
        due = due.filter(Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__lte": clock}))
    served = f"connection__sync_cursor__{KEY}__served"
    workspaces = active.filter(slack_workspace_id__in=due.values("slack_workspace_id")).values(
        "slack_workspace_id",
    ).annotate(served=Max(Cast(served, FloatField()))).order_by(F("served").asc(nulls_first=True), "slack_workspace_id")
    for workspace in workspaces:
        candidates = list(due.filter(slack_workspace_id=workspace["slack_workspace_id"]).order_by(
            F(served).asc(nulls_first=True), "id",
        ).values_list("pk", "connection_id", "user_id"))
        for grant_id, connection_id, user_id in candidates:
            lease = ReadStateLease(grant_id, connection_id, user_id, uuid.uuid4().hex, None, "")
            with transaction.atomic():
                grant, connection = _lock(lease, skip_locked=True)
                if connection is None:
                    continue
                previous = (connection.sync_cursor or {}).get(KEY) or {}
                if max(float(previous.get("due") or 0), float(previous.get("expires") or 0)) > clock:
                    continue
                lease = ReadStateLease(grant_id, connection_id, user_id, lease.token, previous.get("served"), previous.get("after", ""))
                connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: {
                    **previous, "token": lease.token, "expires": clock + 120,
                    "served": clock, "due": clock,
                }}
                connection.save(update_fields=["sync_cursor", "updated_at"])
                return lease
    return None


def finish_read_state(lease, *, after, delay=1, error="", return_turn=False):
    """Release this lease without altering discovery or import checkpoints."""
    with transaction.atomic(), read_state_context(lease):
        grant, connection = _lock(lease)
        if connection is None:
            return
        guard_read_state(grant, connection)
        value = dict((connection.sync_cursor or {}).get(KEY) or {})
        value.update(token="", expires=0, after=after, due=timezone.now().timestamp() + max(1, delay), error=error[:100])
        if return_turn:
            if lease.previous_served is None:
                value.pop("served", None)
            else:
                value["served"] = lease.previous_served
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: value}
        connection.save(update_fields=["sync_cursor", "updated_at"])


def refresh_read_state_once():
    """Refresh at most one account conversation, never marking it as read."""
    from integrations.services import slack_chat_read_state as reads
    if not enabled():
        return 0
    lease = claim_read_state()
    if lease is None:
        return 0
    after, delay, error, return_turn = lease.after, 1, "", False
    target = None
    try:
        with read_state_context(lease):
            grant = SlackDmMirrorGrant.objects.select_related("connection").get(pk=lease.grant_id)
            reads._assert_grant_connection_authorized(grant)
            keys = set(CommunityChatDevice.objects.filter(user_id=grant.user_id, status="verified", revoked_at__isnull=True).values_list("public_key", flat=True))
            targets = sorted(reads._targets_for_keys(grant, keys), key=lambda t: t.slack_id)
            authority = reads._capture_slack_grant_api_authority(grant)
            with transaction.atomic():
                reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
                snapshots = cache.get_many([reads._cache_key(authority, t) for t in targets])
            # Source IDs are stable across discovery reorderings and devices.
            ordered = [t for t in targets if t.slack_id > after] + [t for t in targets if t.slack_id <= after]
            now = timezone.now().timestamp()
            target = next((t for t in ordered if now - (snapshots.get(reads._cache_key(authority, t)) or {}).get("fetched_at", 0) >= 60), None)
            if target is None:
                delay = 10 if targets else 60
                return 0
            reads.refresh_target(grant, authority, target)
            after = target.slack_id
            return 1
    except LeaseLost:
        return 0
    except (BudgetDeferred, reads.SlackDmMirrorRateLimited) as exc:
        error, delay = type(exc).__name__, getattr(exc, "retry_after", 60)
        return_turn = getattr(exc, "before_request_method", "") == "conversations.info"
        return 0
    except Exception as exc:
        # One broken conversation cannot prevent the rest of an owner's sweep.
        after = target.slack_id if target else after
        error, delay = type(exc).__name__, 30
        return 0
    finally:
        try:
            finish_read_state(lease, after=after, delay=delay, error=error, return_turn=return_turn)
        except LeaseLost:
            pass
