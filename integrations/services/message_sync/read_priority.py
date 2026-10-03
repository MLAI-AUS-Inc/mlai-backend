"""Bounded, durable read-refresh hints. Hints never grant access or mark a read."""
import math
import time
import uuid

from django.db import transaction

KEY = "message_sync_read_priority_v1"
MAX_HINTS = 256
RECENT_SOURCE_ACTIVITY_SECONDS = 7 * 86400


def hint_pending(value, now):
    """Activity remains dirty through quota pauses; visibility expires normally."""
    if not isinstance(value, dict):
        return False
    expiry = value.get("until")
    for field in ("requested_at", "last_requested_at"):
        stamp = value.get(field)
        if field in value and (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0):
            return False
    return (type(expiry) in (int, float) and math.isfinite(expiry)
            and expiry >= 0
            and (value.get("reason") == "activity" or expiry > now))


def merged_hints(hints, source_ids, *, now, reason):
    """Coalesce bounded metadata work without renewing a satisfied generation."""
    hints = {key: dict(value) for key, value in hints.items() if hint_pending(value, now)}
    for source_id in source_ids:
        previous = hints.get(source_id) or {}
        requested_at = previous.get("requested_at")
        if type(requested_at) not in (int, float) or not math.isfinite(requested_at):
            requested_at = now
        # Repeated visibility polls refer to the same pending observation. A
        # new source event must be distinguishable from an in-flight request.
        renewed = reason == "activity" or not previous
        hints[source_id] = {
            **previous,
            "requested_at": requested_at,
            "last_requested_at": now if renewed else previous.get("last_requested_at", now),
            "generation": uuid.uuid4().hex if renewed else previous.get("generation", uuid.uuid4().hex),
            "until": now + (90 if reason == "visible" else 300),
            "reason": "activity" if previous.get("reason") == "activity" else reason,
        }
    return dict(sorted(hints.items(), key=lambda item: item[1]["requested_at"])[:MAX_HINTS])


def satisfy_refresh(authority, target, hint, snapshot):
    """Consume only the hint generation observed by this successful source read."""
    if not isinstance(hint, dict) or not hint or not (snapshot.get("available") is True or snapshot.get("excluded") is True):
        return
    from integrations.services import slack_chat_read_state as reads
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        cursor = dict(connection.sync_cursor or {})
        hints = dict(cursor.get(KEY) or {})
        current = hints.get(target.slack_id)
        same_generation = (isinstance(current, dict) and current.get("generation") == hint.get("generation")
                           and (hint.get("generation") or current == hint))
        if (not same_generation or snapshot.get("fetched_at", 0) < hint.get("last_requested_at", hint.get("requested_at", 0))):
            return
        # A concurrent source publication/read receipt must not let an older
        # completed request satisfy the work that superseded it.
        cached = reads.cache.get(reads._cache_key(authority, target)) or {}
        if cached.get("revision") != snapshot.get("revision") or "revision" not in snapshot:
            return
        hints.pop(target.slack_id, None)
        cursor[KEY] = hints
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def prune_unroutable_hints(connection, source_ids, *, now):
    """Bound discovery hints without dropping retryable authorized work.

    The caller holds the owner authority lock. Unknown sources keep their
    discovery grace period; current targets survive provider quota pauses.
    """
    cursor = dict(connection.sync_cursor or {})
    hints = cursor.get(KEY) or {}
    retained = {key: value for key, value in hints.items()
                if hint_pending(value, now) and (key in source_ids or value["until"] > now)}
    if retained != hints:
        cursor[KEY] = retained
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def observation_progress(targets, snapshots, cache_key, *, now):
    """Report bounded, content-free observation coverage for the worker turn."""
    ages, unknown = [], 0
    for target in targets:
        snapshot = snapshots.get(cache_key(target)) or {}
        if not (snapshot.get("excluded") is True or (
            snapshot.get("available") is True and type(snapshot.get("is_unread")) is bool
        )):
            unknown += 1
        stamp = snapshot.get("fetched_at")
        if type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= now:
            ages.append(now - stamp)
    return {"checked_at": now, "expected_snapshot_count": len(targets),
            "unknown_snapshot_count": unknown,
            "oldest_observation_age_seconds": int(max(ages)) if ages else None}


def hint_progress(hints, *, now):
    """Expose pending count and age without source IDs or message content."""
    pending = [value for value in hints.values() if hint_pending(value, now)]
    stamps = [value.get("requested_at") for value in pending]
    ages = [now - stamp for stamp in stamps if type(stamp) in (int, float)
            and math.isfinite(stamp) and 0 <= stamp <= now]
    return {"pending_hint_count": len(pending),
            "oldest_pending_hint_age_seconds": int(max(ages)) if ages else None}


