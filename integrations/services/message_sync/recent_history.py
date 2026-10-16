"""Recent-first Slack imports, with durable, explicitly requested scrollback.

The limit counts source timeline entries admitted by the existing author and
consent checks. It never widens those checks or certifies a
complete archive. All state is content-free and uses existing scan markers.
"""
import hashlib
import json
import re

from django.conf import settings

PRELOAD_MESSAGES = 50
IDLE_SECONDS = 86400


def enabled():
    """Use recent-first imports whenever the durable sync engine owns history."""
    return bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False) and
                getattr(settings, "MESSAGE_SYNC_RECENT_HISTORY_ENABLED", False))


def page_limit(progress, provider_limit):
    """Cap each provider page at the remainder of this requested batch."""
    target = max(PRELOAD_MESSAGES, int(progress.get("requested_messages", PRELOAD_MESSAGES)))
    return max(0, min(provider_limit, target - int(progress.get("recent_messages", 0))))


def advance(progress, messages):
    """Count source entries once, in the same transaction as their cursor."""
    return {**progress, "recent_messages": int(progress.get("recent_messages", 0)) + len(messages)}


def _targets(progress):
    try:
        values = json.loads(progress.get("demand_targets", "{}"))
    except (ValueError, TypeError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in list(values.items())[-16:]
            if re.fullmatch(r"[0-9a-f]{32}", key) and type(value) is int and 0 < value < 10**12}


def _request_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def request_batch(progress, request_key, *, complete=False):
    """Assign each boundary a durable target, including concurrent device polls."""
    progress = dict(progress)
    targets = _targets(progress)
    key = _request_hash(request_key)
    if complete or key in targets:
        return progress
    count = int(progress.get("recent_messages", 0))
    target = max(PRELOAD_MESSAGES, int(progress.get("requested_messages", PRELOAD_MESSAGES)))
    if count >= target:
        target = count + PRELOAD_MESSAGES
    elif not targets:
        # An explicit scroll requests beyond the initial preload even if the
        # background first pass is still running against an old local cache.
        target += PRELOAD_MESSAGES
    targets[key] = target
    progress.update(requested_messages=target,
                    demand_targets=json.dumps(dict(list(targets.items())[-16:]), separators=(",", ":")))
    return progress


def request_pending(progress, key):
    """A later reader's demand cannot restart a completed earlier request."""
    target = _targets(progress).get(_request_hash(key), PRELOAD_MESSAGES)
    return int(progress.get("recent_messages", 0)) < target


def has_demand(progress):
    """Explicit unfinished scrollback gets provider priority without extra quota."""
    return any(target > int(progress.get("recent_messages", 0)) for target in _targets(progress).values())


def merge_demand(checkpoint, current):
    """Keep demand recorded while a public page held its immutable lease cursor."""
    result = dict(checkpoint)
    for field in ("requested_messages", "demand_targets"):
        if field in current:
            result[field] = current[field]
    return result


def private_progress(conversation, *, lock=False):
    """Read the existing authority-bound main timeline marker, never bodies."""
    from integrations.services import slack_dm_mirror as dm
    rows = conversation.deliveries.filter(
        source_platform="slack", source_message_id=dm.HISTORY_MAIN_STATE_ID,
        operation="create", status="completed",
    )
    if lock:
        rows = rows.select_for_update()
    marker = rows.first()
    if marker is not None:
        metadata = marker.metadata or {}
        if (metadata.get("participant_hash") != dm._history_participant_boundary(conversation)
                or metadata.get("mlai_channel_id") != str(conversation.mlai_channel_id)
                or metadata.get("history_days") != dm._grant_history_days(conversation.grant)):
            return None
    return marker


def private_page_limit(conversation, provider_limit):
    """Pause deep imports at fifty entries while retaining the legacy cursor."""
    if not enabled():
        return provider_limit
    marker = private_progress(conversation)
    if marker is None:
        return min(PRELOAD_MESSAGES, provider_limit)
    if (marker.metadata or {}).get("complete"):
        return 0
    return page_limit(marker.metadata or {}, provider_limit)


def imported_floor(state, consent_floor):
    """Repair only already imported history plus new arrivals, not old archives."""
    from .history import timestamp
    if not enabled():
        return consent_floor
    if state.private_conversation_id:
        conversation = state.private_conversation
        frontier = conversation.oldest_synced_ts
        if not frontier and getattr(conversation, "history_backfilled_at", None):
            frontier = f"{int(conversation.history_backfilled_at.timestamp())}.000000"
    else:
        from .head_repair import head_scope
        job = state.jobs.filter(kind="archive", source_object_key="").first()
        progress = job.checkpoint or {} if job else {}
        if progress.get("authority_generation") != head_scope(state):
            return None
        frontier = progress.get("frontier") or (progress.get("upper_bound") if progress.get("timeline_complete") else None)
    if not frontier:
        return None
    return max(consent_floor, timestamp(frontier)[0])
