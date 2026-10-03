"""Incremental repair bounds and quiet-room pacing; never a source-read badge."""
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
import logging
import time

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

OVERLAP_SECONDS = 300
DEEP_SCAN_SECONDS = 6 * 3600
HOT_DELAY_SECONDS = 60
MAX_QUIET_DELAY_SECONDS = 15 * 60
WAKE_COALESCE_SECONDS = 30
logger = logging.getLogger(__name__)


def _stamp(value):
    try:
        stamp = Decimal(str(value))
        return stamp if stamp.is_finite() and stamp >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _policy(state, scope):
    value = state.head_cursor if isinstance(state.head_cursor, dict) else {}
    return value if value.get("version") == 1 and value.get("scope") == scope else {}


def head_scope(state, *, conversation=None, history_days=30):
    """Fence incremental progress to the current mapping and consent window."""
    if conversation is None:
        channel = state.public_channel
        boundary = (channel.slack_workspace_id, channel.slack_channel_id,
                    channel.destination_workspace_id, channel.destination_channel_id)
    else:
        boundary = (conversation.slack_workspace_id, conversation.slack_conversation_id,
                    str(conversation.mlai_channel_id), conversation.participant_hash)
    return ":".join(str(value) for value in (state.authority_generation, history_days, *boundary))


def prepare_head(state, checkpoint, *, now, consent_floor, scope):
    """Resume a fixed scan, or repair from its last complete upper bound.

    A delayed worker covers the whole gap within consent, even when it exceeds
    one day. Periodic deeper scans still observe older edits and thread roots.
    """
    checkpoint = dict(checkpoint)
    if checkpoint.get("authority_generation") not in (None, scope):
        checkpoint = ({"upper_bound": checkpoint["upper_bound"]}
                      if checkpoint.get("upper_bound") else {})
    checkpoint.setdefault("upper_bound", f"{int(now)}.999999")
    upper_stamp = _stamp(checkpoint["upper_bound"])
    if upper_stamp is None:
        raise ValueError("invalid_history_page")
    upper = int(upper_stamp)
    saved_floor = _stamp(checkpoint.get("oldest"))
    if saved_floor is not None and saved_floor < consent_floor:
        # A reduced consent window cannot reuse an earlier pagination cursor.
        checkpoint = {"upper_bound": checkpoint["upper_bound"]}
        saved_floor = None
    checkpoint["authority_generation"] = scope
    if saved_floor is not None:
        return checkpoint
    policy = _policy(state, scope)
    completed = _stamp(policy.get("completed_upper"))
    if completed is None or completed > upper + 1:
        oldest = upper - 86400
    else:
        oldest = int(completed) - OVERLAP_SECONDS
    deep_at = _stamp(policy.get("deep_checked_at"))
    deep = deep_at is None or upper - deep_at >= DEEP_SCAN_SECONDS
    if deep:
        oldest = min(oldest, upper - 86400)
    checkpoint["oldest"] = f"{max(0, consent_floor, oldest)}.000000"
    checkpoint["phase"] = "head_deep" if deep else "head_delta"
    return checkpoint


def observe_head(checkpoint, messages):
    """Accumulate only activity timestamps across pages, excluding message text."""
    checkpoint = dict(checkpoint)
    latest = _stamp(checkpoint.get("head_latest_activity")) or Decimal(0)
    upper = _stamp(checkpoint.get("upper_bound")) or Decimal(0)
    for message in messages:
        edited = message.get("edited") or {}
        for value in (message.get("ts"), message.get("latest_reply"),
                      edited.get("ts") if isinstance(edited, dict) else None):
            stamp = _stamp(value)
            if stamp is not None and latest < stamp <= upper:
                latest = stamp
    checkpoint["head_latest_activity"] = format(latest, "f")
    return checkpoint


def finish_head(state, checkpoint, *, complete, now, scope):
    """Advance a watermark only after all accessible pages commit under a lease."""
    if not complete or checkpoint.get("source_limited"):
        return HOT_DELAY_SECONDS
    policy = dict(_policy(state, scope))
    upper = _stamp(checkpoint.get("upper_bound"))
    if upper is None or upper > Decimal(str(now)) + 1:
        return HOT_DELAY_SECONDS
    previous = _stamp(policy.get("completed_upper"))
    activity = _stamp(checkpoint.get("head_latest_activity")) or Decimal(0)
    # Overlapping old rows alone must not keep a quiet conversation hot.
    changed = activity > (previous if previous is not None else Decimal(str(now - OVERLAP_SECONDS)))
    current = state.head_cursor if isinstance(state.head_cursor, dict) else {}
    wake = _stamp(current.get("wake_requested_at")) or Decimal(0)
    # The query upper bound ends in .999999, slightly after its actual start.
    # Also, a wake represents the following coalescing window: a second event
    # arriving inside that window need not update the stored timestamp. Treat
    # either overlap as hot, including events arriving in the start second.
    changed = changed or bool(wake and wake + WAKE_COALESCE_SECONDS >= int(upper))
    quiet = policy.get("quiet_runs", 0)
    quiet = min(5, quiet) if type(quiet) is int and quiet >= 0 else 0
    quiet = 0 if changed else min(5, quiet + 1)
    policy.update(version=1, scope=scope, quiet_runs=quiet,
                  completed_upper=format(max(previous or Decimal(0), upper), "f"))
    if checkpoint.get("phase") == "head_deep":
        policy["deep_checked_at"] = int(upper)
    if wake:
        policy["wake_requested_at"] = float(wake)
    state.head_cursor = policy
    state.save(update_fields=["head_cursor"])
    if not getattr(settings, "MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED", False):
        return HOT_DELAY_SECONDS
    return min(MAX_QUIET_DELAY_SECONDS, HOT_DELAY_SECONDS * 2 ** quiet)