def enqueue_refresh(authority, targets, *, reason="visible"):
    """Wake the account worker for already-authorized source conversations."""
    from integrations.services import slack_chat_read_state as reads
    from .read_state import KEY as WORKER_KEY
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        now = time.time()
        cursor = dict(connection.sync_cursor or {})
        cursor[KEY] = merged_hints(cursor.get(KEY) or {},
                                  [target.slack_id for target in targets if target.read_scope in authority.scopes],
                                  now=now, reason=reason)
        cursor[WORKER_KEY] = {**(cursor.get(WORKER_KEY) or {}), "due": now}
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def select_target(ordered, snapshots, cache_key, cursor, *, now, turn):
    """Serve visible, known-unread and recent-unknown work with background fairness.

    A quiet room keeps its place even while visible rooms continually renew
    their hints. Shared Slack admission and account fairness remain unchanged.
    """
    hints = (cursor or {}).get(KEY) or {}
    retries = ((cursor or {}).get("message_sync_read_state_v1") or {}).get("retries") or {}
    eligible = [target for target in ordered if retries.get(target.slack_id, 0) <= now]
    def snapshot(target):
        return snapshots.get(cache_key(target)) or {}
    def age(target):
        return now - snapshot(target).get("fetched_at", 0)
    def hinted(target):
        return hint_pending(hints.get(target.slack_id), now)
    def activity(target):
        try:
            value = float(target.source_activity_ts)
        except (AttributeError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) and 0 < value <= now + 300 else None
    def changed_source(target):
        value = snapshot(target)
        source = activity(target)
        try:
            latest = float(value.get("latest_ts") or 0)
        except (ValueError, TypeError):
            latest = 0
        if not math.isfinite(latest):
            latest = 0
        # Activity is scheduling evidence, never evidence of an unread count.
        # Comparing observation time avoids endlessly probing an owner's own
        # posts or thread replies that do not advance the unread frontier.
        return (value.get("excluded") is not True and source is not None
                and source > max(value.get("fetched_at", 0), latest))
    def recent_unknown(target):
        value = snapshot(target)
        if value.get("excluded") is True or (
            value.get("available") is True and type(value.get("is_unread")) is bool
        ):
            return False
        source = activity(target)
        return source is not None and source >= now - RECENT_SOURCE_ACTIVITY_SECONDS
    foreground = [t for t in eligible if
              (hinted(t) and age(t) >= 15)
              or (snapshot(t).get("fetched_at") and changed_source(t) and age(t) >= 15)
              or (snapshot(t).get("refresh_required") and age(t) >= 1)]
    unread = [t for t in eligible if snapshot(t).get("is_unread") and age(t) >= 60]
    recent = [t for t in eligible if age(t) >= 60 and recent_unknown(t)]
    background = [t for t in eligible if age(t) >= 60]
    # Stable sorting preserves the source-ID continuation for equal ages.
    # An older unseen unread must not consume every priority turn until an
    # explicit visible hint expires. Oldest-first still applies within each
    # tier and to the reserved background turn.
    if turn % 4 == 3 and background:
        candidates = background
    elif foreground:
        candidates = foreground
    elif turn % 4 == 2 and recent:
        candidates = recent
    else:
        candidates = unread or recent or background
    return max(candidates, key=age, default=None)


def invalidate_event(payload):
    """Prioritize known conversations for the event's explicit Slack recipients.

    No event body or fabricated count enters the queue. The worker revalidates
    current consent, scopes, membership and device access before provider I/O.
    """
    from integrations.models import SlackDmMirrorGrant
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.slack_dm_mirror import _slack_event_authorized_user_ids, SlackDmMirrorAuthorizationError
    event = payload.get("event") or {}
    if event.get("type") != "message":
        return
    source_id = event.get("channel")
    owners = _slack_event_authorized_user_ids(payload)
    if not source_id or not owners:
        return
    grants = SlackDmMirrorGrant.objects.select_related("connection").filter(
        slack_workspace_id=payload.get("team_id"), slack_user_id__in=owners,
        status="active", revoked_at__isnull=True,
        connection__status__in=("connected", "syncing"),
    ).order_by("user_id", "id")
    for grant in grants:
        # The hint contains only a source identifier, not membership or data.
        # Unknown rooms are discarded by the worker's authorized target list.
        try:
            authority = reads._capture_slack_grant_api_authority(grant)
            enqueue_refresh(authority, [reads.ReadTarget("", source_id, "im")], reason="activity")
        except SlackDmMirrorAuthorizationError:
            # Another recipient's expired/replaced grant cannot hold this
            # already-durable Slack receipt or other owners' hints hostage.
            continue
