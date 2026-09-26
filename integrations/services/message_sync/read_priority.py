"""Bounded, durable read-refresh hints. Hints never grant access or mark a read."""
import math
import time

from django.db import transaction

KEY = "message_sync_read_priority_v1"
MAX_HINTS = 256
RECENT_SOURCE_ACTIVITY_SECONDS = 7 * 86400


def enqueue_refresh(authority, targets, *, reason="visible"):
    """Wake the account worker for already-authorized source conversations."""
    from integrations.services import slack_chat_read_state as reads
    from .read_state import KEY as WORKER_KEY
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        now = time.time()
        cursor = dict(connection.sync_cursor or {})
        hints = {key: value for key, value in (cursor.get(KEY) or {}).items()
                 if value.get("until", 0) > now}
        for target in targets:
            if target.read_scope not in authority.scopes:
                continue
            previous = hints.get(target.slack_id) or {}
            hints[target.slack_id] = {
                "requested_at": previous.get("requested_at", now),
                "until": now + (90 if reason == "visible" else 300),
                "reason": reason,
            }
        # Keep the oldest waiting work when a burst exceeds the bounded queue.
        cursor[KEY] = dict(sorted(hints.items(), key=lambda item: item[1]["requested_at"])[:MAX_HINTS])
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
        return (hints.get(target.slack_id) or {}).get("until", 0) > now
    def recent_unknown(target):
        value = snapshot(target)
        if value.get("excluded") is True or (
            value.get("available") is True and type(value.get("is_unread")) is bool
        ):
            return False
        try:
            activity = float(target.source_activity_ts)
        except (AttributeError, TypeError, ValueError):
            return False
        return (math.isfinite(activity) and
                now - RECENT_SOURCE_ACTIVITY_SECONDS <= activity <= now + 300)
    foreground = [t for t in eligible if
              (hinted(t) and age(t) >= 15)
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
