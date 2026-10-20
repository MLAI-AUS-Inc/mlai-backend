"""Mirror public Slack channel lifecycle into MLAI Chat rooms.

A Slack channel event only says that a channel changed. Each one queues a
refresh; the bridge worker re-reads the channel with ``conversations.info`` and
applies that authoritative state, so duplicate or out-of-order events are
harmless. A ``conversations.list`` pass every ten minutes catches missed events.

``CommunityBridgeChannel`` rows hold the decision for every channel: mirror,
hold or exclude. Public channels created after lifecycle mirroring was first
switched on mirror automatically; older unlisted channels arrive as hold rows
for an admin to decide. Private channels are never created or mirrored here,
and a channel converted to private stops mirroring at once.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeChannelRefresh,
    CommunityBridgeChannelSource,
    CommunityBridgeLifecycleState,
    CommunityBridgeMirrorDecision,
    CommunityBridgePlatform,
)
from integrations.services.message_sync.scheduler import BudgetDeferred

from .buzz import BuzzBridgeClient, BuzzBridgeError
from .slack import SlackBridgeClient


logger = logging.getLogger(__name__)

LIFECYCLE_EVENTS = frozenset({
    "channel_created",
    "channel_rename",
    "channel_archive",
    "channel_unarchive",
    "channel_deleted",
    "channel_id_changed",
})
RECONCILE_SECONDS = 600
RECONCILE_PAGE_SIZE = 200
REFRESH_BATCH = 5
REFRESH_LEASE_SECONDS = 120
REFRESH_RETRY_SECONDS = (10, 30, 120, 300, 600)
REFRESH_MAX_ATTEMPTS = 8
RECONCILE_LEASE_SECONDS = 120
RECONCILE_FAILURE_BACKOFF_SECONDS = 60

_CHANNEL_ID_RE = re.compile(r"^C[A-Z0-9]{2,23}$")
_WORKSPACE_ID_RE = re.compile(r"^T[A-Z0-9]{2,23}$")


def enabled() -> bool:
    return bool(getattr(settings, "COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED", False))


def _bot_workspace_id() -> str:
    return str(getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "") or "").strip()


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


# ---------------------------------------------------------------------------
# Event intake
# ---------------------------------------------------------------------------


def record_lifecycle_event(payload) -> dict | None:
    """Queue a refresh for a Slack channel lifecycle event.

    Returns ``None`` for every other event so callers fall through to message
    ingestion. Lifecycle events are acknowledged and ignored while the flag is
    off; they never reach message ingestion, whose receipts expect a channel ID
    rather than a channel object.
    """
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict) or event.get("type") not in LIFECYCLE_EVENTS:
        return None
    if not enabled():
        return {"status": "ignored", "reason": "channel_lifecycle_disabled"}
    workspace_id = str(payload.get("team_id") or "").strip()
    app_id = str(payload.get("api_app_id") or "").strip()
    expected_app = str(getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "") or "").strip()
    if (
        not _WORKSPACE_ID_RE.fullmatch(workspace_id)
        or workspace_id != _bot_workspace_id()
        or (expected_app and app_id != expected_app)
    ):
        return {"status": "ignored", "reason": "channel_lifecycle_foreign_source"}
    event_type = event["type"]
    if event_type == "channel_id_changed":
        old_id = str(event.get("old_channel_id") or "").strip()
        channel_id = str(event.get("new_channel_id") or "").strip()
        if not _CHANNEL_ID_RE.fullmatch(old_id) or not _CHANNEL_ID_RE.fullmatch(channel_id):
            return {"status": "ignored", "reason": "channel_lifecycle_invalid_channel"}
        rekey_channel(old_id, channel_id)
    else:
        channel = event.get("channel")
        channel_id = str((channel.get("id") if isinstance(channel, dict) else channel) or "").strip()
        if not _CHANNEL_ID_RE.fullmatch(channel_id):
            return {"status": "ignored", "reason": "channel_lifecycle_invalid_channel"}
    lifecycle_state(workspace_id)
    queue_refresh(workspace_id, channel_id, reason=event_type)
    return {"status": "accepted", "reason": event_type}


def queue_refresh(workspace_id: str, channel_id: str, *, reason: str, delay_seconds: int = 0) -> None:
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
    """Follow a Slack channel ID change without forking its room or history."""
    from integrations.message_sync_models import BridgeSyncState

    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().filter(slack_channel_id=old_id).first()
        if row is None:
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
    logger.info("community_bridge_channel_rekeyed old_channel=%s new_channel=%s", old_id, new_id)
    return True


def lifecycle_state(workspace_id: str) -> CommunityBridgeLifecycleState:
    """The workspace's auto-mirror boundary, fixed when lifecycle first runs."""
    state, _ = CommunityBridgeLifecycleState.objects.get_or_create(
        slack_workspace_id=workspace_id, defaults={"auto_mirror_since": timezone.now()},
    )
    return state


