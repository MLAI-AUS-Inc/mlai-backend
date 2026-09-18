"""Owner-scoped Slack read cursors; never infer 'read' from absent API fields."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import re
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from rest_framework.exceptions import ValidationError
from slack_sdk.errors import SlackApiError

from integrations.models import CommunityBridgeChannel
from integrations.services.slack_chat_catalog import (
    OWNER_OPENED_KEY,
    catalog_conversations,
    conversation_activity_at,
    conversation_kind,
    conversation_metadata,
    owner_open_intent,
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
    SlackDmMirrorRateLimited,
)
from integrations.services.message_sync.scheduler import BudgetDeferred


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
    latest = details.get("latest") or {}
    # conversations.info.latest can itself be a join/topic control event. It
    # must not become an unreachable read frontier for the visible timeline.
    visible_subtypes = {"", "bot_message", "file_share", "me_message", "thread_broadcast"}
    latest_ts = (
        latest.get("ts")
        if not latest.get("hidden") and str(latest.get("subtype") or "") in visible_subtypes
        else None
    )
    latest_stamp = max(_timestamp(latest_ts) or Decimal(0), read_at)
    unread = []
    for message in messages:
        # Slack history also contains joins, leaves, topic changes and hidden
        # control messages. The importer does not show these as conversation
        # posts, so they must not create a badge for an apparently empty chat.
        if message.get("hidden") or str(message.get("subtype") or "") not in visible_subtypes:
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


def _targets(grant, public_key, *, recent_only=True):
    key = str(public_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValidationError({"device": "Use a verified MLAI Chat device."})
    return _targets_for_keys(grant, {key}, recent_only=recent_only)


def _targets_for_keys(grant, keys, *, recent_only=True):
    """Internal account sweep over currently verified, provisioned devices."""
    if not keys:
        return []
    targets = []
    days = _grant_history_days(grant) if recent_only else 0
    oldest = int(time.time() - days * 86400) if days else None
    conversations = catalog_conversations(
        grant.conversations.filter(
            status="live", mlai_channel_id__isnull=False
        ).order_by("-latest_synced_ts", "pk")
    )
    for conversation in conversations:
        kind = conversation_kind(conversation)
        if not keys.intersection(conversation.participant_buzz_pubkeys or []):
            continue
        if kind == "private_channel" and not private_channels_enabled(grant):
            continue
        if oldest is not None:
            # Prewarm recent imports without spending Slack quota on the full
            # historical directory or waiting for their delivery queue to drain.
            activity = conversation_activity_at(conversation)
            if activity is None:
                if (kind != "im" or not any(
                        conversation_metadata(conversation).get(OWNER_OPENED_KEY) == owner_open_intent(grant, key)
                        for key in keys)):
                    continue
            elif datetime.fromisoformat(activity).timestamp() < oldest:
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
        # Slack's unread cursor may be zero for a never-read conversation.
        # Omit an unbounded oldest, as in the archive worker's source request.
        **({"oldest": format(oldest, "f")} if oldest else {}),
        inclusive=False,
        limit=100,
    )
    return (
        response.get("messages") or [],
        "slack_history",
        bool(response.get("has_more") or limited),
    )


def _pending_key(authority, target):
    return _cache_key(authority, target) + ":pending-info"


def refresh_target(grant, authority, target):
    """Fetch one source snapshot; persist no message content or guessed zeroes.

    A metadata-only checkpoint lets a group finish its history lookup after a
    budget pause without repeatedly spending the conversations.info allowance.
    """
    key = _cache_key(authority, target)
    pending_key = _pending_key(authority, target)
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        pending = cache.get(pending_key)
        receipt = cache.get(key + ":receipt")
    observed_at = time.time()
    if pending and observed_at - pending["fetched_at"] < 30:
        details, observed_at = pending["details"], pending["fetched_at"]
    else:
        try:
            response = _call_slack_with_grant_authority(
                authority, "conversations_info", required_scopes={target.read_scope},
                channel=target.slack_id,
            )
        except SlackApiError as exc:
            if exc.response.get("error") not in {"channel_not_found", "not_in_channel"}:
                raise
            response = {"channel": {"id": target.slack_id, "is_member": False}}
        details = response.get("channel") or {}
    if details.get("id") != target.slack_id:
        raise SlackDmMirrorError("Slack returned a different conversation.")
    if _is_external_shared_conversation(details) or (
        target.kind in {"public_channel", "private_channel", "mpim"}
        and details.get("is_member") is not True
    ) or details.get("is_member") is False:
        cached = {"available": False}
    else:
        try:
            messages, count_source, partial = _unread_messages(authority, target, details.get("last_read", "0"))
        except (BudgetDeferred, SlackDmMirrorRateLimited):
            # The allowlist excludes text, profiles, topic and private URLs.
            safe = {name: details[name] for name in ("id", "is_member", "last_read", "unread_count_display") if name in details}
            latest = details.get("latest") or {}
            safe["latest"] = {name: latest[name] for name in ("ts", "hidden", "subtype") if name in latest}
            with transaction.atomic():
                _lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
                if cache.get(key + ":receipt") == receipt:
                    cache.set(pending_key, {"details": safe, "fetched_at": observed_at}, timeout=30)
            raise
        snapshot = read_state_snapshot(details, kind=target.kind, owner_id=grant.slack_user_id, messages=messages)
        if snapshot is not None:
            snapshot["fetched_at"] = observed_at
            if snapshot["count_source"] != "slack":
                snapshot["count_source"] = count_source
                if partial:
                    snapshot["unread_count"] = None
                    if not snapshot["is_unread"]:
                        snapshot = None
        cached = {"available": snapshot is not None, **(snapshot or {})}
    cached.setdefault("fetched_at", observed_at)
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        if cache.get(key + ":receipt") != receipt:
            # A confirmed read overtook this source request; retry a fresh read.
            raise BudgetDeferred(1)
        cache.set(key, cached, timeout=24 * 60 * 60)
        cache.delete(pending_key)
    return cached


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
    if getattr(settings, "MESSAGE_SYNC_ENABLED", False):
        targets = [t for t in targets if t.read_scope in authority.scopes]
        with transaction.atomic():
            _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
            stored = cache.get_many([_cache_key(authority, t) for t in targets])
        return {
            "channels": {t.channel_id: stored[_cache_key(authority, t)] for t in targets
                         if _cache_key(authority, t) in stored},
            "authorized_channel_ids": [t.channel_id for t in targets],
            "snapshot_complete": len(stored) == len(targets),
            "next_cursor": None,
            "retry_after_seconds": 10,
        }
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
    retry_after = 0
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
                cached = refresh_target(grant, authority, target)
            except (BudgetDeferred, SlackDmMirrorRateLimited) as exc:
                retry_after = getattr(exc, "retry_after", 60)
                break
        results[target.channel_id] = cached
        index += 1
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
    return {
        "channels": {**bootstrap, **results},
        "authorized_channel_ids": [t.channel_id for t in targets],
        "next_cursor": str(index) if index < len(targets) else None,
        "retry_after_seconds": max(retry_after, 10 if index < len(targets) else 60),
    }


def mark_read(user, *, public_key, channel_id, source_ts):
    """Advance the owner's Slack cursor only through a message the app displayed."""
    stamp = _timestamp(source_ts)
    if stamp is None or stamp <= 0:
        raise ValidationError({"source_ts": "Use a valid Slack message timestamp."})
    grant = active_grant_for_user(user)
    _assert_grant_connection_authorized(grant)
    target = next(
        # A user can read a new message before discovery updates its activity.
        # Keep explicit acknowledgements independent of the polling window.
        (t for t in _targets(grant, public_key, recent_only=False)
         if t.channel_id == str(channel_id)),
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
    device_binding = None
    if getattr(settings, "MESSAGE_SYNC_ENABLED", False):
        from .message_sync.receipts import enqueue_read, complete_read
        device_binding = enqueue_read(authority, target, public_key=public_key, source_ts=str(source_ts))
    try:
        result = apply_read(authority, target, source_ts=str(source_ts), required=required, public_key=public_key, device_binding=device_binding)
    except (BudgetDeferred, SlackDmMirrorRateLimited) as exc:
        if not getattr(settings, "MESSAGE_SYNC_ENABLED", False):
            raise
        return {"synced": False, "pending": True,
                "retry_after_seconds": getattr(exc, "retry_after", 60)}
    if getattr(settings, "MESSAGE_SYNC_ENABLED", False) and result.get("synced"):
        complete_read(authority, target, source_ts=result["last_read"])
    return result


def apply_read(authority, target, *, source_ts, required, public_key=None, device_binding=None):
    """Confirm a source read and immediately share the same result with peers."""
    stamp = _timestamp(source_ts)
    # Serialize competing device reads and the read-before-write check with the
    # same owner consent lock used by the bridge. Never move a cursor backwards.
    with transaction.atomic():
        _lock_slack_grant_api_authority(authority, required_scopes=required)
        if public_key is not None:
            from .slack_dm_mirror import _locked_active_verified_device
            device = _locked_active_verified_device(authority.user_id, public_key)
            if device is None or (device_binding is not None and device_binding != {
                "device_id": str(device.pk), "verified_at": str(device.verified_at),
            }):
                raise SlackDmMirrorError("The requesting device is no longer verified.")
        response = _call_slack_with_grant_authority(
            authority,
            "conversations_info",
            required_scopes=required,
            channel=target.slack_id,
        )
        details = response.get("channel") or {}
        if details.get("id") != target.slack_id or _is_external_shared_conversation(
            details
        ) or details.get("is_member") is False or (
            target.kind != "im" and details.get("is_member") is not True
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
        confirmed_at = time.time()
        confirmed = max(previous, stamp)
        key = _cache_key(authority, target)
        cached = cache.get(key) or {}
        source_latest = details.get("latest") or {}
        latest_visible = not source_latest.get("hidden") and str(source_latest.get("subtype") or "") in {"", "bot_message", "file_share", "me_message", "thread_broadcast"}
        if source_latest.get("thread_ts") not in (None, "", source_latest.get("ts")) and not (
            source_latest.get("broadcast") or source_latest.get("reply_broadcast")
            or source_latest.get("subtype") == "thread_broadcast"
        ):
            latest_visible = False
        source_latest_ts = (_timestamp(source_latest.get("ts")) if latest_visible else None) or Decimal(0)
        latest = max(source_latest_ts,
                     _timestamp(cached.get("latest_ts")) or Decimal(0))
        # Confirmation advances a frontier; it does not prove a partial read
        # cleared every unread. Unknown remainders never become invented zeroes.
        proven_clear = bool(source_latest_ts and confirmed >= latest) or (
            target.kind == "im" and details.get("unread_count_display") == 0
        )
        proven_unread = not proven_clear and bool(source_latest_ts > confirmed and source_latest.get("user")
                             and source_latest["user"] != authority.slack_user_id)
        snapshot = {"available": proven_clear or proven_unread, "last_read": format(confirmed, "f"),
                    "latest_ts": format(max(latest, confirmed), "f"),
                    "is_unread": proven_unread,
                    "unread_count": 0 if proven_clear else None,
                    "count_source": "confirmed_read", "fetched_at": confirmed_at,
                    "confirmed_at": confirmed_at, "refresh_required": True}
        if stamp <= previous:
            source_snapshot = read_state_snapshot(details, kind=target.kind, messages=[], owner_id=authority.slack_user_id)
            if target.kind == "im" and source_snapshot is not None:
                snapshot.update(source_snapshot, available=True, confirmed_at=confirmed_at)
        cache.set(key + ":receipt", uuid.uuid4().hex, timeout=86400)
        cache.set(key, snapshot, timeout=86400)
        cache.delete(_pending_key(authority, target))
    return {
        "synced": True,
        "last_read": format(confirmed, "f"),
        "confirmed_at": confirmed_at,
        "channels": {target.channel_id: snapshot},
    }
