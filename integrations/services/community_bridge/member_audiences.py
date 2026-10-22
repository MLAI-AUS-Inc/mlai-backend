"""Authority-serialized Members audiences using the relay's fixed-command CAS.

No Slack requests, token refresh or participant lists are persisted here. A
metadata-only transaction commits each command UUID before the authority
transaction, which locks Users first and Channel last through bounded adapter
I/O. Ambiguous commands are never rebased or replayed with changed content.
"""

from collections import Counter
from datetime import timedelta
import hashlib
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeChannel, ExternalServiceConnection, SlackChannelMembership, SlackDmMirrorGrant
from integrations.services import slack_channel_membership, slack_member_access
from integrations.services.slack_chat_catalog import public_channels_enabled

from .buzz import BuzzAudienceConflict, BuzzBridgeClient, BuzzBridgeError, EVENT_ID_RE


MAX_KEYS = 1000
SOURCE_FRESH_SECONDS = 300
MAX_AUTHORITY_USERS = 1001


class _CohortChanged(Exception):
    pass


def audience_hash(pubkeys):
    """Match buzz-db bridge_channel_audience.rs: sorted unique raw32 keys."""
    keys = set(pubkeys)
    if any(not isinstance(key, str) or not EVENT_ID_RE.fullmatch(key) for key in keys):
        raise ValueError("invalid_member_audience_key")
    digest = hashlib.sha256(b"mlai:bridge-channel-audience:v1:")
    for key in sorted(keys):
        digest.update(bytes.fromhex(key))
    return digest.hexdigest()


def _device_key_result(user_ids):
    from integrations.services.slack_dm_mirror import _owner_device_pubkeys

    keys = set()
    for user_id in sorted(user_ids):
        values = _owner_device_pubkeys(user_id, limit=8)
        if any(not EVENT_ID_RE.fullmatch(value) for value in values):
            return None, "audience_authority_unavailable"
        keys.update(values)
        if len(keys) > MAX_KEYS:
            return None, "audience_too_large"
    return keys, ""


def _device_keys(user_ids):
    return _device_key_result(user_ids)[0]


def current_audience_keys(workspace_id, channel_id):
    """Flag-independent readiness snapshot; None means no complete proof.

    This is never an I/O lease. The synchronizer independently locks and
    derives all authority again. Empty is a valid complete audience.
    """
    from .member_channels import valid_slack_channel_identity

    if not valid_slack_channel_identity(workspace_id, channel_id):
        return None
    if len(_cohort(workspace_id, channel_id)) > MAX_AUTHORITY_USERS:
        return None
    grants = slack_channel_membership.consenting_members(workspace_id, channel_id)
    return _device_keys({grant.user_id for grant in grants})


def _identity(row):
    return (row.slack_workspace_id, row.slack_channel_id, row.destination_channel_id)


def _managed(row):
    from .member_channels import valid_slack_channel_identity

    applied = (row.room_state or {}).get("applied") or {}
    try:
        destination = uuid.UUID(row.destination_channel_id)
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(isinstance(applied, dict) and row.audience == "members" and row.destination_platform == "buzz"
                and applied.get("audience") == "members" and destination.int
                and valid_slack_channel_identity(row.slack_workspace_id, row.slack_channel_id))


def _prepare(pk):
    with transaction.atomic():
        row = CommunityBridgeChannel.objects.select_for_update().filter(pk=pk).first()
        if row is None or not _managed(row):
            return None
        command = str(uuid.uuid4())
        row.room_state = {**(row.room_state or {}), "audience_attempt": {
            "generation": command, "prepared_at": timezone.now().isoformat(), "status": "prepared",
        }}
        row.audience_sync_pending = True
        row.save(update_fields=("room_state", "audience_sync_pending", "updated_at"))
        return command, _identity(row)


def _cohort(workspace, channel):
    return set(SlackChannelMembership.objects.filter(
        slack_workspace_id=workspace, slack_channel_id=channel, state="joined",
        grant__slack_workspace_id=workspace,
    ).order_by("grant__user_id").values_list("grant__user_id", flat=True).distinct()[:MAX_AUTHORITY_USERS + 1])