# ---------------------------------------------------------------------------
# Applying Slack state
# ---------------------------------------------------------------------------


def fetch_channel_state(channel_id: str) -> SlackChannelState:
    """Read one channel. A channel the bot can no longer see counts as deleted."""
    client = SlackBridgeClient.get_client()
    try:
        response = client.conversations_info(channel=channel_id)
    except SlackApiError as exc:
        if exc.response.get("error") == "channel_not_found":
            return SlackChannelState(channel_id=channel_id, exists=False)
        raise
    return SlackChannelState.from_slack(response.get("channel") or {})


def _new_channel_decision(state: SlackChannelState, auto_mirror_since) -> tuple[str, str]:
    if state.is_ext_shared:
        return CommunityBridgeMirrorDecision.HOLD, "shared_channel"
    created = datetime.fromtimestamp(state.created, tz=dt_timezone.utc) if state.created else None
    if created is not None and created >= auto_mirror_since:
        return CommunityBridgeMirrorDecision.MIRROR, ""
    return CommunityBridgeMirrorDecision.HOLD, "backlog"


def _destination_workspace_id() -> str:
    return (
        CommunityBridgeChannel.objects.filter(destination_platform=CommunityBridgePlatform.BUZZ)
        .exclude(destination_workspace_id="")
        .values_list("destination_workspace_id", flat=True)
        .first()
        or ""
    )


def _observe(row: CommunityBridgeChannel, state: SlackChannelState, now) -> None:
    row.slack_seen_at = now
    if not state.exists:
        row.slack_deleted = True
        row.enabled = False
        return
    row.slack_deleted = False
    row.slack_channel_name = state.name or row.slack_channel_name
    row.slack_visibility = "private" if state.is_private else "public"
    row.slack_archived = state.is_archived
    row.slack_bot_member = state.is_member
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


def _text_managed(row: CommunityBridgeChannel) -> bool:
    """Whether the room's description and topic follow Slack.

    Rooms created through lifecycle always follow Slack. Operator-provisioned
    rooms keep their MLAI Chat text until Slack's purpose or topic changes
    after they were adopted, so enabling lifecycle never rewrites them all.
    """
    if row.lifecycle_source or row.room_state.get("text_managed"):
        return True
    baseline = row.room_state.get("text_baseline")
    return baseline is not None and baseline != [row.slack_purpose, row.slack_topic]