def wake_head_locked(state, *, now=None):
    """Coalesce a verified hint without replacing a lease, cursor or backoff.

    The caller holds the state lock after its existing authority locks. Provider
    admission still applies when the job eventually runs.
    """
    if state.status in {"paused", "revoked"}:
        return
    now = time.time() if now is None else now
    policy = dict(state.head_cursor) if isinstance(state.head_cursor, dict) else {}
    previous = _stamp(policy.get("wake_requested_at"))
    if previous is not None and 0 <= Decimal(str(now)) - previous < WAKE_COALESCE_SECONDS:
        return
    policy["wake_requested_at"] = now
    state.head_cursor = policy
    state.save(update_fields=["head_cursor"])
    completed = _stamp(policy.get("completed_upper")) or Decimal(0)
    due = datetime.fromtimestamp(max(now, float(completed) + HOT_DELAY_SECONDS), tz=datetime_timezone.utc)
    clock = timezone.now()
    job = state.jobs.select_for_update(skip_locked=True).filter(
        kind="head", due_at__gt=due, backoff_seconds=0, last_error_code="",
    ).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=clock),
    ).order_by("pk").first()
    if job is not None:
        job.due_at = due
        job.save(update_fields=["due_at"])


def wake_private_targets(authority, targets):
    """Wake existing owner mirrors only; a hint cannot create or authorize one."""
    from integrations.models import BridgeSyncState
    sources = {target.slack_id for target in targets if target.read_scope in authority.scopes}
    if not sources:
        return
    states = BridgeSyncState.objects.select_for_update(of=("self",)).filter(
        workspace_id=authority.workspace_id, source_channel_id__in=sources,
        private_conversation__grant_id=authority.grant_id,
        private_conversation__status="live",
    ).exclude(status__in=("paused", "revoked")).order_by("pk")
    for state in states:
        wake_head_locked(state)


def defer_public_target_wake(authority, targets):
    """Wake authorized public mappings only after the owner locks are released.

    Inbox routing takes public state before owner locks. Deferring this hint
    avoids reversing that order in an authenticated foreground read request.
    The callback imports no content and cannot create a mapping or grant access.
    """
    from integrations.models import BridgeSyncState
    pairs = sorted({(target.slack_id, target.channel_id) for target in targets
             if target.kind == "public_channel" and target.channel_id
             and target.read_scope in authority.scopes})[:4]
    if not pairs:
        return
    workspace_id = authority.workspace_id
    mapped = Q(pk__in=[])
    for source_id, channel_id in pairs:
        mapped |= Q(source_channel_id=source_id, public_channel__destination_channel_id=channel_id)
    # This read takes no state lock while the caller owns account locks.
    try:
        # A savepoint keeps failure of this optional hint out of the owner's
        # already-successful read transaction.
        with transaction.atomic():
            versions = list(BridgeSyncState.objects.filter(
                mapped, workspace_id=workspace_id, public_channel__enabled=True,
                public_channel__destination_platform="buzz",
            ).values_list("pk", "authority_generation")[:4])
    except Exception:
        logger.warning("message_sync_public_head_wake_failed")
        return
    if not versions:
        return

    def wake():
        generations = Q(pk__in=[])
        for state_id, generation in versions:
            generations |= Q(pk=state_id, authority_generation=generation)
        try:
            with transaction.atomic():
                states = BridgeSyncState.objects.select_for_update(skip_locked=True, of=("self",)).filter(
                    mapped, generations, workspace_id=workspace_id, public_channel__enabled=True,
                    public_channel__destination_platform="buzz",
                ).exclude(status__in=("paused", "revoked")).order_by("pk")
                for state in states:
                    wake_head_locked(state)
        except Exception:
            # This optional acceleration cannot fail an already-committed read.
            logger.warning("message_sync_public_head_wake_failed")

    transaction.on_commit(wake, robust=True)
