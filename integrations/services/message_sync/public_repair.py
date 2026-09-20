"""Repair observable message content and supported reactions from source pages.

Missing history rows alone never imply deletion: retention, permissions and
partial API results can all omit a row. Only explicit source deletions use the
delete path. Partial reaction user lists permit additions, never inferred removal.
"""
import hashlib
import json

from integrations.models import CommunityBridgeDelivery
from integrations.services.community_bridge.store import (
    _canonicalize_event, _normalize_slack_event, ingest_slack_event,
)
from integrations.services.community_bridge.formatting import reaction_object_id


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _content(payload):
    metadata = payload.get("metadata") or {}
    return [payload.get("text", ""), payload.get("attachments", []),
            payload.get("source_parent_message_id", ""), bool(metadata.get("broadcast"))]


def repair_observed_message(channel, message, *, read_started_at):
    """Queue idempotent corrections, avoiding callbacks newer than this read."""
    ts = str(message["ts"])
    base = {"team_id": channel.slack_workspace_id, "event": {
        **message, "type": "message", "channel": channel.slack_channel_id, "channel_type": "channel",
    }}
    normalized = _normalize_slack_event(base)
    if normalized is None:
        return
    canonical = _canonicalize_event(receipt_key="sync-observation", source_platform="slack",
        source_channel_id=channel.slack_channel_id, normalized_event=normalized)
    latest = CommunityBridgeDelivery.objects.filter(channel=channel, source_platform="slack", target_platform="buzz",
        source_message_id=ts, delivery_type__in=["create", "edit", "delete"]).order_by("-id").first()
    if latest is not None and latest.created_at <= read_started_at and latest.delivery_type != "delete":
        if _content(latest.payload or {}) != _content(canonical):
            receipt = _digest([channel.pk, ts, latest.pk, _content(canonical)])
            revision = str((message.get("edited") or {}).get("ts") or ts)
            ingest_slack_event({**base, "event_id": f"sync:content:{receipt}", "event": {
                "type": "message", "subtype": "message_changed", "channel": channel.slack_channel_id,
                "channel_type": "channel", "event_ts": revision, "message": message,
            }})
    _repair_reactions(channel, message, read_started_at)


def _repair_reactions(channel, message, read_started_at):
    ts = str(message["ts"])
    raw = message.get("reactions", [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) or not item.get("name") for item in raw):
        return
    observed = {str(item["name"]): item for item in raw}
    known = {}
    for delivery in CommunityBridgeDelivery.objects.filter(channel=channel, source_platform="slack", target_platform="buzz",
        source_parent_message_id=ts, delivery_type__in=["reaction_add", "reaction_remove"]).order_by("-id").iterator(chunk_size=100):
        known.setdefault(delivery.source_message_id, delivery)
    desired = {}
    for name, item in observed.items():
        users = item.get("users")
        if not isinstance(users, list):
            continue
        for author in users:
            if isinstance(author, str) and author:
                desired[reaction_object_id(message_id=ts, reaction=name, author_id=author)] = (name, author, True)
    for key, delivery in known.items():
        payload = delivery.payload or {}
        name = str((payload.get("metadata") or {}).get("slack_reaction") or "")
        author = str(payload.get("source_author_id") or "")
        item = observed.get(name)
        # Slack may truncate users even when it supplies a full count. Absence
        # is meaningful only when the reaction itself is absent or the list is complete.
        complete = item is None or (isinstance(item.get("users"), list) and all(isinstance(user, str) for user in item["users"])
            and isinstance(item.get("count"), int) and item["count"] == len(set(item["users"])))
        if name and author and key not in desired and complete:
            desired[key] = (name, author, False)
    for key, (name, author, present) in desired.items():
        previous = known.get(key)
        if previous is not None:
            if previous.created_at > read_started_at or (previous.delivery_type == "reaction_add") == present:
                continue
        elif not present:
            continue
        receipt = _digest([channel.pk, key, previous.pk if previous else None, present])
        # Observation time orders repairs against late reaction callbacks.
        revision = f"{int(read_started_at.timestamp())}.{read_started_at.microsecond:06d}"
        ingest_slack_event({"team_id": channel.slack_workspace_id, "event_id": f"sync:reaction:{receipt}", "event": {
            "type": "reaction_added" if present else "reaction_removed", "user": author, "reaction": name,
            "event_ts": revision, "item": {"type": "message", "channel": channel.slack_channel_id, "ts": ts},
        }})
