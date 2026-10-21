"""Inactive foundations for member-authorized Slack channel rooms.

These helpers keep queue, observed Slack metadata and adapter room state. They
never fetch Slack with a bot token, join a channel, discover channels or schedule
a worker. Phase 4 supplies member authority, routing and readiness gates.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeChannelAudience,
    CommunityBridgeChannelRefresh,
    CommunityBridgeMirrorDecision,
    CommunityBridgePlatform,
)

from .buzz import BuzzBridgeClient, BuzzBridgeError


logger = logging.getLogger(__name__)
REFRESH_BATCH = 5
REFRESH_LEASE_SECONDS = 120
REFRESH_MAX_ATTEMPTS = 8
_CHANNEL_ID_RE = re.compile(r"^C[A-Z0-9]{2,23}$")
_WORKSPACE_ID_RE = re.compile(r"^T[A-Z0-9]{2,23}$")
_AUDIENCES = frozenset(CommunityBridgeChannelAudience.values)


def valid_slack_channel_identity(workspace_id: str, channel_id: str) -> bool:
    """Whether an observation names a supported Slack workspace/public channel."""
    return bool(_WORKSPACE_ID_RE.fullmatch(workspace_id) and _CHANNEL_ID_RE.fullmatch(channel_id))


@dataclass(frozen=True)
class SlackChannelState:
    """The fields of a Slack conversation object that decide its room."""

    channel_id: str
    exists: bool = True
    name: str = ""
    is_private: bool = False
    is_archived: bool = False
    is_member: bool = False
    is_ext_shared: bool = False
    topic: str = ""
    purpose: str = ""
    created: int = 0

    @classmethod
    def from_slack(cls, channel: dict) -> "SlackChannelState":
        def text(value):
            return str((value or {}).get("value") or "") if isinstance(value, dict) else ""

        return cls(
            channel_id=str(channel.get("id") or "").strip(),
            name=str(channel.get("name") or "").strip(),
            is_private=bool(
                channel.get("is_private") or channel.get("is_group")
                or channel.get("is_mpim") or channel.get("is_im")
            ),
            is_archived=bool(channel.get("is_archived")),
            is_member=bool(channel.get("is_member")),
            is_ext_shared=bool(channel.get("is_ext_shared") or channel.get("is_pending_ext_shared")),
            topic=text(channel.get("topic")),
            purpose=text(channel.get("purpose")),
            created=int(channel.get("created") or 0),
        )


def queue_refresh(workspace_id: str, channel_id: str, *, reason: str, delay_seconds: int = 0) -> None:
    """Coalesce one channel refresh without starting a Slack reader."""
    if not valid_slack_channel_identity(workspace_id, channel_id):
        raise ValueError("invalid_slack_channel_identity")
    CommunityBridgeChannelRefresh.objects.update_or_create(
        slack_channel_id=channel_id,
        defaults={
            "slack_workspace_id": workspace_id,
            "reason": reason[:40],
            "available_at": timezone.now() + timedelta(seconds=delay_seconds),
            "attempts": 0,
            "last_error_code": "",
        },
    )


def rekey_channel(old_id: str, new_id: str) -> bool:
    """Rekey an unmapped source; mapped room identities require explicit migration."""
    from integrations.message_sync_models import BridgeSyncState

    if not _CHANNEL_ID_RE.fullmatch(old_id) or not _CHANNEL_ID_RE.fullmatch(new_id):
        raise ValueError("invalid_slack_channel_identity")
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().filter(slack_channel_id=old_id).first()
        if row is None:
            return False
        if old_id == new_id:
            return True
        if row.destination_channel_id:
            row.room_sync_error = "rekey_requires_room_migration"
            row.save(update_fields=["room_sync_error", "updated_at"])
            if _WORKSPACE_ID_RE.fullmatch(row.slack_workspace_id):
                queue_refresh(row.slack_workspace_id, old_id, reason="rekey_requires_room_migration")
            return False
        if CommunityBridgeChannel.objects.filter(slack_channel_id=new_id).exclude(pk=row.pk).exists():
            logger.warning(
                "community_bridge_channel_rekey_conflict old_channel=%s new_channel=%s", old_id, new_id,
            )
            return False
        row.slack_channel_id = new_id
        row.room_sync_pending = bool(row.destination_channel_id) or row.room_sync_pending
        row.save()
        BridgeSyncState.objects.filter(public_channel=row).update(source_channel_id=new_id)
        CommunityBridgeChannelRefresh.objects.filter(slack_channel_id=old_id).delete()
        queue_refresh(row.slack_workspace_id, new_id, reason="channel_id_changed")
    logger.info("community_bridge_channel_rekeyed old_channel=%s new_channel=%s", old_id, new_id)
    return True


def _observe(row: CommunityBridgeChannel, state: SlackChannelState, now) -> None:
    if row.slack_channel_id != state.channel_id:
        raise ValueError("slack_channel_identity_mismatch")
    row.slack_seen_at = now
    if not state.exists:
        row.slack_deleted = True
        row.enabled = False
        return
    row.slack_deleted = False
    row.slack_channel_name = state.name or row.slack_channel_name
    row.slack_visibility = "private" if state.is_private else "public"
    row.slack_archived = state.is_archived
    if state.is_archived:
        row.enabled = False
    row.slack_topic = state.topic
    row.slack_purpose = state.purpose
    if state.created and row.slack_created_at is None:
        row.slack_created_at = datetime.fromtimestamp(state.created, tz=dt_timezone.utc)
    if state.is_private:
        # Fail closed: private content never reaches the community room.
        row.enabled = False
        if row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR:
            row.mirror_decision = CommunityBridgeMirrorDecision.HOLD
            row.hold_reason = "converted_to_private"
    elif state.is_ext_shared:
        row.enabled = False
        if row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR:
            row.mirror_decision = CommunityBridgeMirrorDecision.HOLD
            row.hold_reason = "slack_connect"


def _text_managed(row: CommunityBridgeChannel) -> bool:
    """Whether the room's description and topic follow Slack.

    Rooms created by channel discovery always follow Slack. Operator-provisioned
    rooms keep their MLAI Chat text until Slack's purpose or topic changes
    after they were adopted, so retaining helpers never rewrites them all.
    """
    if row.lifecycle_source or row.room_state.get("text_managed"):
        return True
    baseline = row.room_state.get("text_baseline")
    return baseline is not None and baseline != [row.slack_purpose, row.slack_topic]


def _desired_room(row: CommunityBridgeChannel) -> dict | None:
    """Room state the adapter should hold, or ``None`` when there is no room."""
    if row.audience not in _AUDIENCES:
        raise ValueError("invalid_bridge_room_audience")
    archived = row.slack_archived or row.slack_deleted or row.slack_visibility != "public"
    if not row.destination_channel_id and (
        row.mirror_decision != CommunityBridgeMirrorDecision.MIRROR or archived
    ):
        return None
    managed = _text_managed(row)
    return {
        "audience": row.audience,
        "name": row.slack_channel_name,
        "archived": archived,
        "about": row.slack_purpose if managed else None,
        "topic": row.slack_topic if managed else None,
    }


def _room_out_of_date(row: CommunityBridgeChannel) -> bool:
    desired = _desired_room(row)
    if desired is None:
        return False
    if not row.destination_channel_id:
        return True
    return row.room_state.get("applied") != desired


def _record_sync_failure(pk: int, code: str, *, request_state: dict, **changes) -> bool:
    return bool(CommunityBridgeChannel.objects.filter(pk=pk, **request_state).exclude(
        room_sync_error="rekey_requires_room_migration",
    ).update(room_sync_error=code[:100], **changes))


def sync_room(pk: int) -> str:
    """Apply a row's desired room state through the MLAI Chat adapter.

    Adapter calls run outside any transaction. This never joins Slack or opens
    the delivery gate; Phase 4 owns reader, membership and readiness decisions.
    The result is written back only if the immutable request identity is current.
    """
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=pk)
        if row.room_sync_error == "rekey_requires_room_migration":
            return "held"
        if not row.room_sync_pending:
            return "unchanged"
        if row.destination_platform != CommunityBridgePlatform.BUZZ:
            row.room_sync_error = "unsupported_destination"
            row.save(update_fields=["room_sync_error", "updated_at"])
            return "failed"
        if not row.lifecycle_source and "text_baseline" not in row.room_state:
            row.room_state = {**row.room_state, "text_baseline": [row.slack_purpose, row.slack_topic]}
            row.save(update_fields=["room_state", "updated_at"])
        desired = _desired_room(row)
        if desired is None:
            row.room_sync_pending = False
            row.room_sync_error = ""
            row.save(update_fields=["room_sync_pending", "room_sync_error", "updated_at"])
            return "unchanged"
        managed = _text_managed(row)
        slack_channel_id = row.slack_channel_id
        slack_workspace_id = row.slack_workspace_id
        destination = row.destination_channel_id
        audience = row.audience
        mirror = row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR
        previous_audience = (row.room_state.get("applied") or {}).get("audience")
        if destination and previous_audience and previous_audience != audience:
            row.room_sync_error = "immutable_room_audience"
            row.save(update_fields=["room_sync_error", "updated_at"])
            return "failed"
        request_state = {
            "slack_workspace_id": slack_workspace_id,
            "slack_channel_id": slack_channel_id,
            "audience": audience,
            "destination_channel_id": destination,
            "destination_platform": row.destination_platform,
            "updated_at": row.updated_at,
            "mirror_decision": row.mirror_decision,
            "hold_reason": row.hold_reason,
            "enabled": row.enabled,
            "room_sync_pending": row.room_sync_pending,
            "room_sync_error": row.room_sync_error,
            "room_state": row.room_state,
            "lifecycle_source": row.lifecycle_source,
            "slack_channel_name": row.slack_channel_name,
            "slack_archived": row.slack_archived,
            "slack_deleted": row.slack_deleted,
            "slack_visibility": row.slack_visibility,
            "slack_topic": row.slack_topic,
            "slack_purpose": row.slack_purpose,
        }
    try:
        result = BuzzBridgeClient.upsert_public_channel(
            slack_workspace_id=slack_workspace_id,
            slack_channel_id=slack_channel_id,
            audience=audience,
            name=desired["name"],
            archived=desired["archived"],
            about=desired["about"],
            topic=desired["topic"],
            channel_id=destination,
        )
    except BuzzBridgeError as exc:
        code = exc.adapter_error or type(exc).__name__
        logger.warning(
            "community_bridge_channel_room_sync_failed channel=%s error=%s permanent=%s",
            slack_channel_id, code, exc.permanent,
        )
        if exc.adapter_error == "name_conflict":
            # A native room already uses this name. Hold for a human decision
            # rather than silently creating a second room with the same name.
            changes = {"room_sync_pending": False}
            if mirror and not destination:
                changes.update(
                    mirror_decision=CommunityBridgeMirrorDecision.HOLD, hold_reason="name_conflict",
                )
            return "held" if _record_sync_failure(pk, "name_conflict", request_state=request_state, **changes) else "retry"
        if not _record_sync_failure(pk, code, request_state=request_state):
            return "retry"
        return "failed" if exc.permanent else "retry"

    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=pk)
        if row.room_sync_error == "rekey_requires_room_migration":
            return "held"
        if (
            row.slack_channel_id != slack_channel_id
            or row.slack_workspace_id != slack_workspace_id
            or row.audience != audience
            or row.destination_channel_id not in {"", destination}
        ):
            return "retry"
        if not row.destination_channel_id:
            if CommunityBridgeChannel.objects.filter(
                destination_platform=CommunityBridgePlatform.BUZZ,
                destination_channel_id=result["channel_id"],
            ).exclude(pk=pk).exists():
                row.room_sync_error = "destination_in_use"
                row.save(update_fields=["room_sync_error", "updated_at"])
                return "failed"
            row.destination_channel_id = result["channel_id"]
        row.destination_platform = CommunityBridgePlatform.BUZZ
        row.destination_channel_name = desired["name"]
        row.room_state = {
            **row.room_state,
            "applied": desired,
            "text_managed": bool(row.room_state.get("text_managed") or managed),
        }
        row.room_sync_error = ""
        row.room_synced_at = timezone.now()
        if desired["archived"] or row.mirror_decision != CommunityBridgeMirrorDecision.MIRROR:
            row.enabled = False
        # Slack may have changed again while the adapter call was in flight.
        row.room_sync_pending = _room_out_of_date(row)
        row.save()
    if result["changed"]:
        logger.info(
            "community_bridge_channel_room_synced channel=%s room=%s changed=%s",
            slack_channel_id, result["channel_id"], ",".join(result["changed"]),
        )
    return "synced"


def _retry_refresh(refresh, *, seconds: int, code: str, count_attempt: bool = True) -> None:
    """Retry only the claimed observation; never overwrite a newer queued event."""
    current = CommunityBridgeChannelRefresh.objects.filter(
        pk=refresh.pk, updated_at=refresh.updated_at, available_at=refresh.available_at,
    )
    attempts = refresh.attempts + (1 if count_attempt else 0)
    if count_attempt and attempts >= REFRESH_MAX_ATTEMPTS:
        deleted, _ = current.delete()
        if deleted:
            logger.warning(
                "community_bridge_channel_refresh_abandoned channel=%s error=%s",
                refresh.slack_channel_id, code,
            )
        return
    current.update(
        attempts=attempts,
        available_at=timezone.now() + timedelta(seconds=seconds),
        last_error_code=code[:100],
    )


def claim_refreshes(limit: int = REFRESH_BATCH) -> list[CommunityBridgeChannelRefresh]:
    """Lease due queue rows for a future authorized reader, without doing I/O."""
    now = timezone.now()
    with transaction.atomic():
        due = list(
            CommunityBridgeChannelRefresh.objects.select_for_update(skip_locked=True)
            .filter(available_at__lte=now)
            .order_by("available_at", "id")[:limit]
        )
        expires = now + timedelta(seconds=REFRESH_LEASE_SECONDS)
        CommunityBridgeChannelRefresh.objects.filter(pk__in=[item.pk for item in due]).update(
            available_at=expires, updated_at=now,
        )
        for item in due:
            item.available_at = expires
            item.updated_at = now
    return due


def complete_refresh(refresh) -> bool:
    """Complete a leased observation while preserving any newer queued event."""
    deleted, _ = CommunityBridgeChannelRefresh.objects.filter(
        pk=refresh.pk, updated_at=refresh.updated_at, available_at=refresh.available_at,
    ).delete()
    return bool(deleted)
