"""Fair account unread sweeps that continue while every client is closed."""
import math
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
from .request_priority import request_priority

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
    turn: int = 0


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
                claimed_at = timezone.now().timestamp()
                previous = (connection.sync_cursor or {}).get(KEY) or {}
                if max(float(previous.get("due") or 0), float(previous.get("expires") or 0)) > claimed_at:
                    continue
                lease = ReadStateLease(grant_id, connection_id, user_id, lease.token, previous.get("served"), previous.get("after", ""), int(previous.get("turn") or 0))
                connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: {
                    **previous, "token": lease.token, "expires": claimed_at + 120,
                    "served": claimed_at, "due": claimed_at,
                }}
                connection.save(update_fields=["sync_cursor", "updated_at"])
                return lease
    return None


def finish_read_state(lease, *, after, delay=1, error="", return_turn=False, failed_source="", retry_seconds=60,
                      progress=None, observed_at=None, deferred_stage="", deferred_seconds=0):
    """Release this lease without altering discovery or import checkpoints."""
    with transaction.atomic(), read_state_context(lease):
        grant, connection = _lock(lease)
        if connection is None:
            return
        guard_read_state(grant, connection)
        value = dict((connection.sync_cursor or {}).get(KEY) or {})
        now = timezone.now().timestamp()
        from .read_priority import KEY as PRIORITY_KEY, hint_progress
        summary = dict(value.get("progress") or {})
        summary.update(progress or {})
        summary.update(hint_progress((connection.sync_cursor or {}).get(PRIORITY_KEY) or {}, now=now))
        summary["updated_at"] = now
        if observed_at is not None:
            summary["last_successful_observation_at"] = observed_at
        summary["deferred_stage"] = deferred_stage
        summary["deferred_until"] = now + deferred_seconds if deferred_stage else None
        if deferred_stage:
            summary["deferred_turn_count"] = int(summary.get("deferred_turn_count") or 0) + 1
        value["progress"] = summary
        retries = {key: due for key, due in (value.get("retries") or {}).items() if due > now}
        if failed_source:
            retries[failed_source] = now + max(1, retry_seconds)
        value.update(retries=retries, turn=lease.turn + (0 if return_turn else 1))
        value.update(token="", expires=0, after=after, due=timezone.now().timestamp() + max(1, delay), error=error[:100])
        if return_turn:
            if lease.previous_served is None:
                value.pop("served", None)
            else:
                value["served"] = lease.previous_served
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: value}
        connection.save(update_fields=["sync_cursor", "updated_at"])


