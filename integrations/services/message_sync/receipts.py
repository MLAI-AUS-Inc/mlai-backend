"""Durable, coalesced owner read intents; retry even after every app closes.

The existing connection JSON checkpoint stores only identifiers and timestamps.
Every retry revalidates consent, OAuth generation, device and source membership.
"""
import time

from django.db import transaction


KEY = "message_sync_read_receipts_v1"


def _save(connection, queue):
    connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: queue}
    connection.save(update_fields=["sync_cursor", "updated_at"])


def enqueue_read(authority, target, *, public_key, source_ts):
    """Persist the highest requested frontier before attempting provider I/O."""
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.slack_dm_mirror import _locked_active_verified_device
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        device = _locked_active_verified_device(authority.user_id, public_key)
        if device is None:
            raise reads.SlackDmMirrorError("The requesting device is no longer verified.")
        binding = {"device_id": str(device.pk), "verified_at": str(device.verified_at)}
        now = time.time()
        queue = {k: v for k, v in ((connection.sync_cursor or {}).get(KEY) or {}).items()
                 if now - v.get("requested_at", 0) < 7 * 86400}
        key = reads._cache_key(authority, target)
        intent_key = f"{target.slack_id}:{device.pk}"
        previous = queue.get(intent_key) or {}
        if previous.get("authority") != key or previous.get("device") != binding:
            previous = {}
        if (reads._timestamp(previous.get("source_ts")) or 0) > reads._timestamp(source_ts):
            source_ts = previous["source_ts"]
        # Separate device frontiers preserve a valid lower read if another
        # device's higher read is revoked before it reaches Slack.
        queue[intent_key] = {"authority": key, "channel_id": target.channel_id,
                             "source_id": target.slack_id, "device": binding,
                             "public_key": public_key, "source_ts": source_ts,
                             "requested_at": previous.get("requested_at", now), "due": now}
        from .read_state import KEY as READ_STATE_KEY
        cursor = dict(connection.sync_cursor or {})
        cursor[READ_STATE_KEY] = {**(cursor.get(READ_STATE_KEY) or {}), "due": now}
        connection.sync_cursor = cursor
        _save(connection, queue)
        return binding


def complete_read(authority, target, *, source_ts):
    """Remove only the exact generation and frontier that was confirmed."""
    from integrations.services import slack_chat_read_state as reads
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        queue = dict((connection.sync_cursor or {}).get(KEY) or {})
        confirmed = {k for k, value in queue.items()
                     if value.get("source_id") == target.slack_id
                     and value.get("authority") == reads._cache_key(authority, target)
                     and (reads._timestamp(value.get("source_ts")) or 0) <= reads._timestamp(source_ts)}
        if confirmed:
            _save(connection, {k: v for k, v in queue.items() if k not in confirmed})


def flush_read_once(grant, authority, keys):
    """Retry one due intent. Return None when normal unread sweeping may run."""
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.slack_dm_mirror import _locked_active_verified_device
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        queue = dict((connection.sync_cursor or {}).get(KEY) or {})
    now = time.time()
    pending = sorted(((k, v) for k, v in queue.items() if v.get("due", 0) <= now),
                     key=lambda item: (item[1].get("requested_at", 0), item[0]))
    if not pending:
        return None
    intent_key, intent = pending[0]
    source_id = intent.get("source_id")
    target = next((t for t in reads._targets_for_keys(grant, {intent.get("public_key")} & keys, recent_only=False)
                   if t.slack_id == source_id and t.channel_id == intent.get("channel_id")), None)
    valid = (target is not None and intent.get("public_key") in keys
             and now - intent.get("requested_at", 0) < 7 * 86400
             and intent.get("authority") == reads._cache_key(authority, target)
             and reads._timestamp(intent.get("source_ts")) is not None)
    if valid:
        scope = {"im": "im:write", "mpim": "mpim:write", "private_channel": "groups:write"}.get(target.kind, "channels:write")
        valid = {scope, target.read_scope}.issubset(authority.scopes)
    if valid:
        with transaction.atomic():
            reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
            device = _locked_active_verified_device(authority.user_id, intent["public_key"])
            valid = device is not None and intent.get("device") == {
                "device_id": str(device.pk), "verified_at": str(device.verified_at),
            }
    result = None
    error = None
    if valid:
        try:
            result = reads.apply_read(authority, target, source_ts=intent["source_ts"], required={scope, target.read_scope}, public_key=intent["public_key"], device_binding=intent["device"])
        except Exception as exc:
            error = exc
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        current = dict((connection.sync_cursor or {}).get(KEY) or {})
        # A newer foreground intent must survive completion of this attempt.
        if current.get(intent_key) == intent:
            if not valid or (result or {}).get("synced"):
                current.pop(intent_key, None)
            else:
                current[intent_key] = {**intent, "due": now + max(1, getattr(error, "retry_after", 30)),
                                       "error": type(error).__name__ if error else "source_cursor_unavailable"}
            _save(connection, current)
    if error is not None:
        # Back off this intent only. A permanently inaccessible conversation
        # must not pause every unread refresh for this owner for seven days.
        return None
    return 1 if (result or {}).get("synced") else 0