def _desired_room(row: CommunityBridgeChannel) -> dict | None:
    """Room state the adapter should hold, or ``None`` when there is no room."""
    archived = row.slack_archived or row.slack_deleted or row.slack_visibility != "public"
    if not row.destination_channel_id and (
        row.mirror_decision != CommunityBridgeMirrorDecision.MIRROR or archived
    ):
        return None
    managed = _text_managed(row)
    return {
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
    if (
        row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR
        and not desired["archived"]
        and not row.enabled
    ):
        return True
    return row.room_state.get("applied") != desired


def apply_channel_state(state: SlackChannelState, *, workspace_id: str, source: str) -> str:
    """Record one channel's Slack state and bring its room up to date."""
    now = timezone.now()
    with transaction.atomic():
        row = (
            CommunityBridgeChannel.objects.select_for_update()
            .filter(slack_channel_id=state.channel_id)
            .first()
        )
        if row is None:
            if not state.exists or state.is_private or state.is_archived or not state.name:
                return "skipped"
            decision, reason = _new_channel_decision(state, lifecycle_state(workspace_id).auto_mirror_since)
            try:
                with transaction.atomic():
                    row = CommunityBridgeChannel.objects.create(
                        slack_workspace_id=workspace_id,
                        slack_channel_id=state.channel_id,
                        slack_channel_name=state.name,
                        destination_platform=CommunityBridgePlatform.BUZZ,
                        destination_workspace_id=_destination_workspace_id(),
                        enabled=False,
                        mirror_decision=decision,
                        hold_reason=reason,
                        lifecycle_source=source,
                    )
            except IntegrityError:
                row = CommunityBridgeChannel.objects.select_for_update().get(slack_channel_id=state.channel_id)
            logger.info(
                "community_bridge_channel_discovered channel=%s decision=%s reason=%s",
                state.channel_id, decision, reason or "-",
            )
        _observe(row, state, now)
        row.room_sync_pending = row.room_sync_pending or _room_out_of_date(row)
        row.save()
        pending = row.room_sync_pending
    return sync_room(row.pk) if pending else "unchanged"


def _ensure_bot_member(row: CommunityBridgeChannel) -> bool:
    """Join the bot so Slack sends this channel's messages and history."""
    try:
        response = SlackBridgeClient.get_client().conversations_join(channel=row.slack_channel_id)
    except SlackApiError as exc:
        logger.warning(
            "community_bridge_channel_join_failed channel=%s error=%s",
            row.slack_channel_id, exc.response.get("error"),
        )
        return False
    return bool(response.get("ok"))


def _record_sync_failure(pk: int, code: str, **changes) -> None:
    CommunityBridgeChannel.objects.filter(pk=pk).update(room_sync_error=code[:100], **changes)


def sync_room(pk: int) -> str:
    """Apply a row's desired room state through the MLAI Chat adapter.

    Slack and adapter calls run outside any transaction. The result is written
    back only if the row still describes the same Slack channel and room.
    """
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=pk)
        if not row.room_sync_pending:
            return "unchanged"
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
        destination = row.destination_channel_id
        mirror = row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR
        bot_member = row.slack_bot_member

    if mirror and not desired["archived"] and not bot_member:
        if not _ensure_bot_member(row):
            _record_sync_failure(pk, "bot_not_in_channel")
            return "waiting_for_bot"
        bot_member = True
    try:
        result = BuzzBridgeClient.upsert_public_channel(
            slack_workspace_id=row.slack_workspace_id,
            slack_channel_id=slack_channel_id,
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
            _record_sync_failure(pk, "name_conflict", **changes)
            return "held"
        _record_sync_failure(pk, code)
        return "failed" if exc.permanent else "retry"

    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=pk)
        if row.slack_channel_id != slack_channel_id or row.destination_channel_id not in {"", destination}:
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
        row.slack_bot_member = row.slack_bot_member or bot_member
        row.room_state = {
            **row.room_state,
            "applied": desired,
            "text_managed": bool(row.room_state.get("text_managed") or managed),
        }
        row.room_sync_error = ""
        row.room_synced_at = timezone.now()
        if row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR:
            row.enabled = not desired["archived"]
        else:
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


# ---------------------------------------------------------------------------
# Worker turns
# ---------------------------------------------------------------------------


def _retry_refresh(refresh, *, seconds: int, code: str, count_attempt: bool = True) -> None:
    attempts = refresh.attempts + (1 if count_attempt else 0)
    if count_attempt and attempts >= REFRESH_MAX_ATTEMPTS:
        # The ten-minute reconciliation pass keeps trying the channel.
        CommunityBridgeChannelRefresh.objects.filter(pk=refresh.pk).delete()
        logger.warning(
            "community_bridge_channel_refresh_abandoned channel=%s error=%s",
            refresh.slack_channel_id, code,
        )
        return
    CommunityBridgeChannelRefresh.objects.filter(pk=refresh.pk).update(
        attempts=attempts,
        available_at=timezone.now() + timedelta(seconds=seconds),
        last_error_code=code[:100],
    )


def process_refreshes_once(limit: int = REFRESH_BATCH) -> int:
    """Re-read due channels and apply their state. Returns refreshes completed."""
    if not enabled():
        return 0
    now = timezone.now()
    with transaction.atomic():
        due = list(
            CommunityBridgeChannelRefresh.objects.select_for_update(skip_locked=True)
            .filter(available_at__lte=now)
            .order_by("available_at", "id")[:limit]
        )
        # A lease, not a lock: queue_refresh() still resets a claimed row so a
        # newer event is never lost while this turn works.
        CommunityBridgeChannelRefresh.objects.filter(pk__in=[item.pk for item in due]).update(
            available_at=now + timedelta(seconds=REFRESH_LEASE_SECONDS),
        )
    completed = 0
    for refresh in due:
        try:
            outcome = apply_channel_state(
                fetch_channel_state(refresh.slack_channel_id),
                workspace_id=refresh.slack_workspace_id,
                source=CommunityBridgeChannelSource.EVENT,
            )
        except BudgetDeferred as exc:
            _retry_refresh(refresh, seconds=exc.retry_after, code="budget_deferred", count_attempt=False)
            continue
        except Exception as exc:  # noqa: BLE001 - one channel must not stall the lane
            delay = REFRESH_RETRY_SECONDS[min(refresh.attempts, len(REFRESH_RETRY_SECONDS) - 1)]
            _retry_refresh(refresh, seconds=delay, code=type(exc).__name__)
            continue
        if outcome == "retry":
            delay = REFRESH_RETRY_SECONDS[min(refresh.attempts, len(REFRESH_RETRY_SECONDS) - 1)]
            _retry_refresh(refresh, seconds=delay, code="room_sync_retry")
            continue
        # A newer event re-queues the row (updating it); keep that one.
        CommunityBridgeChannelRefresh.objects.filter(pk=refresh.pk, updated_at=refresh.updated_at).delete()
        completed += 1
    return completed


