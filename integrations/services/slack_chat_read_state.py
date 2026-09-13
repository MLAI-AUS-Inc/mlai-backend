"""Owner-scoped Slack read cursors; never infer 'read' from absent API fields."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re
import time

from django.core.cache import cache
from django.db import transaction
from rest_framework.exceptions import ValidationError
from slack_sdk.errors import SlackApiError

from integrations.models import CommunityBridgeChannel
from integrations.services.slack_chat_catalog import (
    catalog_conversations,
    conversation_kind,
    private_channels_enabled,
)
from integrations.services.slack_dm_mirror import (
    _assert_grant_connection_authorized,
    _call_slack_with_grant_authority,
    _capture_slack_grant_api_authority,
    _lock_slack_grant_api_authority,
    _is_external_shared_conversation,
    active_grant_for_user,
    _grant_history_days,
    SlackDmMirrorError,
)


@dataclass
class ReadTarget:
    """One channel already visible to the requesting account and device."""

    channel_id: str
    slack_id: str
    kind: str
    conversation: object = None
    bridge: object = None

    @property
    def read_scope(self):
        return {
            "im": "im:read",
            "mpim": "mpim:read",
            "private_channel": "groups:read",
        }.get(self.kind, "channels:read")


def _timestamp(value):
    try:
        stamp = Decimal(str(value))
        if stamp.is_finite() and 0 <= stamp <= Decimal(str(time.time() + 300)):
            return stamp
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def read_state_snapshot(details, *, kind, messages, owner_id):
    """Combine Slack's cursor with source messages, without fabricating counts.

    Slack exposes unread_count_display only for IMs. For other conversations,
    top-level source messages establish unread activity; numeric channel
    badges count explicit user/broadcast mentions, while group DMs count messages.
    Thread-only replies and the owner's own messages do not increment the list.
    """
    read_at = _timestamp(details.get("last_read"))
    if read_at is None:
        return None
    latest = (details.get("latest") or {}).get("ts")
    latest_stamp = max(_timestamp(latest) or Decimal(0), read_at)
    unread = []
    for message in messages:
        # Slack history also contains joins, leaves, topic changes and hidden
        # control messages. The importer does not show these as conversation
        # posts, so they must not create a badge for an apparently empty chat.
        if message.get("hidden") or str(message.get("subtype") or "") not in {
            "", "bot_message", "file_share", "me_message", "thread_broadcast"
        }:
            continue
        stamp = _timestamp(message.get("ts"))
        if stamp is None:
            continue
        thread = str(message.get("thread_ts") or "")
        if (
            thread
            and thread != str(message["ts"])
            and not (
                message.get("broadcast")
                or message.get("reply_broadcast")
                or message.get("subtype") == "thread_broadcast"
            )
        ):
            continue
        latest_stamp = max(latest_stamp, stamp)
        if stamp > read_at and message.get("user") != owner_id:
            unread.append(message)
    count = details.get("unread_count_display") if kind == "im" else None
    authoritative_count = type(count) is int and count >= 0
    if authoritative_count:
        is_unread = count > 0
        source = "slack"
    else:
        if kind == "im" and not unread:
            return None
        is_unread = bool(unread)
        count = (
            len(unread)
            if kind in {"im", "mpim"}
            else sum(
                bool(
                    re.search(
                        r"<@"
                        + re.escape(owner_id)
                        + r"(?:\|[^>]+)?>|<!(?:channel|here|everyone)(?:\|[^>]+)?>",
                        str(m.get("text") or ""),
                    )
                )
                for m in unread
            )
        )
        source = "imported_messages"
    return {
        "last_read": str(details["last_read"]),
        "latest_ts": format(latest_stamp, "f"),
        "is_unread": is_unread,
        "unread_count": count,
        "count_source": source,
        "fetched_at": time.time(),
    }


def _targets(grant, public_key):
    key = str(public_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValidationError({"device": "Use a verified MLAI Chat device."})
    targets = []
    conversations = catalog_conversations(
        grant.conversations.filter(
            status="live", mlai_channel_id__isnull=False
        ).order_by("-latest_synced_ts", "pk")
    )
    for conversation in conversations:
        kind = conversation_kind(conversation)
        if key not in (conversation.participant_buzz_pubkeys or []):
            continue
        if kind == "private_channel" and not private_channels_enabled(grant):
            continue
        targets.append(
            ReadTarget(
                str(conversation.mlai_channel_id),
                conversation.slack_conversation_id,
                kind,
                conversation=conversation,
            )
        )
    # The owner's user token separately verifies Slack membership for public
    # bridge channels. A bot's read position is never used for a member.
    public = [
        ReadTarget(
            c.destination_channel_id, c.slack_channel_id, "public_channel", bridge=c
        )
        for c in CommunityBridgeChannel.objects.filter(
            slack_workspace_id=grant.slack_workspace_id,
            destination_platform="buzz",
            enabled=True,
        )
        .exclude(destination_channel_id="")
        .order_by("slack_channel_id")
    ]
    # Public channels first, then the most recently active private conversations.
    return public + targets


def _cache_key(authority, target):
    scope = ":".join(
        str(v)
        for v in (
            authority.user_id,
            authority.grant_id,
            authority.connection_id,
            authority.consent_generation,
            authority.oauth_generation,
            authority.workspace_id,
            authority.slack_user_id,
            target.slack_id,
        )
    )
    return "slack-chat-read-v1:" + hashlib.sha256(scope.encode()).hexdigest()


def _unread_messages(authority, target, last_read):
    if target.kind == "im":
        return [], "slack", False
    # Read positions must not depend on the import being caught up. Completed
    # private deliveries also erase their bodies, including mention entities.
    # Inspect only source unread messages and retain only counts/cursors.
    oldest = _timestamp(last_read)
    if oldest is None:
        return [], "unknown", False
    days = (
        _grant_history_days(target.conversation.grant)
        if target.conversation is not None
        else 0
    )
    limited = bool(days and oldest < Decimal(str(time.time() - days * 86400)))
    if limited:
        oldest = Decimal(str(time.time() - days * 86400))
    response = _call_slack_with_grant_authority(
        authority,
        "conversations_history",
        required_scopes={
            {"private_channel": "groups:history", "mpim": "mpim:history"}.get(
                target.kind, "channels:history"
            )
        },
        channel=target.slack_id,
        oldest=format(oldest, "f"),
        inclusive=False,
        limit=100,
    )
    return (
        response.get("messages") or [],
        "slack_history",
        bool(response.get("has_more") or limited),
    )


def read_state_page(user, *, public_key, cursor=0, channel_ids=None):
    """Refresh a bounded page, preserving unknown state and Slack rate limits."""
    try:
        offset = int(cursor or 0)
        if offset < 0:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValidationError({"cursor": "Use a non-negative page cursor."}) from exc
    grant = active_grant_for_user(user)
    _assert_grant_connection_authorized(grant)
    targets = _targets(grant, public_key)
    if channel_ids is not None:
        if not isinstance(channel_ids, list) or len(channel_ids) > 4:
            raise ValidationError(
                {"channel_ids": "Choose at most four visible conversations."}
            )
        requested = {str(value) for value in channel_ids}
        targets = [target for target in targets if target.channel_id in requested]
        offset = 0
    authority = _capture_slack_grant_api_authority(grant)
    # A cold app launch receives all previously checked cursors immediately;
    # refreshing a large Slack directory must not hide already known unreads.
    bootstrap = {}
    if offset == 0 and channel_ids is None:
        with transaction.atomic():
            _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
            cached_states = cache.get_many([_cache_key(authority, t) for t in targets])
        bootstrap = {
            t.channel_id: cached_states[_cache_key(authority, t)]
            for t in targets
            if _cache_key(authority, t) in cached_states
        }
    results = {}
    deadline = time.monotonic() + 8
    calls = 0
    index = offset
    while index < len(targets) and len(results) < 50:
        target = targets[index]
        key = _cache_key(authority, target)
        with transaction.atomic():
            _lock_slack_grant_api_authority(
                authority, required_scopes={target.read_scope}
            )
            cached = cache.get(key)
        fresh = cached is not None and time.time() - cached.get("fetched_at", 0) < (
            30 if channel_ids is not None else 60
        )
        if not fresh:
            if calls >= 4 or time.monotonic() >= deadline:
                break
            calls += 1
            try:
                response = _call_slack_with_grant_authority(
                    authority,
                    "conversations_info",
                    required_scopes={target.read_scope},
                    channel=target.slack_id,
                )
            except SlackApiError as exc:
                if exc.response.get("error") not in {
                    "channel_not_found",
                    "not_in_channel",
                }:
                    raise
                results[target.channel_id] = {"available": False}
                index += 1
                continue
            observed_at = time.time()
            details = response.get("channel") or {}
            if details.get("id") != target.slack_id:
                raise SlackDmMirrorError("Slack returned a different conversation.")
            if _is_external_shared_conversation(details) or (
                target.kind in {"public_channel", "private_channel", "mpim"}
                and details.get("is_member") is not True
            ):
                cached = {"available": False}
            else:
                messages, count_source, partial = _unread_messages(
                    authority,
                    target,
                    details.get("last_read", "0"),
                )
                snapshot = read_state_snapshot(
                    details,
                    kind=target.kind,
                    owner_id=grant.slack_user_id,
                    messages=messages,
                )
                if snapshot is not None:
                    snapshot["fetched_at"] = observed_at
                if snapshot is not None and snapshot["count_source"] != "slack":
                    snapshot["count_source"] = count_source
                    if partial:
                        snapshot["unread_count"] = None
                        if not snapshot["is_unread"]:
                            snapshot = None
                if (
                    snapshot is not None
                    and not snapshot["is_unread"]
                    and snapshot["count_source"] == "imported_messages"
                    and target.conversation is not None
                    and target.conversation.history_backfilled_at is None
                ):
                    snapshot = None
                cached = {"available": snapshot is not None, **(snapshot or {})}
            with transaction.atomic():
                _lock_slack_grant_api_authority(
                    authority, required_scopes={target.read_scope}
                )
                cached.setdefault("fetched_at", time.time())
                cache.set(key, cached, timeout=24 * 60 * 60)
        results[target.channel_id] = cached
        index += 1
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
    return {
        "channels": {**bootstrap, **results},
        "next_cursor": str(index) if index < len(targets) else None,
        "retry_after_seconds": 10 if index < len(targets) else 60,
    }


def mark_read(user, *, public_key, channel_id, source_ts):
    """Advance the owner's Slack cursor only through a message the app displayed."""
    stamp = _timestamp(source_ts)
    if stamp is None or stamp <= 0:
        raise ValidationError({"source_ts": "Use a valid Slack message timestamp."})
    grant = active_grant_for_user(user)
    _assert_grant_connection_authorized(grant)
    target = next(
        (t for t in _targets(grant, public_key) if t.channel_id == str(channel_id)),
        None,
    )
    if target is None:
        raise ValidationError(
            {"channel_id": "Slack conversation is not available to this device."}
        )
    scope = {
        "im": "im:write",
        "mpim": "mpim:write",
        "private_channel": "groups:write",
    }.get(target.kind, "channels:write")
    if scope not in (grant.connection.scopes or []):
        return {"synced": False, "needs_reauthorization": True}
    authority = _capture_slack_grant_api_authority(grant)
    required = {scope, target.read_scope}
    # Serialize competing device reads and the read-before-write check with the
    # same owner consent lock used by the bridge. Never move a cursor backwards.
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes=required)
        response = _call_slack_with_grant_authority(
            authority,
            "conversations_info",
            required_scopes=required,
            channel=target.slack_id,
        )
        details = response.get("channel") or {}
        if details.get("id") != target.slack_id or _is_external_shared_conversation(
            details
        ):
            raise SlackDmMirrorError("Slack conversation is not available.")
        previous = _timestamp(details.get("last_read"))
        if previous is None:
            return {"synced": False, "needs_reauthorization": False}
        if stamp > previous:
            _call_slack_with_grant_authority(
                authority,
                "conversations_mark",
                required_scopes=required,
                channel=target.slack_id,
                ts=str(source_ts),
            )
        cache.delete(_cache_key(authority, target))
    return {"synced": True, "last_read": format(max(previous, stamp), "f")}
