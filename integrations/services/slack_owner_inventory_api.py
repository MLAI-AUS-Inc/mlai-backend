"""Device-scoped, content-free views of the owner's Slack source directory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
import re
import time
import uuid

from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeChannel, CommunityBridgePlatform, SlackDmMirrorGrant
from integrations.services.slack_chat_catalog import (
    catalog_conversations,
    conversation_activity_at,
    ready_for_display,
)
from integrations.services.slack_chat_read_state import ReadTarget, _cache_key
from integrations.services.slack_dm_mirror import (
    _call_slack_with_grant_authority,
    _capture_slack_grant_api_authority,
    _grant_history_days,
    _is_external_shared_conversation,
    _lock_slack_grant_api_authority,
    _slack_conversation_activity_seconds,
    SlackDmMirrorRateLimited,
)
from integrations.services.slack_owner_inventory import (
    KINDS,
    READ_FRESH_SECONDS,
    device_epoch,
    enabled,
    has_metadata_consent,
    _kind,
    state_for,
)
from integrations.services.message_sync.scheduler import BudgetDeferred


CURSOR_SALT = "slack-owner-inventory-page-v1"
METADATA_FRESH_SECONDS = 600


class InventoryError(Exception):
    """Stable owner-inventory error code and HTTP status."""

    def __init__(self, code: str, status_code: int, *, retry_after_seconds: int = 0):
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


def _authorized(user, public_key):
    if not enabled():
        raise InventoryError("slack_inventory_unavailable", 404)
    key = str(public_key or "").strip().lower()
    device = CommunityChatDevice.objects.filter(
        user=user, public_key=key, status="verified", revoked_at__isnull=True,
    ).first()
    if device is None:
        raise InventoryError("device_unverified", 403)
    grant = SlackDmMirrorGrant.objects.select_related("connection").filter(
        user=user, status="active", revoked_at__isnull=True,
    ).order_by("-updated_at").first()
    if grant is None:
        if SlackDmMirrorGrant.objects.filter(user=user, status="paused", revoked_at__isnull=True).exists():
            raise InventoryError("slack_grant_paused", 403)
        raise InventoryError("slack_inventory_unavailable", 404)
    authority = _capture_slack_grant_api_authority(grant, refresh_token=False)
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if not has_metadata_consent(connection, authority):
            raise InventoryError("inventory_consent_required", 403)
        state = state_for(connection, authority)
    return grant, authority, device, state


def _timestamp_iso(raw):
    try:
        value = Decimal(str(raw))
        if value.is_finite() and 0 < value <= Decimal(str(time.time() + 300)):
            return datetime.fromtimestamp(float(value), tz=dt_timezone.utc).isoformat()
    except (InvalidOperation, OverflowError, TypeError, ValueError, OSError):
        pass
    return None


def _valid_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _read_state(snapshot, now):
    observed = snapshot.get("fetched_at") if isinstance(snapshot, dict) else None
    if not snapshot or snapshot.get("available") is not True or type(snapshot.get("is_unread")) is not bool:
        return {
            "availability": "unknown", "is_unread": None, "unread_count": None,
            "has_personal_mention": None, "observed_at": None,
        }
    try:
        observed = float(observed)
        freshness = "available" if 0 <= now - observed <= READ_FRESH_SECONDS else "stale"
        observed_at = datetime.fromtimestamp(observed, tz=dt_timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        freshness, observed_at = "unknown", None
    if freshness == "unknown":
        return {
            "availability": "unknown", "is_unread": None, "unread_count": None,
            "has_personal_mention": None, "observed_at": None,
        }
    count = snapshot.get("unread_count")
    mention = snapshot.get("has_personal_mention")
    return {
        "availability": freshness,
        "is_unread": snapshot["is_unread"],
        "unread_count": count if type(count) is int and count >= 0 else None,
        "has_personal_mention": mention if type(mention) is bool else None,
        "observed_at": observed_at,
    }


def _coverage(state, now):
    result = {}
    for kind in KINDS:
        value = (state.get("coverage") or {}).get(kind, "pending")
        if value == "complete":
            observed = state.get("last_public_at" if kind == "public_channel" else "last_private_at")
            try:
                if observed is None or (timezone.now() - datetime.fromisoformat(observed)).total_seconds() > METADATA_FRESH_SECONDS:
                    value = "stale"
            except (TypeError, ValueError):
                value = "stale"
        result[kind] = value
    return result


def _cursor(cursor, *, binding):
    if not cursor:
        return None
    try:
        value = signing.loads(cursor, salt=CURSOR_SALT, max_age=900)
    except signing.BadSignature as exc:
        raise InventoryError("inventory_cursor_invalid", 403) from exc
    if not isinstance(value, dict) or value.get("binding") != binding:
        raise InventoryError("inventory_cursor_stale", 409)
    last = value.get("after")
    if not isinstance(last, list) or len(last) != 2 or last[0] not in KINDS or type(last[1]) is not int:
        raise InventoryError("inventory_cursor_invalid", 403)
    return tuple(last)


def _item(row, *, mirror, public_map, public_key, state, read_state, oldest):
    source_activity = _timestamp_iso(row.source_activity_ts)
    mirror_activity = conversation_activity_at(mirror) if mirror is not None else None
    activity = source_activity or mirror_activity
    name = row.source_name or row.display_name
    if row.kind == "im" and mirror is not None:
        profile = (mirror.participant_profiles or {}).get(row.counterpart_slack_user_id) or {}
        name = str(profile.get("display_name") or name or "")
    if row.kind == "mpim" and name.startswith("mpdm-"):
        participants = re.sub(r"-\d+$", "", name[5:]).split("--")
        if participants and all(participants):
            name = ", ".join(part.replace("-", " ").title() for part in participants)
    name = name or row.counterpart_slack_user_id or row.slack_conversation_id
    channel_id = None
    if row.kind == "public_channel":
        channel_id = public_map.get(row.slack_conversation_id)
    elif mirror is not None and public_key in (mirror.participant_buzz_pubkeys or []) and mirror.mlai_channel_id:
        channel_id = str(mirror.mlai_channel_id)
    if row.eligibility != "eligible":
        status = "unsupported"
        channel_id = None
    elif row.kind == "public_channel":
        status = "mapped" if channel_id else "unmapped"
    elif mirror is not None and mirror.status == "error":
        status = "error"
    elif mirror is not None and channel_id and ready_for_display(mirror, public_key=public_key):
        status = "ready"
    elif activity and oldest and datetime.fromisoformat(activity).timestamp() < oldest:
        status = "out_of_window"
    elif row.source_archived is True and oldest:
        status = "out_of_window"
    elif mirror is not None:
        status = "importing"
    else:
        status = "source_only"
    return {
        "slack_conversation_id": row.slack_conversation_id,
        "kind": row.kind,
        "name": name[:255],
        "last_message_at": activity,
        "metadata_observed_at": row.last_seen_at.isoformat(),
        "source_archived": row.source_archived,
        "source_is_open": row.source_is_open,
        "eligibility": row.eligibility,
        "state": status,
        "mlai_channel_id": channel_id,
        "read_state": read_state,
    }


def conversation_page(user, *, public_key, limit=50, cursor="", unread_only=False):
    """Return one signed, owner-scoped source page without calling Slack."""
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise InventoryError("inventory_limit_invalid", 400) from exc
    if not 1 <= limit <= 100:
        raise InventoryError("inventory_limit_invalid", 400)
    if unread_only not in (False, True):
        raise InventoryError("inventory_filter_invalid", 400)
    grant, authority, device, state = _authorized(user, public_key)
    epoch = device_epoch(grant, authority, device, state=state)
    revision = int(state.get("revision") or 0)
    read_cursor = (grant.connection.sync_cursor or {}).get("message_sync_read_snapshot_v1") or {}
    read_revision = int(read_cursor.get("content_revision", read_cursor.get("revision")) or 0)
    binding = [grant.pk, str(device.pk), epoch, revision, read_revision if unread_only else None, unread_only]
    after = _cursor(cursor, binding=binding)
    rows = list(grant.owner_conversation_inventory.order_by("kind", "id"))
    targets = [ReadTarget(row.slack_conversation_id, row.slack_conversation_id, row.kind) for row in rows]
    keys = [_cache_key(authority, target) for target in targets]
    snapshots = cache.get_many(keys)
    now = time.time()
    states = {
        row.pk: _read_state(snapshots.get(key), now)
        for row, key in zip(rows, keys)
    }
    coverage = _coverage(state, now)
    read_summary = {
        "eligible_count": 0, "fresh_count": 0, "stale_count": 0,
        "unknown_count": 0, "fresh_unread_count": 0,
        "provisional_unread_count": 0,
    }
    for row in rows:
        if row.eligibility != "eligible" or row.source_archived is True:
            continue
        read_summary["eligible_count"] += 1
        snapshot = states[row.pk]
        availability = snapshot["availability"]
        read_summary[{"available": "fresh_count", "stale": "stale_count", "unknown": "unknown_count"}[availability]] += 1
        if snapshot["is_unread"]:
            read_summary["fresh_unread_count" if availability == "available" else "provisional_unread_count"] += 1
    discovery_complete = all(value == "complete" for value in coverage.values())
    discovery_terminal = all(value != "pending" for value in coverage.values())
    read_summary.update(
        complete=discovery_complete and read_summary["unknown_count"] == 0 and read_summary["stale_count"] == 0,
        observed_at=timezone.now().isoformat(),
        inventory_revision=revision,
        read_revision=read_revision,
    )
    visible = [row for row in rows if (row.kind, row.pk) > after] if after else rows
    if unread_only:
        visible = [
            row for row in visible
            if row.eligibility == "eligible" and row.source_archived is not True
            and states[row.pk]["is_unread"] is True
        ]
    selected = visible[:limit]
    next_cursor = None
    if len(visible) > len(selected) and selected:
        next_cursor = signing.dumps(
            {"binding": binding, "after": [selected[-1].kind, selected[-1].pk]},
            salt=CURSOR_SALT,
        )
    selected_ids = [row.slack_conversation_id for row in selected]
    mirrors = {
        mirror.slack_conversation_id: mirror
        for mirror in catalog_conversations(grant.conversations.filter(
            slack_conversation_id__in=selected_ids,
        ))
    }
    for mirror in mirrors.values():
        mirror.grant = grant
    public_map = {
        bridge.slack_channel_id: _valid_uuid(bridge.destination_channel_id)
        for bridge in CommunityBridgeChannel.objects.filter(
            slack_workspace_id=authority.workspace_id,
            slack_channel_id__in=selected_ids,
            destination_platform=CommunityBridgePlatform.BUZZ,
            enabled=True,
        ) if _valid_uuid(bridge.destination_channel_id)
    }
    days = _grant_history_days(grant)
    oldest = time.time() - days * 86400 if days else None
    items = [
        _item(row, mirror=mirrors.get(row.slack_conversation_id), public_map=public_map,
              public_key=device.public_key, state=state, read_state=states[row.pk], oldest=oldest)
        for row in selected
    ]
    with transaction.atomic():
        _, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if not CommunityChatDevice.objects.filter(
            pk=device.pk, user=user, public_key=device.public_key,
            status="verified", revoked_at__isnull=True,
        ).exists():
            raise InventoryError("device_unverified", 403)
        if not has_metadata_consent(connection, authority):
            raise InventoryError("inventory_consent_required", 403)
        if state_for(connection, authority).get("revision") != revision:
            raise InventoryError("inventory_cursor_stale", 409)
        current_read = (connection.sync_cursor or {}).get("message_sync_read_snapshot_v1") or {}
        if unread_only and int(current_read.get(
            "content_revision", current_read.get("revision"),
        ) or 0) != read_revision:
            raise InventoryError("inventory_cursor_stale", 409)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "total": len(rows),
        "eligible_total": sum(row.eligibility == "eligible" for row in rows),
        "inventory_epoch": epoch,
        "inventory_revision": revision,
        "discovery_complete": discovery_complete,
        "discovery_terminal": discovery_terminal,
        "last_sweep_at": state.get("last_full_sweep_at"),
        "coverage": coverage,
        "read_state_coverage": read_summary,
    }


def request_open(user, *, public_key, slack_conversation_id):
    """Prioritize existing consented discovery for one verified owner row."""
    grant, authority, device, _ = _authorized(user, public_key)
    source_id = str(slack_conversation_id or "").strip()
    row = grant.owner_conversation_inventory.filter(slack_conversation_id=source_id).first()
    if row is None:
        raise InventoryError("inventory_conversation_unavailable", 404)
    if row.eligibility != "eligible":
        raise InventoryError(row.eligibility, 409)
    if row.kind == "public_channel":
        bridge = CommunityBridgeChannel.objects.filter(
            slack_workspace_id=authority.workspace_id,
            slack_channel_id=source_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            enabled=True,
        ).first()
        channel_id = _valid_uuid(bridge.destination_channel_id) if bridge is not None else None
        if channel_id is None:
            raise InventoryError("public_mapping_required", 409)
        return 200, {"state": "ready", "mlai_channel_id": channel_id}
    days = _grant_history_days(grant)
    if days and row.source_archived is True:
        raise InventoryError("inventory_history_consent_required", 409)
    scope = ReadTarget(source_id, source_id, row.kind).read_scope
    try:
        response = _call_slack_with_grant_authority(
            authority, "conversations_info", required_scopes={scope}, channel=source_id,
        )
    except SlackApiError as exc:
        code = str(exc.response.get("error") or "")
        if code in {"channel_not_found", "not_in_channel"}:
            raise InventoryError("inventory_conversation_unavailable", 404) from exc
        if code in {"ratelimited"}:
            raise InventoryError("inventory_rate_limited", 429, retry_after_seconds=5) from exc
        if code in {"missing_scope", "invalid_auth", "token_revoked"}:
            raise InventoryError("slack_authority_changed", 403) from exc
        raise InventoryError("slack_upstream_unavailable", 502) from exc
    except (BudgetDeferred, SlackDmMirrorRateLimited) as exc:
        raise InventoryError(
            "inventory_rate_limited", 429,
            retry_after_seconds=max(1, int(getattr(exc, "retry_after", 5) or 5)),
        ) from exc
    details = response.get("channel") if hasattr(response, "get") else None
    if (
        not isinstance(details, dict)
        or details.get("id") != source_id
        or _kind(details) != row.kind
        or _is_external_shared_conversation(details)
        or details.get("is_member") is False
        or (row.kind in {"mpim", "private_channel"} and details.get("is_member") is not True)
    ):
        raise InventoryError("inventory_source_changed", 409)
    if details.get("is_open") is False:
        raise InventoryError("inventory_closed_in_slack", 409)
    activity = _slack_conversation_activity_seconds(details)
    if activity is None:
        activity = _slack_conversation_activity_seconds({"latest": row.source_activity_ts})
    if activity is None:
        history_scope = {
            "im": "im:history", "mpim": "mpim:history",
            "private_channel": "groups:history",
        }[row.kind]
        try:
            history = _call_slack_with_grant_authority(
                authority, "conversations_history",
                required_scopes={history_scope}, channel=source_id,
                **({"oldest": str(int(time.time() - days * 86400))} if days else {}),
                inclusive=False, limit=1,
            )
        except (BudgetDeferred, SlackDmMirrorRateLimited) as exc:
            raise InventoryError(
                "inventory_rate_limited", 429,
                retry_after_seconds=max(1, int(getattr(exc, "retry_after", 5) or 5)),
            ) from exc
        except SlackApiError as exc:
            code = str(exc.response.get("error") or "")
            if code == "ratelimited":
                raise InventoryError("inventory_rate_limited", 429, retry_after_seconds=5) from exc
            if code in {"missing_scope", "invalid_auth", "token_revoked"}:
                raise InventoryError("slack_authority_changed", 403) from exc
            raise InventoryError("slack_upstream_unavailable", 502) from exc
        messages = history.get("messages") if hasattr(history, "get") else None
        if not isinstance(messages, list):
            raise InventoryError("slack_upstream_unavailable", 502)
        if not messages:
            raise InventoryError(
                "inventory_no_in_window_activity" if days else "inventory_no_messages", 409,
            )
        activity = _slack_conversation_activity_seconds({"latest": messages[0]})
        if activity is None:
            raise InventoryError("slack_upstream_unavailable", 502)
    if days and activity < time.time() - days * 86400:
        raise InventoryError("inventory_history_consent_required", 409)
    with transaction.atomic():
        locked_grant, connection = _lock_slack_grant_api_authority(
            authority, required_scopes={"im:read"},
        )
        if not CommunityChatDevice.objects.filter(
            pk=device.pk, user=user, public_key=device.public_key,
            status="verified", revoked_at__isnull=True,
        ).exists():
            raise InventoryError("device_unverified", 403)
        if not has_metadata_consent(connection, authority):
            raise InventoryError("inventory_consent_required", 403)
        if not locked_grant.owner_conversation_inventory.filter(
            pk=row.pk, eligibility="eligible",
        ).exists():
            raise InventoryError("inventory_conversation_unavailable", 404)
        mirror = catalog_conversations(locked_grant.conversations.filter(
            slack_conversation_id=source_id,
        )).first()
        if mirror is not None:
            mirror.grant = locked_grant
            if (
                device.public_key in (mirror.participant_buzz_pubkeys or [])
                and mirror.mlai_channel_id
                and ready_for_display(mirror, public_key=device.public_key)
            ):
                return 200, {"state": "ready", "mlai_channel_id": str(mirror.mlai_channel_id)}
        # The importer owns membership, source window and relay provisioning.
        # This hint changes no consent, history range or shared bridge mapping.
        locked_grant.last_discovery_at = None
        locked_grant.save(update_fields=("last_discovery_at", "updated_at"))
    return 202, {"state": "importing", "mlai_channel_id": None, "retry_after_seconds": 10}