def _lease_reconcile(workspace_id: str):
    now = timezone.now()
    with transaction.atomic():
        lifecycle_state(workspace_id)
        state = CommunityBridgeLifecycleState.objects.select_for_update().get(slack_workspace_id=workspace_id)
        if state.lease_expires_at and state.lease_expires_at > now:
            return None
        continuing = bool(state.reconcile_cursor)
        due = state.last_full_pass_at is None or state.last_full_pass_at <= now - timedelta(seconds=RECONCILE_SECONDS)
        if not continuing and not due:
            return None
        if not continuing:
            state.pass_started_at = now
        state.lease_expires_at = now + timedelta(seconds=RECONCILE_LEASE_SECONDS)
        state.save()
        return state


def _finish_reconcile_pass(state: CommunityBridgeLifecycleState) -> None:
    """Re-read mapped channels the listing no longer shows (deleted or private)."""
    started = state.pass_started_at or timezone.now()
    missing = (
        CommunityBridgeChannel.objects.filter(
            slack_workspace_id=state.slack_workspace_id,
            slack_visibility="public",
            slack_deleted=False,
        )
        .exclude(mirror_decision=CommunityBridgeMirrorDecision.EXCLUDE)
        .exclude(destination_channel_id="", mirror_decision=CommunityBridgeMirrorDecision.HOLD)
        .exclude(slack_seen_at__gte=started)
        .values_list("slack_channel_id", flat=True)
    )
    for channel_id in missing:
        if _CHANNEL_ID_RE.fullmatch(channel_id):
            queue_refresh(state.slack_workspace_id, channel_id, reason="reconcile_missing")
    for channel_id in CommunityBridgeChannel.objects.filter(
        slack_workspace_id=state.slack_workspace_id, room_sync_pending=True,
    ).values_list("slack_channel_id", flat=True):
        queue_refresh(state.slack_workspace_id, channel_id, reason="reconcile_pending")


def reconcile_once() -> str:
    """Advance the conversations.list pass by one page when it is due."""
    if not enabled():
        return "disabled"
    workspace_id = _bot_workspace_id()
    if (
        not _WORKSPACE_ID_RE.fullmatch(workspace_id)
        or not SlackBridgeClient.is_configured()
        or not BuzzBridgeClient.is_configured()
    ):
        return "unconfigured"
    state = _lease_reconcile(workspace_id)
    if state is None:
        return "idle"
    try:
        response = SlackBridgeClient.get_client().conversations_list(
            types="public_channel",
            exclude_archived=False,
            limit=RECONCILE_PAGE_SIZE,
            cursor=state.reconcile_cursor or None,
        )
    except BudgetDeferred as exc:
        CommunityBridgeLifecycleState.objects.filter(pk=state.pk).update(
            lease_expires_at=timezone.now() + timedelta(seconds=exc.retry_after),
            last_error_code="budget_deferred",
        )
        return "deferred"
    except Exception as exc:  # noqa: BLE001 - retried on a later turn
        CommunityBridgeLifecycleState.objects.filter(pk=state.pk).update(
            lease_expires_at=timezone.now() + timedelta(seconds=RECONCILE_FAILURE_BACKOFF_SECONDS),
            last_error_code=type(exc).__name__[:100],
        )
        logger.warning("community_bridge_channel_reconcile_failed error=%s", type(exc).__name__)
        return "failed"

    for channel in response.get("channels") or []:
        observed = SlackChannelState.from_slack(channel)
        if not _CHANNEL_ID_RE.fullmatch(observed.channel_id):
            continue
        try:
            apply_channel_state(observed, workspace_id=workspace_id, source=CommunityBridgeChannelSource.RECONCILE)
        except Exception as exc:  # noqa: BLE001 - BudgetDeferred included; retried via refresh
            queue_refresh(workspace_id, observed.channel_id, reason="reconcile_retry",
                          delay_seconds=getattr(exc, "retry_after", RECONCILE_FAILURE_BACKOFF_SECONDS))

    next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
    with transaction.atomic():
        state = CommunityBridgeLifecycleState.objects.select_for_update().get(pk=state.pk)
        state.lease_expires_at = None
        state.last_error_code = ""
        state.reconcile_cursor = next_cursor[:255]
        if not next_cursor:
            _finish_reconcile_pass(state)
            state.last_full_pass_at = timezone.now()
            state.pass_started_at = None
        state.save()
    return "page" if next_cursor else "complete"
