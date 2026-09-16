"""One-page public history/head/thread repair with atomic durable checkpoints."""
import hashlib
import json
import time

from django.db import transaction
from django.utils import timezone

from integrations.models import BridgeSyncState, CommunityBridgeChannel, SlackDmMirrorConversation
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.store import ingest_slack_event
from .coverage import record_page
from .history_policy import history_page_limit
from .scheduler import locked_job, schedule_job, finish_job


def ensure_state(owner):
    """Resolve metadata by exact public mapping or owner-private conversation."""
    private = isinstance(owner, SlackDmMirrorConversation)
    key = "private_conversation" if private else "public_channel"
    state, _ = BridgeSyncState.objects.get_or_create(
        **{key: owner}, defaults={
            "workspace_id": owner.slack_workspace_id,
            "source_channel_id": owner.slack_conversation_id if private else owner.slack_channel_id,
        },
    )
    schedule_job(state, "head")
    schedule_job(state, "archive")
    return state


def seed_states(limit=100):
    """Incrementally discover every eligible mapping without relying on login."""
    public = CommunityBridgeChannel.objects.filter(
        enabled=True, destination_platform="buzz", sync_state__isnull=True,
    ).order_by("id")[:limit]
    private = SlackDmMirrorConversation.objects.filter(
        status="live", grant__status="active", grant__revoked_at__isnull=True,
        sync_state__isnull=True,
    ).order_by("id")[:limit]
    for owner in list(public) + list(private):
        with transaction.atomic():
            state = ensure_state(owner)
            schedule_job(state, "head")
            schedule_job(state, "archive")
    # Durable mode replaces the legacy history loop, including its recovery
    # scheduler. Keep source recovery alive without depending on a user login.
    from .recovery import schedule_private_recoveries
    schedule_private_recoveries(limit=min(5, max(1, limit)))


def timestamp(value):
    """Compare Slack timestamps without losing microseconds to float rounding."""
    parts = str(value).split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts) or len(parts[1]) != 6:
        raise ValueError("invalid_slack_timestamp")
    return int(parts[0]), int(parts[1])


def next_checkpoint(checkpoint, response, *, thread=False):
    """Advance only from source pagination evidence, never from rendered rows."""
    cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
    has_more = bool(response.get("has_more") or cursor)
    updated = dict(checkpoint)
    if cursor:
        if cursor == checkpoint.get("cursor"):
            raise RuntimeError("slack_history_cursor_stalled")
        updated["cursor"] = cursor
    elif has_more:
        if thread:
            raise RuntimeError("slack_thread_cursor_missing")
        ids = [str(item.get("ts")) for item in response.get("messages", []) if isinstance(item, dict) and item.get("ts")]
        if not ids:
            raise RuntimeError("slack_history_page_empty")
        oldest = min(ids, key=timestamp)
        if checkpoint.get("latest") and timestamp(oldest) >= timestamp(checkpoint["latest"]):
            raise RuntimeError("slack_history_page_stalled")
        updated["latest"] = oldest
        updated.pop("cursor", None)
    return updated, not has_more


def public_page(lease, state):
    channel = state.public_channel
    checkpoint = dict(lease.checkpoint)
    # Each sweep fixes its upper bound so an active channel cannot keep a scan
    # perpetually open. New arrivals are covered by callbacks and the next head.
    checkpoint.setdefault("upper_bound", f"{int(time.time())}.999999")
    if lease.kind == "head":
        checkpoint.setdefault("oldest", f"{max(0, int(time.time()) - 86400)}.000000")
    else:
        checkpoint.setdefault("oldest", "0.000000")
    kwargs = dict(channel=channel.slack_channel_id, limit=history_page_limit(), inclusive=False,
                  latest=checkpoint.get("latest", checkpoint["upper_bound"]))
    # Slack rejects an explicit decimal-zero oldest timestamp. Its documented
    # unbounded request omits oldest; the durable range retains its zero marker.
    if timestamp(checkpoint["oldest"]) != (0, 0):
        kwargs["oldest"] = checkpoint["oldest"]
    if checkpoint.get("cursor"):
        kwargs["cursor"] = checkpoint["cursor"]
    client = SlackBridgeClient.get_client()
    read_started_at = timezone.now()
    if lease.kind == "thread":
        kwargs["ts"] = lease.source_object_key
        response = client.conversations_replies(**kwargs)
    else:
        response = client.conversations_history(**kwargs)
    if not response.get("ok"):
        raise RuntimeError("slack_history_response_failed")
    updated, complete = next_checkpoint(checkpoint, response, thread=lease.kind == "thread")
    with transaction.atomic():
        current, _ = locked_job(lease)
        # Mapping identity changes are not permission to finish an old scan
        # into a new destination. Re-read the mapping while the page commits.
        mapped = CommunityBridgeChannel.objects.select_for_update().get(pk=channel.pk)
        if (not mapped.enabled or mapped.slack_channel_id != channel.slack_channel_id
                or mapped.slack_workspace_id != channel.slack_workspace_id
                or mapped.destination_channel_id != channel.destination_channel_id
                or mapped.destination_workspace_id != channel.destination_workspace_id):
            raise RuntimeError("sync_mapping_changed")
        messages = [item for item in response.get("messages") or [] if isinstance(item, dict) and item.get("ts")]
        for message in sorted(messages, key=lambda item: timestamp(item["ts"])):
            if not isinstance(message, dict) or not message.get("ts"):
                continue
            ts = str(message["ts"])
            event = {**message, "type": "message", "channel": channel.slack_channel_id, "channel_type": "channel"}
            if lease.kind == "thread" and ts != lease.source_object_key:
                event["thread_ts"] = lease.source_object_key
            from .public_repair import repair_observed_message
            repair_observed_message(mapped, event, read_started_at=read_started_at)
            digest = hashlib.sha256(json.dumps(
                [channel.slack_workspace_id, channel.slack_channel_id, ts], separators=(",", ":"),
            ).encode()).hexdigest()
            base = {"team_id": channel.slack_workspace_id, "event_id": f"sync:create:{digest}", "event": event}
            ingest_slack_event(base)
            edited = message.get("edited") or {}
            if edited.get("ts"):
                revision = str(edited["ts"])
                revision_key = hashlib.sha256(f"{digest}:{revision}".encode()).hexdigest()
                ingest_slack_event({**base, "event_id": f"sync:edit:{revision_key}", "event": {
                    "type": "message", "subtype": "message_changed", "channel": channel.slack_channel_id,
                    "channel_type": "channel", "event_ts": revision, "message": message,
                }})
            root = str(message.get("thread_ts") or ts)
            if message.get("reply_count") or message.get("latest_reply") or root != ts:
                schedule_job(current, "thread", source_object_key=root)
        updated = record_page(current, lease.kind, updated, response, complete=complete)
        finish_job(lease, checkpoint={} if complete else updated,
                   delay_seconds=({"head": 60, "thread": 3600, "archive": 86400}[lease.kind]) if complete else 0, complete=complete)
