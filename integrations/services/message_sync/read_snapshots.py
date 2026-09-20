"""Versioned owner snapshots and a coalesced, content-free notification outbox."""
import time

from django.db import transaction

KEY = "message_sync_read_snapshot_v1"
FIELDS = ("available", "last_read", "latest_ts", "is_unread", "unread_count", "has_personal_mention")


def publish_snapshot(connection, cache_key, snapshot):
    """Write under the existing owner/connection authority lock.

    Revision numbers retain ordering when requests finish out of order or the
    wall clock steps backwards. Cache eviction can cause a harmless gap, never
    make an old response newer than a source-confirmed read.
    """
    from django.core.cache import cache
    previous = cache.get(cache_key) or {}
    cursor = dict(connection.sync_cursor or {})
    state = dict(cursor.get(KEY) or {})
    revision = max(int(state.get("revision") or 0), int(previous.get("revision") or 0),
                   time.time_ns() // 1000) + 1
    snapshot = {**snapshot, "revision": revision}
    state["revision"] = revision
    if any(previous.get(field) != snapshot.get(field) for field in FIELDS):
        state.update(pending=revision, due=0)
    cursor[KEY] = state
    connection.sync_cursor = cursor
    connection.save(update_fields=["sync_cursor", "updated_at"])
    cache.set(cache_key, snapshot, timeout=86400)
    return snapshot


def flush_notification(authority):
    """Deliver a hint only; clients must fetch a fresh authenticated snapshot.

    A failed relay or rolling deployment leaves the latest revision pending.
    Polling and reconnect recovery work independently of this fast path.
    """
    from community_chat.models import CommunityChatDevice
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.community_bridge.buzz import BuzzBridgeClient
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        state = dict((connection.sync_cursor or {}).get(KEY) or {})
        revision = state.get("pending")
        if not revision or state.get("due", 0) > time.time():
            return
        keys = list(CommunityChatDevice.objects.filter(
            user_id=authority.user_id, status="verified", revoked_at__isnull=True,
        ).order_by("pk").values_list("public_key", flat=True))
    succeeded = False
    try:
        # The protocol deliberately carries no Slack ID, room ID, cursor,
        # message content or unread count. Revoked devices cannot fetch data.
        for offset in range(0, len(keys), 8):
            BuzzBridgeClient.notify_read_state(keys[offset:offset + 8], revision=revision)
        succeeded = True
    except Exception:
        # Notification delivery must never prevent confirming a Slack read.
        pass
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        cursor = dict(connection.sync_cursor or {})
        current = dict(cursor.get(KEY) or {})
        if current.get("pending") == revision:
            if succeeded:
                current.pop("pending", None)
                current.pop("due", None)
            else:
                current["due"] = time.time() + 30
            cursor[KEY] = current
            connection.sync_cursor = cursor
            connection.save(update_fields=["sync_cursor", "updated_at"])
