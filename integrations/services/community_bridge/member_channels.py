"""Member-authorized Slack metadata, room lifecycle and durable audience wakes.

Expansion obeys the feature switches and canary cohort. Existing-room authority
reductions remain active independently; no helper joins a Slack channel or
uses the bot token to inspect a member source.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeChannelAudience,
    CommunityBridgeChannelRefresh,
    CommunityBridgeMirrorDecision,
    CommunityBridgePlatform,
    SlackChannelMembership,
    SlackDmMirrorGrant,
)

from .buzz import BuzzBridgeClient, BuzzBridgeError


logger = logging.getLogger(__name__)
REFRESH_BATCH = 5
REFRESH_LEASE_SECONDS = 120
REFRESH_MAX_ATTEMPTS = 8
_CHANNEL_ID_RE = re.compile(r"^C[A-Z0-9]{2,23}$")
_WORKSPACE_ID_RE = re.compile(r"^T[A-Z0-9]{2,23}$")
_AUDIENCES = frozenset(CommunityBridgeChannelAudience.values)
_INFO_HINT = re.compile(r"i:([1-9a-f][0-9a-f]{0,15}):([1-9a-f][0-9a-f]{0,14})")
_EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)


@dataclass(frozen=True)
class InfoReaderHint:
    """A scheduling preference, never public privacy or membership evidence."""

    grant_id: int
    observed_at: datetime


def info_reader_hint_reason(grant_id, observed_at):
    """Encode a bounded server grant identity without storing a Slack subject."""
    if (type(grant_id) is not int or not 0 < grant_id <= 2**63 - 1
            or not isinstance(observed_at, datetime) or timezone.is_naive(observed_at)
            or not _EPOCH < observed_at <= timezone.now() + timedelta(minutes=5)):
        return None
    delta = observed_at.astimezone(dt_timezone.utc) - _EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return f"i:{grant_id:x}:{micros:x}"


def info_reader_hint(reason):
    """Strictly parse the existing queue field; malformed hints confer nothing."""
    if not isinstance(reason, str) or len(reason) > 40:
        return None
    match = _INFO_HINT.fullmatch(reason)
    if match is None:
        return None
    grant_id, micros = (int(value, 16) for value in match.groups())
    try:
        observed_at = _EPOCH + timedelta(microseconds=micros)
    except OverflowError:
        return None
    if info_reader_hint_reason(grant_id, observed_at) != reason:
        return None
    return InfoReaderHint(grant_id, observed_at)


def _refresh_reason(previous, incoming):
    """Coalesce scalar hints under the queue lock, without authority lookups."""
    old, new = info_reader_hint(previous), info_reader_hint(incoming)
    if old is not None and (new is None or old.observed_at >= new.observed_at):
        return previous
    # Never truncate an invalid encoded value into a different valid grant.
    if new is None and isinstance(incoming, str) and incoming.startswith("i:"):
        return "public_source_unverified"
    return incoming[:40]


def valid_slack_channel_identity(workspace_id: str, channel_id: str) -> bool:
    """Whether an observation names a supported Slack workspace/public channel."""
    return bool(isinstance(workspace_id, str) and isinstance(channel_id, str)
                and _WORKSPACE_ID_RE.fullmatch(workspace_id) and _CHANNEL_ID_RE.fullmatch(channel_id))


def member_channel_in_scope(workspace_id: str, channel_id: str) -> bool:
    """Check the configured workspace/canary cohort, never source privacy.

    The empty allowlist permits the normal enabled rollout. Malformed explicit
    cohorts deny expansion. Callers separately check the feature gate, current
    consent and authoritative public-conversation metadata. Existing-room
    authority reductions must not depend on this expansion-only predicate.
    """
    if (not valid_slack_channel_identity(workspace_id, channel_id)
            or workspace_id != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")):
        return False
    raw = getattr(settings, "SLACK_MEMBER_CHANNEL_ALLOWED_IDS", "")
    if raw == "":
        return True
    if not isinstance(raw, str) or len(raw) > 25000 or re.fullmatch(r"[A-Z0-9, \t]*", raw) is None:
        return False
    identifiers = [value.strip() for value in raw.split(",")]
    return bool(len(identifiers) <= 1000
                and all(_CHANNEL_ID_RE.fullmatch(value) for value in identifiers)
                and channel_id in identifiers)


def route_member_public_event(payload: dict) -> dict | None:
    """Route an authenticated callback through event-family privacy checks."""
    from .member_channel_events import route_member_public_event as route

    return route(payload)


def _members_mapping(row) -> bool:
    """Identify a Members destination without converting an adopted room."""
    if not isinstance(row.room_state, dict):
        return False
    applied = row.room_state.get("applied") or {}
    if not isinstance(applied, dict):
        return False
    return bool(row.audience == CommunityBridgeChannelAudience.MEMBERS
                and row.destination_platform == CommunityBridgePlatform.BUZZ
                and applied.get("audience") in (None, "members"))


def _schedule_audience_reductions(targets):
    """Attempt captured room reductions only after local authority commits."""
    targets = tuple(targets)
    if not targets:
        return

    def reduce_after_commit():
        from .member_audiences import sync_audience

        for pk in targets[:REFRESH_BATCH]:
            try:
                sync_audience(pk, reduction_only=True)
            except Exception as exc:
                logger.warning("member_channel_authority_cleanup_pending error_code=%s", type(exc).__name__)

    transaction.on_commit(reduce_after_commit)


def mark_member_channels_pending(user_id, *, reason, channel_ids=None, reduction=False):
    """Persist an authority wake under User → Channel order, without I/O.

    Capture this wake before deleting grants or memberships. Reductions ignore
    rollout flags and disabled/archived rows. A conservative workspace wake
    also retains cleanup targets when the subject's last membership disappears.
    The worker re-derives every entitlement; a wake grants no access.
    """
    with transaction.atomic():
        if not get_user_model().objects.select_for_update().filter(pk=user_id).exists():
            return 0
        workspaces = set(SlackDmMirrorGrant.objects.filter(user_id=user_id).values_list(
            "slack_workspace_id", flat=True,
        )) | set(SlackChannelMembership.objects.filter(grant__user_id=user_id).values_list(
            "slack_workspace_id", flat=True,
        ))
        rows = CommunityBridgeChannel.objects.select_for_update().filter(
            slack_workspace_id__in=workspaces, audience=CommunityBridgeChannelAudience.MEMBERS,
            destination_platform=CommunityBridgePlatform.BUZZ,
        ).order_by("pk")
        if channel_ids is not None:
            rows = rows.filter(slack_channel_id__in=channel_ids)
        targets = []
        for row in rows:
            if not _members_mapping(row):
                continue
            row.audience_sync_pending = True
            if reduction:
                row.enabled = False
            row.room_state = {**(row.room_state or {}), "audience_pending_reason": str(reason)[:64]}
            row.save(update_fields=["audience_sync_pending", "enabled", "room_state", "updated_at"])
            targets.append(row.pk)
        if reduction:
            _schedule_audience_reductions(targets)
        return len(targets)


def schedule_member_authority_change(user_id, *, reason, reduction=False):
    """Capture durable cleanup targets before a destructive authority change.

    Call inside the authority transaction. Local denial commits even during an
    adapter outage; the synchronous on-commit attempt is subtract-only and
    leaves durable pending work for the worker on failure.
    """
    if reason in {"slack_grant_paused", "slack_consent_revoked", "slack_oauth_generation_changed",
                  "slack_consent_renewed", "slack_consent_changed"}:
        from integrations.services.slack_member_membership_epoch import invalidate_memberships_locked

        invalidate_memberships_locked(user_id)
    mark_member_channels_pending(user_id, reason=reason, reduction=reduction)


def mark_source_reduction(workspace_id, channel_id, *, reason,
                          event_observed_at=None, new_channel_id=None) -> bool:
    """Close a known source using trusted control metadata, never discovery.

    This metadata-only transaction ends before any authority or adapter work.
    Community mappings also close on privacy changes so their legacy bot lane
    cannot keep importing the newly private source.
    """
    if not valid_slack_channel_identity(workspace_id, channel_id):
        return False
    reasons = {"member_left", "converted_to_private", "slack_connect", "deleted",
               "archived", "rekey_requires_room_migration"}
    if reason not in reasons:
        return False
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().filter(
            slack_workspace_id=workspace_id, slack_channel_id=channel_id,
        ).first()
        if row is None:
            return False
        if reason == "member_left":
            if _members_mapping(row):
                row.audience_sync_pending = True
                row.enabled = False
                row.save(update_fields=["audience_sync_pending", "enabled", "updated_at"])
                _schedule_audience_reductions([row.pk])
            return True
        state = dict(row.room_state or {})
        observed = event_observed_at
        if isinstance(observed, datetime) and timezone.is_aware(observed):
            previous = parse_datetime(str(state.get("source_reduction_observed_at") or ""))
            if previous is not None and timezone.is_aware(previous) and observed < previous:
                return False
            if row.slack_seen_at is not None and observed < row.slack_seen_at:
                return False
            state["source_reduction_observed_at"] = observed.isoformat()
        row.enabled = False
        if reason not in {"archived", "deleted"} and row.mirror_decision == CommunityBridgeMirrorDecision.MIRROR:
            row.mirror_decision = CommunityBridgeMirrorDecision.HOLD
            row.hold_reason = reason
        row.room_sync_pending = bool(row.destination_channel_id)
        if reason == "converted_to_private":
            row.slack_visibility = "private"
        elif reason == "deleted":
            row.slack_deleted = True
        elif reason == "archived":
            row.slack_archived = True
        elif reason == "rekey_requires_room_migration":
            row.room_sync_error = reason
            if valid_slack_channel_identity(workspace_id, new_channel_id):
                state["rekey_target"] = new_channel_id
        if _members_mapping(row):
            row.audience_sync_pending = True
        row.room_state = state
        row.save()
        from .observability import room
        room(row, "held")
        if _members_mapping(row):
            _schedule_audience_reductions([row.pk])
        return True


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
    with transaction.atomic():
        row, _ = CommunityBridgeChannelRefresh.objects.select_for_update().get_or_create(
            slack_channel_id=channel_id,
            defaults={"slack_workspace_id": workspace_id, "reason": "",
                      "available_at": timezone.now()},
        )
        row.slack_workspace_id = workspace_id
        row.reason = _refresh_reason(row.reason, reason)
        row.available_at = timezone.now() + timedelta(seconds=delay_seconds)
        row.attempts = 0
        row.last_error_code = ""
        row.save()


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
        if row.destination_channel_id or row.audience == "members":
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
    archived = (row.slack_archived or row.slack_deleted or row.slack_visibility != "public"
                or row.hold_reason in {"slack_connect", "rekey_requires_room_migration"})
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
    ).update(room_sync_error=code[:100], updated_at=timezone.now(), **changes))


def sync_room(pk: int, *, reduction_only=False) -> str:
    """Apply a row's desired room state through the MLAI Chat adapter.

    Adapter calls run outside any transaction. This never joins Slack or opens
    the delivery gate; Phase 4 owns reader, membership and readiness decisions.
    The result is written back only if the immutable request identity is current.
    """
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=pk)
        if row.room_sync_error == "rekey_requires_room_migration":
            from .observability import room
            room(row, "held")
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
        if reduction_only and (not row.destination_channel_id or desired is None or not desired["archived"]):
            return "unchanged"
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
        acknowledged = desired
        if reduction_only:
            acknowledged = {**(row.room_state.get("applied") or {}), "audience": audience, "archived": True}
        if destination and previous_audience and previous_audience != audience:
            row.room_sync_error = "immutable_room_audience"
            row.save(update_fields=["room_sync_error", "updated_at"])
            from .observability import room
            room(row, "held")
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
    request_name = (row.destination_channel_name or desired["name"]) if reduction_only else desired["name"]
    try:
        result = BuzzBridgeClient.upsert_public_channel(
            slack_workspace_id=slack_workspace_id,
            slack_channel_id=slack_channel_id,
            audience=audience,
            name=request_name,
            archived=desired["archived"],
            about=None if reduction_only else desired["about"],
            topic=None if reduction_only else desired["topic"],
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
            recorded = _record_sync_failure(pk, "name_conflict", request_state=request_state, **changes)
            if recorded:
                from .observability import room
                room(row, "held")
            return "held" if recorded else "retry"
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
        row.destination_channel_name = request_name
        row.room_state = {
            **row.room_state,
            "applied": acknowledged,
            "text_managed": bool(row.room_state.get("text_managed") or managed),
        }
        row.room_sync_error = ""
        row.room_synced_at = timezone.now()
        if desired["archived"] or row.mirror_decision != CommunityBridgeMirrorDecision.MIRROR:
            row.enabled = False
        # Slack may have changed again while the adapter call was in flight.
        current_desired = _desired_room(row)
        row.room_sync_pending = (
            not bool(current_desired and current_desired["archived"])
            if reduction_only else _room_out_of_date(row)
        )
        row.save()
        from .observability import room
        room(row, "archived" if desired["archived"] else "room_created" if not destination else "room_updated")
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


def process_refreshes_once(limit: int = REFRESH_BATCH) -> int:
    """Refresh bounded public metadata with a current member token only.

    Metadata inspection may observe a privacy reduction, but cannot import
    content or add keys. Unknown rooms are created empty and stay disabled
    until the separate authority-serialized audience acknowledgement.
    """
    from integrations.services import slack_dm_mirror as dm, slack_member_access as access
    from integrations.services.slack_channel_membership import consenting_members, mark_joined, mark_left
    from integrations.services.slack_chat_catalog import PUBLIC_CHANNEL_SCOPES, public_channels_enabled
    from .member_audiences import sync_audience
    from integrations.services.message_sync.scheduler import BudgetDeferred

    if not access.enabled():
        return 0
    limit = max(1, min(int(limit), REFRESH_BATCH))
    stale = timezone.now() - timedelta(minutes=5)
    # Periodic metadata audit uses existing source identities, never Slack
    # discovery. Exclude already queued rows so their leases cannot be reset.
    queued = CommunityBridgeChannelRefresh.objects.values("slack_channel_id")
    for row in CommunityBridgeChannel.objects.filter(
        audience="members", destination_platform="buzz", mirror_decision="mirror",
    ).filter(Q(slack_seen_at__isnull=True) | Q(slack_seen_at__lt=stale)).exclude(
        slack_channel_id__in=queued,
    ).order_by("slack_seen_at", "pk")[:limit]:
        if _members_mapping(row) and member_channel_in_scope(row.slack_workspace_id, row.slack_channel_id):
            queue_refresh(row.slack_workspace_id, row.slack_channel_id, reason="metadata_audit")
    completed = 0
    for refresh in claim_refreshes(limit):
        workspace_id, channel_id = refresh.slack_workspace_id, refresh.slack_channel_id
        if not member_channel_in_scope(workspace_id, channel_id):
            complete_refresh(refresh)
            continue
        if CommunityBridgeChannel.objects.filter(
            slack_workspace_id=workspace_id, slack_channel_id=channel_id,
            mirror_decision=CommunityBridgeMirrorDecision.EXCLUDE,
        ).exists():
            complete_refresh(refresh)
            continue
        hint = info_reader_hint(refresh.reason)
        # Resolve all selection/cache authority before taking any Channel lock.
        hinted_reader = access.hinted_info_reader(workspace_id, channel_id, hint)
        if hinted_reader is not None and access.info_reader_deferred(hinted_reader, channel_id):
            _retry_refresh(refresh, seconds=60, code="info_reader_deferred", count_attempt=False)
            continue
        reader = access.reader_for_channel(workspace_id, channel_id, purpose="info", info_reader_hint=hint)
        if reader is None:
            _retry_refresh(refresh, seconds=60, code="no_reader", count_attempt=False)
            continue
        readers = [reader]
        if hinted_reader is not None and hinted_reader.pk != reader.pk:
            readers.append(hinted_reader)
        # Preserve known-reader priority, then prove the actor's own membership.
        # At most two bounded INFO probes; no selector or token refresh runs
        # while the prior reader's User/Channel transaction is held.
        for index, reader in enumerate(readers):
            final_reader = index == len(readers) - 1
            authority = None
            started_at = timezone.now()
            try:
                authority = dm._capture_slack_grant_api_authority(reader)
                # Unlike a content call, an info inspection must retain a private
                # or Connect response long enough to close its known old mapping.
                with transaction.atomic():
                    current, connection = dm._lock_slack_grant_api_authority(
                        authority, required_scopes=PUBLIC_CHANNEL_SCOPES,
                    )
                    current.connection = connection
                    if not public_channels_enabled(current, authority=authority):
                        raise access.MemberAccessDenied("Current Slack consent is required.")
                    response = dm._call_slack_with_grant_authority(
                        authority, "conversations_info", required_scopes={"channels:read"}, channel=channel_id,
                    )
                raw = response.get("channel")
                if not isinstance(raw, dict) or raw.get("id") != channel_id:
                    raise ValueError("invalid_channel_info")
                if not access._public_channel_response(response, channel_id):
                    private = any(raw.get(key) is True for key in ("is_private", "is_group", "is_im", "is_mpim"))
                    shared = any(raw.get(key) for key in (
                        "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared",
                    ))
                    if private or shared:
                        with transaction.atomic():
                            dm._lock_slack_grant_api_authority(authority, required_scopes=PUBLIC_CHANNEL_SCOPES)
                            mark_source_reduction(workspace_id, channel_id,
                                                  reason="converted_to_private" if private else "slack_connect",
                                                  event_observed_at=started_at)
                        known = CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace_id,
                                                                      slack_channel_id=channel_id).first()
                        if known is not None:
                            sync_audience(known.pk, reduction_only=True)
                            sync_room(known.pk)
                        complete_refresh(refresh)
                        completed += 1
                        break
                    raise ValueError("unproven_public_channel")
                state = SlackChannelState.from_slack(raw)
                with transaction.atomic():
                    current, connection = dm._lock_slack_grant_api_authority(
                        authority, required_scopes=PUBLIC_CHANNEL_SCOPES,
                    )
                    current.connection = connection
                    if raw.get("is_member") is True:
                        mark_joined(current, channel_id, observed_at=started_at, source="inventory",
                                    authority=authority, public_verified=True)
                    elif raw.get("is_member") is False:
                        mark_left(current, channel_id, observed_at=started_at, source="inventory",
                                  authority=authority, public_verified=True)
                    members = consenting_members(workspace_id, channel_id)
                    row = CommunityBridgeChannel.objects.select_for_update().filter(
                        slack_channel_id=channel_id, slack_workspace_id=workspace_id,
                    ).first()
                    if row is None:
                        if not members or state.is_archived:
                            if final_reader or state.is_archived:
                                complete_refresh(refresh)
                            if state.is_archived:
                                break
                            continue
                        row = CommunityBridgeChannel.objects.create(
                            slack_workspace_id=workspace_id, slack_channel_id=channel_id,
                            audience="members", destination_platform="buzz", enabled=False,
                            lifecycle_source="reconcile", mirror_decision="mirror",
                            room_sync_pending=True, audience_sync_pending=True,
                        )
                    if row.slack_seen_at is None or row.slack_seen_at <= started_at:
                        _observe(row, state, started_at)
                        row.reader_grant = next((grant for grant in members if grant.pk == current.pk),
                                                members[0] if members else None)
                        row.sync_paused_reason = "" if row.reader_grant_id else "no_reader"
                        if _members_mapping(row):
                            row.room_sync_pending = _room_out_of_date(row)
                            row.audience_sync_pending = True
                        row.save()
                    pk = row.pk
                sync_room(pk)
                sync_audience(pk)
                if final_reader:
                    complete_refresh(refresh)
                    completed += 1
            except Exception as exc:
                if isinstance(exc, dm.SlackApiError) and authority is not None:
                    access.record_reader_failure(reader, exc, channel_id=channel_id,
                                                 authority=authority, purpose="info",
                                                 observed_at=started_at)
                deferred = isinstance(exc, (BudgetDeferred, dm.SlackDmMirrorRateLimited))
                # A live actor's discovery must survive scheduling and transient
                # failures. A hint never changes the provider's authority checks.
                preserve_hint = hint is not None
                _retry_refresh(refresh, seconds=max(60, getattr(exc, "retry_after", 0)),
                               code=type(exc).__name__, count_attempt=not (deferred or preserve_hint))
                logger.warning("member_channel_refresh_failed error_code=%s", type(exc).__name__)
                break
    return completed


def sync_audiences_once(limit=REFRESH_BATCH):
    """Run the authority lane independently of expansion feature switches."""
    from .member_audiences import sync_audiences_once as sync

    return sync(limit=limit)


def process_room_reductions_once(limit=REFRESH_BATCH):
    """Archive only existing closed-source rooms, even with rollout off.

    This cannot provision a destination, change its audience, unarchive it or
    fetch Slack metadata. It supplies no new topic/about/name metadata.
    """
    rows = CommunityBridgeChannel.objects.filter(
        destination_platform="buzz", room_sync_pending=True,
    ).filter(
        Q(slack_archived=True) | Q(slack_deleted=True) | ~Q(slack_visibility="public")
        | Q(hold_reason__in=("slack_connect", "rekey_requires_room_migration")),
    ).exclude(destination_channel_id="").order_by("updated_at", "pk")
    completed = 0
    for row in rows[:max(1, min(int(limit), REFRESH_BATCH))]:
        desired = _desired_room(row)
        if desired is not None and desired["archived"]:
            completed += sync_room(row.pk, reduction_only=True) == "synced"
    return completed