def refresh_request_priority(target, snapshot, hint, *, now):
    """Keep source activity selected for prompt observation in the foreground.

    Match the timestamp and age gates in read_priority.select_target. A source
    activity timestamp changes scheduling only; it never asserts an unread.
    """
    from .read_priority import hint_pending
    from django.conf import settings
    if getattr(settings, "MESSAGE_SYNC_TARGETED_READ_POLLING", False):
        from .targeted_reads import possibly_unread
        if possibly_unread(target, snapshot, now=now):
            return "foreground"
    if hint_pending(hint, now):
        return "foreground"
    try:
        fetched_at = float(snapshot.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return "background"
    if not math.isfinite(fetched_at):
        return "background"
    age = now - fetched_at
    if snapshot.get("refresh_required") and age >= 1:
        return "foreground"
    if not fetched_at or age < 15 or snapshot.get("excluded") is True:
        return "background"
    try:
        source = float(target.source_activity_ts)
    except (AttributeError, TypeError, ValueError):
        return "background"
    if not math.isfinite(source) or not 0 < source <= now + 300:
        return "background"
    try:
        latest = float(snapshot.get("latest_ts") or 0)
    except (TypeError, ValueError):
        latest = 0
    if not math.isfinite(latest):
        latest = 0
    return "foreground" if source > max(fetched_at, latest) else "background"


def refresh_read_state_once():
    """Confirm one explicit queued read, or refresh one account conversation."""
    from integrations.services import slack_chat_read_state as reads
    if not enabled():
        return 0
    lease = claim_read_state()
    if lease is None:
        return 0
    after, delay, error, return_turn, failed_source = lease.after, 1, "", False, ""
    retry_seconds = 60
    progress, observed_at, deferred_stage, deferred_seconds = None, None, "", 0
    target = None
    try:
        with read_state_context(lease):
            grant = SlackDmMirrorGrant.objects.select_related("connection").get(pk=lease.grant_id)
            reads._assert_grant_connection_authorized(grant)
            keys = set(CommunityChatDevice.objects.filter(user_id=grant.user_id, status="verified", revoked_at__isnull=True).values_list("public_key", flat=True))
            authority = reads._capture_slack_grant_api_authority(grant)
            from .inbox_observations import flush
            flush(authority, grant, keys)
            from .read_snapshots import flush_notification
            flush_notification(authority)
            from .receipts import flush_read_once
            # A sustained stream of explicit reads must still leave refresh
            # capacity for other conversations belonging to this account.
            with request_priority("foreground"):
                from django.conf import settings
                if getattr(settings, 'MESSAGE_SYNC_INBOX_READ_EXPORT', False) and lease.turn % 4 != 3:
                    from .inbox_exports import flush_once
                    exported = flush_once(grant, authority, keys)
                    if exported is not None:
                        return exported
                confirmed = flush_read_once(grant, authority, keys) if lease.turn % 4 != 3 else None
            if confirmed is not None:
                return confirmed
            routed_targets = [
                target for target in reads._targets_for_keys(grant, keys)
                if target.read_scope in authority.scopes
            ]
            from integrations.services.slack_owner_inventory import source_read_targets

            targets = sorted(
                routed_targets + source_read_targets(grant, authority, routed_targets),
                key=lambda target: target.slack_id,
            )
            from .read_priority import KEY as PRIORITY_KEY, observation_progress, prune_unroutable_hints, select_target, satisfy_refresh
            with transaction.atomic():
                _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
                snapshots = cache.get_many([reads._cache_key(authority, t) for t in targets])
                prune_unroutable_hints(connection, {t.slack_id for t in targets}, now=timezone.now().timestamp())
            # Source IDs are stable across discovery reorderings and devices.
            ordered = [t for t in targets if t.slack_id > after] + [t for t in targets if t.slack_id <= after]
            now = timezone.now().timestamp()
            progress = observation_progress(targets, snapshots, lambda t: reads._cache_key(authority, t), now=now)
            target = select_target(ordered, snapshots, lambda t: reads._cache_key(authority, t),
                                   connection.sync_cursor, now=now, turn=lease.turn)
            if target is None:
                delay = 10 if targets else 60
                return 0
            hint = ((connection.sync_cursor or {}).get(PRIORITY_KEY) or {}).get(target.slack_id)
            priority = refresh_request_priority(
                target, snapshots.get(reads._cache_key(authority, target)) or {}, hint, now=now,
            )
            with request_priority(priority):
                snapshot = reads.refresh_target(grant, authority, target)
            satisfy_refresh(authority, target, hint, snapshot)
            if snapshot.get("available") is True or snapshot.get("excluded") is True:
                observed_at = snapshot.get("fetched_at")
            else:
                # A source response without a usable owner cursor is still
                # unknown. Retain the dirty hint, but do not retry it at the
                # visible-row cadence and crowd out resolvable conversations.
                failed_source, retry_seconds = target.slack_id, 60
            after = target.slack_id
            return 1
    except LeaseLost:
        return 0
    except (BudgetDeferred, reads.SlackDmMirrorRateLimited) as exc:
        error, delay = type(exc).__name__, getattr(exc, "retry_after", 60)
        return_turn = getattr(exc, "before_request_method", "") == "conversations.info"
        method = getattr(exc, "read_state_method", "") or getattr(exc, "before_request_method", "")
        deferred_stage, deferred_seconds = method or "provider", delay
        if target is not None and method in {"conversations.history", "conversations.replies"}:
            # A secondary history quota must not hold this owner's independent
            # DM info snapshots hostage. Retain the metadata checkpoint and
            # pause only this target while other methods continue to progress.
            failed_source, retry_seconds = target.slack_id, max(15, delay)
            delay = 1
        return 0
    except Exception as exc:
        # One broken conversation cannot prevent the rest of an owner's sweep.
        after = target.slack_id if target else after
        if target is not None:
            with transaction.atomic():
                reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
                key = reads._cache_key(authority, target)
                value = cache.get(key)
                if value and value.get("refresh_required"):
                    cache.set(key, {**value, "refresh_required": False}, timeout=86400)
        error, delay = type(exc).__name__, 1
        failed_source = target.slack_id if target else ""
        return 0
    finally:
        try:
            finish_read_state(lease, after=after, delay=delay, error=error, return_turn=return_turn,
                              failed_source=failed_source, retry_seconds=retry_seconds,
                              progress=progress, observed_at=observed_at, deferred_stage=deferred_stage,
                              deferred_seconds=deferred_seconds)
        except LeaseLost:
            pass
