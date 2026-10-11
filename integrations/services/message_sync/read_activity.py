"""Content-free public activity for existing mapped-room unread scheduling."""
from decimal import Decimal, InvalidOperation
import time

from django.db import transaction


def latest_activity(messages, *, now=None):
    """Return a visible top-level source timestamp, never a guessed unread."""
    now = time.time() if now is None else now
    stamps = []
    for raw in messages:
        message = raw.get("message") if raw.get("subtype") == "message_changed" else raw
        if not isinstance(message, dict) or message.get("hidden"):
            continue
        if str(message.get("subtype") or "") not in {"", "bot_message", "file_share", "me_message", "thread_broadcast"}:
            continue
        if message.get("thread_ts") not in (None, "", message.get("ts")) and not (
            message.get("broadcast") or message.get("reply_broadcast")
            or message.get("subtype") == "thread_broadcast"
        ):
            continue
        for value in (message.get("ts"), raw.get("event_ts")):
            try:
                stamp = Decimal(str(value))
                if stamp.is_finite() and 0 < stamp <= Decimal(str(now + 300)):
                    stamps.append(stamp)
            except (InvalidOperation, ValueError, TypeError):
                pass
    return format(max(stamps), "f") if stamps else ""


def advance_public_activity(state, messages):
    """Advance the already-locked public mapping frontier monotonically."""
    if not state.public_channel_id or state.private_conversation_id:
        return
    stamp = latest_activity(messages)
    if not stamp:
        return
    previous = latest_activity([{"ts": state.latest_source_activity}])
    if not previous or Decimal(stamp) > Decimal(previous):
        state.latest_source_activity = stamp
        state.save(update_fields=["latest_source_activity"])


def record_public_event(payload):
    """Signal only an explicitly mapped public room from a verified callback.

    This stores a timestamp, not an owner cursor or recipient list. Every owner
    still verifies current membership with their own token before publishing
    read state. Bot messages may schedule that check without importing a body.
    """
    event = payload.get("event") or {}
    if (event.get("type") != "message" or event.get("channel_type") not in (None, "channel")
            or not latest_activity([event])):
        return
    from integrations.models import CommunityBridgeChannel, BridgeSyncState
    from .history import ensure_state
    channel = CommunityBridgeChannel.objects.filter(
        slack_workspace_id=payload.get("team_id"), slack_channel_id=event.get("channel"),
        destination_platform="buzz", enabled=True,
    ).exclude(destination_channel_id="").first()
    if channel is None:
        return
    with transaction.atomic():
        state = BridgeSyncState.objects.select_for_update().filter(public_channel=channel).first()
        if state is None:
            created = ensure_state(channel)
            # A competing seed may have won get_or_create; its returned row
            # is not guaranteed to be locked by this transaction.
            state = BridgeSyncState.objects.select_for_update().get(pk=created.pk)
        # Match the history worker's state -> channel lock order.
        mapped = CommunityBridgeChannel.objects.select_for_update().filter(
            pk=channel.pk, enabled=True, destination_platform="buzz",
            slack_workspace_id=channel.slack_workspace_id, slack_channel_id=channel.slack_channel_id,
            destination_channel_id=channel.destination_channel_id,
        ).first()
        if mapped is None:
            return
        if state.workspace_id != channel.slack_workspace_id or state.source_channel_id != channel.slack_channel_id:
            return
        advance_public_activity(state, [event])
        from .head_repair import wake_head_locked
        wake_head_locked(state)


def delivery_activity(delivery, link):
    """Return the actual Slack time of a completed countable mapped delivery.

    Only confirmed destination identities count for MLAI-originated messages;
    their Nostr creation time cannot stand in for Slack's ordering domain.
    """
    if (delivery.status != "completed" or delivery.delivery_type != "create"
            or link is None or link.source_deleted_at or link.destination_deleted_at):
        return ""
    payload = delivery.payload or {}
    metadata = payload.get("metadata") or {}
    parent = delivery.source_parent_message_id
    if parent and not metadata.get("broadcast"):
        return ""
    if delivery.source_platform == "slack" and delivery.target_platform == "buzz":
        stamp = delivery.source_message_id
    elif delivery.source_platform == "buzz" and delivery.target_platform == "slack":
        stamp = link.destination_message_id
    else:
        return ""
    return latest_activity([{"ts": stamp}])


def record_public_delivery(delivery_id):
    """Advance public activity after delivery commit, under state->mapping locks.

    The completed outbox and its delivery link are durable evidence. Repeating
    this hook is harmless; the timestamp frontier only advances. No Slack call
    or user read position is involved.
    """
    from django.conf import settings
    if not getattr(settings, "MESSAGE_SYNC_TARGETED_READ_POLLING", False):
        return
    from integrations.models import (
        BridgeSyncState, CommunityBridgeChannel, CommunityBridgeDelivery,
        CommunityBridgeMessageLink,
    )
    from .history import ensure_state
    delivery = CommunityBridgeDelivery.objects.select_related("channel").filter(
        pk=delivery_id, status="completed", delivery_type="create",
    ).first()
    if delivery is None or delivery.channel is None:
        return
    channel = delivery.channel
    with transaction.atomic():
        state = BridgeSyncState.objects.select_for_update().filter(public_channel=channel).first()
        if state is None:
            created = ensure_state(channel)
            state = BridgeSyncState.objects.select_for_update().get(pk=created.pk)
        mapped = CommunityBridgeChannel.objects.select_for_update().filter(
            pk=channel.pk, enabled=True, destination_platform="buzz",
            slack_workspace_id=channel.slack_workspace_id,
            slack_channel_id=channel.slack_channel_id,
            destination_channel_id=channel.destination_channel_id,
        ).first()
        if (mapped is None or state.workspace_id != channel.slack_workspace_id
                or state.source_channel_id != channel.slack_channel_id):
            return
        current = CommunityBridgeDelivery.objects.select_for_update().filter(
            pk=delivery.pk, channel=mapped, status="completed", delivery_type="create",
        ).first()
        if current is None:
            return
        source_channel = (mapped.slack_channel_id if current.source_platform == "slack"
                          else mapped.destination_channel_id)
        target_channel = (mapped.destination_channel_id if current.target_platform == "buzz"
                          else mapped.slack_channel_id)
        if (current.source_channel_id != source_channel
                or current.target_channel_id != target_channel):
            return
        link = CommunityBridgeMessageLink.objects.filter(
            channel=mapped, source_platform=current.source_platform,
            source_channel_id=current.source_channel_id,
            source_message_id=current.source_message_id,
            destination_platform=current.target_platform,
            destination_channel_id=target_channel,
        ).first()
        stamp = delivery_activity(current, link)
        if stamp:
            advance_public_activity(state, [{"ts": stamp}])