def _lock_authority(user_ids, workspace, channel):
    users = list(get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
    grants = list(SlackDmMirrorGrant.objects.select_for_update().filter(
        user_id__in=user_ids,
    ).order_by("user_id", "pk"))
    connections = list(ExternalServiceConnection.objects.select_for_update().filter(
        user_id__in=user_ids, provider="slack",
    ).order_by("user_id", "pk"))
    list(CommunityChatDevice.objects.select_for_update().filter(user_id__in=user_ids).order_by("user_id", "pk"))
    memberships = list(SlackChannelMembership.objects.select_for_update().filter(
        grant__user_id__in=user_ids, slack_workspace_id=workspace, slack_channel_id=channel,
    ).order_by("grant_id", "pk"))
    return users, grants, connections, memberships


def _eligible_grants(locked, workspace):
    users, grants, connections, memberships = locked
    by_user = {user.pk: user for user in users}
    by_connection = {item.pk: item for item in connections}
    by_grant = {item.pk: item for item in grants}
    eligible = []
    for member in memberships:
        grant = by_grant.get(member.grant_id)
        if (grant is None or grant.slack_workspace_id != workspace or grant.user_id not in by_user
                or grant.connection_id not in by_connection):
            continue
        grant.user = by_user[grant.user_id]
        grant.connection = by_connection[grant.connection_id]
        if slack_member_access._membership_state(grant, member) == "joined" and public_channels_enabled(grant):
            eligible.append(grant)
    return eligible


def _source_closed(row):
    return bool(row.slack_visibility != "public" or row.slack_archived or row.slack_deleted
                or row.mirror_decision != "mirror"
                or (row.hold_reason and row.hold_reason not in {"audience_too_large", "audience_authority_unavailable"})
                or row.room_sync_error == "rekey_requires_room_migration"
                or not row.slack_seen_at)


def _expansion_allowed(row, reduction_only):
    from .member_channels import member_channel_in_scope

    now = timezone.now()
    return bool(not reduction_only and not _source_closed(row) and slack_member_access.enabled()
                and member_channel_in_scope(row.slack_workspace_id, row.slack_channel_id)
                and now - timedelta(seconds=SOURCE_FRESH_SECONDS) <= row.slack_seen_at <= now)


def _save_attempt(row, status, *, pending=True):
    state = dict(row.room_state or {})
    attempt = dict(state.get("audience_attempt") or {})
    attempt["status"] = status
    state.update(audience_attempt=attempt, audience_audited_at=timezone.now().isoformat())
    row.room_state = state
    row.audience_sync_pending = pending
    if status in {"conflict", "retry"}:
        row.enabled = False
    row.save(update_fields=("room_state", "audience_sync_pending", "enabled", "updated_at"))


def _execute(pk, prepared, *, reduction_only):
    command, identity = prepared
    workspace, channel, destination = identity
    user_ids = _cohort(workspace, channel)
    bounded_empty = len(user_ids) > MAX_AUTHORITY_USERS
    with transaction.atomic():
        locked = _lock_authority(set() if bounded_empty else user_ids, workspace, channel)
        row = CommunityBridgeChannel.objects.select_for_update().filter(pk=pk).first()
        if (row is None or not _managed(row) or _identity(row) != identity
                or (row.room_state or {}).get("audience_attempt", {}).get("generation") != command):
            return "superseded"
        if not bounded_empty and not _cohort(workspace, channel).issubset(user_ids):
            raise _CohortChanged()
        locked_ids = {item.pk for item in locked[3]}
        if not bounded_empty and SlackChannelMembership.objects.filter(
            slack_workspace_id=workspace, slack_channel_id=channel, state="joined",
            grant__slack_workspace_id=workspace,
        ).exclude(pk__in=locked_ids).exists():
            raise _CohortChanged()
        eligible = _eligible_grants(locked, workspace)
        keys, hold = (None, "audience_authority_unavailable") if bounded_empty else _device_key_result(
            {grant.user_id for grant in eligible},
        )
        expansion = _expansion_allowed(row, reduction_only)
        try:
            current = BuzzBridgeClient.inspect_member_audience(destination, generation=command)
            previous = set(current["participant_pubkeys"])
            if hold or _source_closed(row):
                desired = set()
            else:
                desired = keys if expansion else previous & keys
            generation = current["generation"]
            if desired != previous or generation is None:
                result = BuzzBridgeClient.update_member_audience(
                    destination, previous_pubkeys=sorted(previous), participant_pubkeys=sorted(desired),
                    expected_generation=generation, generation=command,
                )
                generation = result["generation"]
        except BuzzAudienceConflict:
            _save_attempt(row, "conflict")
            return "conflict"
        except BuzzBridgeError:
            _save_attempt(row, "retry")
            return "retry"
        digest = audience_hash(desired)
        row.audience_generation = uuid.UUID(generation)
        row.audience_hash = digest
        state = dict(row.room_state or {})
        state["audience_applied"] = {
            "generation": generation, "hash": digest, "workspace_id": workspace,
            "channel_id": channel, "destination_channel_id": destination,
        }
        row.room_state = state
        if hold:
            row.hold_reason = hold
        elif expansion and row.hold_reason in {"audience_too_large", "audience_authority_unavailable"}:
            row.hold_reason = ""
        reader_current = row.reader_grant_id in {grant.pk for grant in eligible}
        ready = bool(expansion and desired and not hold and reader_current
                     and not row.hold_reason and not row.room_sync_pending and not row.room_sync_error)
        # A reduction never opens a gate; a complete full sync may open it only
        # under the same authority and room fence as its acknowledged audience.
        row.enabled = ready
        row.save(update_fields=("audience_generation", "audience_hash", "room_state",
                                "enabled", "hold_reason", "updated_at"))
        _save_attempt(row, "applied", pending=bool(hold))
        return "held" if hold else "applied"


def sync_audience(pk, *, reduction_only=False):
    """Synchronize one exact Members room; never call inside a caller's tx."""
    if connection.in_atomic_block:
        return "deferred"
    prepared = _prepare(pk)
    if prepared is None:
        return "ignored"
    try:
        return _execute(pk, prepared, reduction_only=reduction_only)
    except _CohortChanged:
        # Yield audit fairness without erasing a newer worker's command/wake.
        with transaction.atomic():
            row = CommunityBridgeChannel.objects.select_for_update().filter(pk=pk).first()
            if (row is not None and _managed(row)
                    and (row.room_state or {}).get("audience_attempt", {}).get("generation") == prepared[0]):
                _save_attempt(row, "cohort_changed")
        return "cohort_changed"


def sync_audiences_once(limit=5):
    """Process pending work plus a fair existing-room audit, flags on or off."""
    limit = max(1, min(int(limit), 20))
    rows = CommunityBridgeChannel.objects.filter(
        audience="members", destination_platform="buzz", room_state__applied__audience="members",
    ).exclude(destination_channel_id="")
    pending = list(rows.filter(audience_sync_pending=True).order_by("updated_at", "pk").values_list(
        "pk", flat=True,
    )[:max(0, limit - 1)])
    audits = list(rows.exclude(pk__in=pending).order_by(
        F("room_state__audience_audited_at").asc(nulls_first=True), "pk",
    ).values_list("pk", flat=True)[:limit - len(pending)])
    return dict(Counter(sync_audience(pk) for pk in pending + audits))
