"""Owner-only Slack source directory; never creates relay rooms or imports bodies."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.models import SlackOwnerConversationInventory
from integrations.services.slack_chat_catalog import raw_conversation_kind


KEY = "slack_owner_inventory_v1"
CONSENT_KEY = "slack_owner_inventory_metadata_consent_v1"
KINDS = ("im", "mpim", "private_channel", "public_channel")
READ_FRESH_SECONDS = 120
SOURCE_ID = re.compile(r"^[A-Z0-9]{2,100}$")
PUBLIC_PAGE_SIZE = 200
PUBLIC_PAGES_PER_TURN = 3
PUBLIC_MEMBERSHIP_IDS_LIMIT = 10000


def enabled() -> bool:
    """Permit operators to disable collection and API reads during rollback."""
    return bool(getattr(settings, "SLACK_OWNER_INVENTORY_ENABLED", False))


def _identity(authority) -> dict:
    return {
        "grant_id": authority.grant_id,
        "connection_id": authority.connection_id,
        "workspace_id": authority.workspace_id,
        "slack_user_id": authority.slack_user_id,
        "consent_generation": authority.consent_generation,
        "consent_version": authority.consent_version,
        "oauth_generation": authority.oauth_generation,
        "scopes": list(authority.scopes),
    }


def _consent_identity(authority) -> dict:
    return _identity(authority)


def has_metadata_consent(connection, authority) -> bool:
    """A prior message-history choice alone does not authorize older names."""
    return (connection.sync_cursor or {}).get(CONSENT_KEY) == _consent_identity(authority)


def grant_metadata_consent(grant) -> None:
    """Record the owner's explicit inventory choice after the connect POST."""
    from integrations.services.slack_dm_mirror import (
        _capture_slack_grant_api_authority,
        _lock_slack_grant_api_authority,
    )

    authority = _capture_slack_grant_api_authority(grant)
    with transaction.atomic():
        locked_grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if has_metadata_consent(connection, authority):
            return
        # A reconnect may revoke access to previously enumerated rooms. Only
        # pages collected under the newly consented authority may reappear.
        SlackOwnerConversationInventory.objects.filter(grant=locked_grant).delete()
        cursor = dict(connection.sync_cursor or {})
        cursor[CONSENT_KEY] = _consent_identity(authority)
        state = _new_state(authority)
        state["started_after"] = timezone.now().isoformat()
        cursor[KEY] = state
        connection.sync_cursor = cursor
        connection.save(update_fields=("sync_cursor", "updated_at"))
        locked_grant.last_discovery_at = None
        locked_grant.save(update_fields=("last_discovery_at", "updated_at"))


def _new_state(authority) -> dict:
    seed = repr(sorted(_identity(authority).items())).encode()
    epoch = hmac.new(settings.SECRET_KEY.encode(), seed, hashlib.sha256).hexdigest()[:32]
    return {
        **_identity(authority),
        "epoch": epoch,
        "revision": 1,
        "coverage": {kind: "pending" for kind in KINDS},
        "private_sweep": "",
        "directory_private_sweep": "",
        "directory_private_cursor": "",
        "directory_private_enabled": False,
        "public_sweep": "",
        "public_cursor": "",
        "public_started_at": None,
        "last_full_sweep_at": None,
    }


def state_for(connection, authority) -> dict:
    raw = (connection.sync_cursor or {}).get(KEY)
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in _identity(authority).items()):
        return _new_state(authority)
    return raw


def needs_private_sweep(connection, authority) -> bool:
    """Let a consented owner directory advance during device retry cooldowns."""
    if not enabled() or not has_metadata_consent(connection, authority):
        return False
    coverage = state_for(connection, authority).get("coverage")
    return isinstance(coverage, dict) and any(
        coverage.get(kind) == "pending" for kind in KINDS[:3]
    )


def needs_public_sweep(connection, authority) -> bool:
    """Continue an unfinished authorized public inventory without history I/O."""
    if not enabled() or not public_inventory_allowed(None, connection, authority):
        return False
    state = state_for(connection, authority)
    return bool(state.get("public_cursor") or state.get("public_sweep"))


def _save_state(connection, state) -> None:
    cursor = dict(connection.sync_cursor or {})
    cursor[KEY] = state
    connection.sync_cursor = cursor
    connection.save(update_fields=("sync_cursor", "updated_at"))


def rotate_epoch_locked(connection) -> None:
    """Invalidate client pages and discard names after a grant transition.

    A pause can outlast a Slack membership change. Re-enumerate on resume
    before publishing any previously visible private channel names again.
    """
    SlackOwnerConversationInventory.objects.filter(grant__connection=connection).delete()
    cursor = dict(connection.sync_cursor or {})
    state = dict(cursor.get(KEY) or {})
    state.update(epoch=uuid.uuid4().hex, revision=int(state.get("revision") or 0) + 1)
    state["coverage"] = {kind: "pending" for kind in KINDS}
    state.update(
        private_sweep="", directory_private_sweep="", directory_private_cursor="",
        directory_private_enabled=False, public_sweep="", public_cursor="",
        public_started_at=None, public_membership_ids=[], public_membership_complete=False,
        last_full_sweep_at=None,
    )
    cursor[KEY] = state
    connection.sync_cursor = cursor
    connection.save(update_fields=("sync_cursor", "updated_at"))


def device_epoch(grant, authority, device, *, state=None) -> str:
    """Opaque client cache boundary for one verified device and grant."""
    state = state or state_for(grant.connection, authority)
    material = "\0".join(str(value) for value in (
        grant.user_id, grant.pk, authority.workspace_id, authority.slack_user_id,
        authority.consent_generation, authority.oauth_generation,
        state["epoch"], device.pk, device.public_key, device.verified_at,
    ))
    return hmac.new(settings.SECRET_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()


def _kind(raw):
    kind = raw_conversation_kind(raw)
    if kind in KINDS:
        return kind
    if raw.get("is_channel") and raw.get("is_private") is False:
        return "public_channel"
    return None


def _eligibility(raw):
    if any(bool(raw.get(name)) for name in (
        "is_ext_shared", "is_ext_ws_shared", "is_ext_shared_channel",
        "is_external_shared", "is_pending_ext_shared",
    )):
        return "unsupported_external"
    if raw.get("is_shared") and not raw.get("is_org_shared"):
        return "unsupported_ambiguous"
    return "eligible"


def _activity_ts(raw) -> str:
    latest = raw.get("latest")
    values = [raw.get("latest_reply")]
    if isinstance(latest, dict):
        values.extend((latest.get("ts"), latest.get("latest_reply")))
    else:
        values.append(latest)
    stamps = []
    for value in values:
        try:
            stamp = Decimal(str(value))
            if stamp.is_finite() and 0 < stamp <= Decimal(str(time.time() + 300)):
                stamps.append(stamp)
        except (InvalidOperation, ValueError, TypeError):
            continue
    return format(max(stamps), "f") if stamps else ""


def _upsert_rows(grant, rows, sweep_id, *, expected_kinds, profiles=None):
    now = timezone.now()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("Slack returned a malformed conversation page.")
        source_id = str(raw.get("id") or "").strip()
        kind = _kind(raw)
        if not SOURCE_ID.fullmatch(source_id) or kind not in expected_kinds:
            raise ValueError("Slack returned an unexpected conversation in the inventory page.")
        name = str(raw.get("name") or "")[:255] if kind != "im" else ""
        counterpart = str(raw.get("user") or "")[:100] if kind == "im" else ""
        existing = SlackOwnerConversationInventory.objects.filter(
            grant=grant, slack_conversation_id=source_id,
        ).first()
        activity = _activity_ts(raw) or (existing.source_activity_ts if existing else "")
        profile = (profiles or {}).get(counterpart) if counterpart else None
        display_name = (
            str(profile.get("display_name") or "")[:255]
            if isinstance(profile, dict) else (existing.display_name if existing else "")
        )
        SlackOwnerConversationInventory.objects.update_or_create(
            grant=grant,
            slack_conversation_id=source_id,
            defaults={
                "kind": kind,
                "source_name": name,
                "counterpart_slack_user_id": counterpart,
                "display_name": display_name,
                "source_activity_ts": activity,
                "source_archived": raw.get("is_archived") if type(raw.get("is_archived")) is bool else None,
                "source_is_open": raw.get("is_open") if type(raw.get("is_open")) is bool else None,
                "eligibility": _eligibility(raw),
                "last_seen_sweep_id": sweep_id,
                "last_seen_at": now,
            },
        )


def _private_sweep_id(authority, started_at) -> uuid.UUID:
    material = f"{authority.grant_id}:{authority.consent_generation}:{authority.oauth_generation}:{started_at.isoformat()}"
    return uuid.uuid5(uuid.NAMESPACE_URL, material)


def record_private_page(authority, rows, *, started_at, kinds, profiles=None) -> None:
    """Commit a source page before the history recency/provisioning gate."""
    if not enabled():
        return
    from integrations.services.slack_dm_mirror import _lock_slack_grant_api_authority

    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if not has_metadata_consent(connection, authority):
            return
        state = state_for(connection, authority)
        if state.get("started_after") and started_at.isoformat() < state["started_after"]:
            return
        if state.get("directory_private_enabled"):
            # The independent metadata listing owns the sweep boundary. An
            # older history-import page must not replace its deletion marker.
            return
        sweep_id = _private_sweep_id(authority, started_at)
        _upsert_rows(grant, rows, sweep_id, expected_kinds=kinds, profiles=profiles)
        state.update(private_sweep=str(sweep_id), revision=int(state["revision"]) + 1)
        _save_state(connection, state)


def complete_private_sweep(authority, *, started_at, kinds) -> None:
    """Remove missing IDs only after the complete authorized source listing."""
    if not enabled():
        return
    from integrations.services.slack_dm_mirror import _lock_slack_grant_api_authority

    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if not has_metadata_consent(connection, authority):
            return
        state = state_for(connection, authority)
        if state.get("started_after") and started_at.isoformat() < state["started_after"]:
            return
        if state.get("directory_private_enabled"):
            return
        sweep_id = _private_sweep_id(authority, started_at)
        if state.get("private_sweep") != str(sweep_id):
            return
        SlackOwnerConversationInventory.objects.filter(grant=grant, kind__in=kinds).exclude(
            last_seen_sweep_id=sweep_id,
        ).delete()
        coverage = dict(state["coverage"])
        for kind in KINDS[:3]:
            coverage[kind] = "complete" if kind in kinds else "permission_required"
            if kind not in kinds:
                grant.owner_conversation_inventory.filter(kind=kind).update(
                    eligibility="permission_limited",
                )
        state["coverage"] = coverage
        state["last_private_at"] = timezone.now().isoformat()
        state["revision"] = int(state["revision"]) + 1
        if all(value == "complete" for value in coverage.values()):
            state["last_full_sweep_at"] = timezone.now().isoformat()
        _save_state(connection, state)


def collect_private_page(authority) -> None:
    """List one owner-only metadata page independently of message import.

    Old group chats may need many history or profile calls. Their import must
    not prevent the owner from seeing later DMs in the source directory.
    """
    if not enabled():
        return
    from integrations.services.slack_dm_mirror import (
        DIRECT_DM_SCOPES, GROUP_DM_SCOPES, _call_slack_with_grant_authority,
        _lock_slack_grant_api_authority, private_channels_enabled,
    )

    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(
            authority, required_scopes=DIRECT_DM_SCOPES,
        )
        if not has_metadata_consent(connection, authority):
            return
        state = state_for(connection, authority)
        last_private = state.get("last_private_at")
        if all(state["coverage"].get(kind) != "pending" for kind in KINDS[:3]) and last_private:
            try:
                if timezone.now() - timezone.datetime.fromisoformat(last_private) < timedelta(minutes=5):
                    return
            except (ValueError, TypeError):
                pass
        kinds = {"im"}
        if GROUP_DM_SCOPES.issubset(set(authority.scopes)):
            kinds.add("mpim")
        if private_channels_enabled(grant):
            kinds.add("private_channel")
        cursor = str(state.get("directory_private_cursor") or "")
        sweep_id = state.get("directory_private_sweep") or uuid.uuid4().hex
        # Fence history-import writes before the source request. They cannot
        # overwrite this scan's last-seen marker while its cursor is in flight.
        if not state.get("directory_private_enabled"):
            state["directory_private_enabled"] = True
            state["revision"] = int(state["revision"]) + 1
            _save_state(connection, state)
    response = _call_slack_with_grant_authority(
        authority, "users_conversations", required_scopes=DIRECT_DM_SCOPES,
        types=",".join(kind for kind in KINDS[:3] if kind in kinds),
        exclude_archived=False, limit=50, cursor=cursor,
    )
    rows = response.get("channels")
    if not isinstance(rows, list):
        raise ValueError("Slack returned a malformed private conversation page.")
    next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
    if next_cursor and next_cursor == cursor:
        raise ValueError("Slack private inventory pagination made no progress.")
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(
            authority, required_scopes=DIRECT_DM_SCOPES,
        )
        if not has_metadata_consent(connection, authority):
            return
        state = state_for(connection, authority)
        if str(state.get("directory_private_cursor") or "") != cursor:
            return
        if state.get("directory_private_sweep") not in (None, "", sweep_id):
            return
        _upsert_rows(grant, rows, uuid.UUID(sweep_id), expected_kinds=kinds)
        state.update(
            directory_private_sweep=sweep_id, directory_private_cursor=next_cursor,
            revision=int(state["revision"]) + 1,
        )
        if not next_cursor:
            SlackOwnerConversationInventory.objects.filter(grant=grant, kind__in=KINDS[:3]).exclude(
                last_seen_sweep_id=uuid.UUID(sweep_id),
            ).delete()
            coverage = dict(state["coverage"])
            for kind in KINDS[:3]:
                coverage[kind] = "complete" if kind in kinds else "permission_required"
            state.update(coverage=coverage, directory_private_sweep="",
                         last_private_at=timezone.now().isoformat())
            if all(value == "complete" for value in coverage.values()):
                state["last_full_sweep_at"] = timezone.now().isoformat()
        _save_state(connection, state)


def collect_public_page(authority) -> None:
    """Advance a bounded public directory turn; never creates a relay room."""
    if not enabled():
        return
    from integrations.services import slack_member_access as access

    for _ in range(PUBLIC_PAGES_PER_TURN if access.enabled() else 1):
        if not _collect_public_page_once(authority):
            break


def _public_membership_evidence(rows):
    """Read membership from our own-user ``users.conversations`` page.

    That method lists the calling user's conversations and deliberately omits
    ``is_member``. Only this exact-authority endpoint context supplies the
    missing positive; explicit false and malformed present values retain their
    existing meanings. This is not a rule for ``conversations.info`` or events.
    https://docs.slack.dev/reference/methods/users.conversations/
    """
    from integrations.services.slack_member_access import _public_channel_response

    seen = set()
    complete = True
    for raw in rows:
        source_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        member = raw["is_member"] if isinstance(raw, dict) and "is_member" in raw else True
        if (not isinstance(raw, dict) or not _public_channel_response({"channel": raw}, source_id)
                or _eligibility(raw) != "eligible"
                or raw.get("is_im") or raw.get("is_mpim")
                or type(member) is not bool):
            complete = False
            continue
        if member:
            seen.add(source_id)
    return seen, complete


def _collect_public_page_once(authority) -> bool:
    """Persist sweep identity before I/O, then accept only that exact page."""
    from integrations.services.slack_dm_mirror import (
        _call_slack_with_grant_authority,
        _lock_slack_grant_api_authority,
    )
    from integrations.services import slack_member_access as access
    from integrations.services.slack_chat_catalog import public_channels_enabled

    with transaction.atomic():
        required = set() if getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False) else {"im:read"}
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes=required)
        if not public_inventory_allowed(grant, connection, authority):
            return False
        state = state_for(connection, authority)
        if "channels:read" not in authority.scopes:
            coverage = dict(state["coverage"])
            changed = coverage.get("public_channel") != "permission_required"
            coverage["public_channel"] = "permission_required"
            state["coverage"] = coverage
            updated = SlackOwnerConversationInventory.objects.filter(
                grant_id=authority.grant_id, kind="public_channel",
            ).exclude(eligibility="permission_limited").update(eligibility="permission_limited")
            if changed or updated:
                state["revision"] = int(state["revision"]) + 1
            _save_state(connection, state)
            return False
        last_public = state.get("last_public_at")
        if state["coverage"].get("public_channel") == "complete" and last_public:
            try:
                if timezone.now() - timezone.datetime.fromisoformat(last_public) < timedelta(minutes=5):
                    return False
            except (ValueError, TypeError):
                pass
        cursor = str(state.get("public_cursor") or "")
        membership_enabled = access.enabled() and public_channels_enabled(grant, authority=authority)
        if (not state.get("public_sweep") or not state.get("public_started_at")
                or membership_enabled != bool(state.get("public_membership_enabled"))):
            cursor = ""
            state.update(
                public_sweep=uuid.uuid4().hex, public_cursor="",
                public_started_at=timezone.now().isoformat(), public_membership_ids=[],
                public_membership_complete=True, public_membership_enabled=membership_enabled,
            )
            state["revision"] = int(state["revision"]) + 1
            _save_state(connection, state)
        sweep_id = state["public_sweep"]
        started_at = state["public_started_at"]
    response = _call_slack_with_grant_authority(
        authority, "users_conversations", required_scopes={"channels:read"},
        types="public_channel", exclude_archived=False,
        limit=PUBLIC_PAGE_SIZE if access.enabled() else 20, cursor=cursor,
    )
    rows = response.get("channels")
    if not isinstance(rows, list):
        raise ValueError("Slack returned a malformed public conversation page.")
    next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
    if next_cursor and next_cursor == cursor:
        raise ValueError("Slack public inventory pagination made no progress.")
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"channels:read"})
        if not public_inventory_allowed(grant, connection, authority):
            return False
        state = state_for(connection, authority)
        if (str(state.get("public_cursor") or "") != cursor
                or state.get("public_sweep") != sweep_id or state.get("public_started_at") != started_at):
            return False
        _upsert_rows(grant, rows, uuid.UUID(sweep_id), expected_kinds={"public_channel"})
        if membership_enabled:
            seen, complete = _public_membership_evidence(rows)
            seen.update(state.get("public_membership_ids") or [])
            evidence_complete = bool(state.get("public_membership_complete")) and complete
            if len(seen) > PUBLIC_MEMBERSHIP_IDS_LIMIT:
                seen = set()
                evidence_complete = False
            state.update(public_membership_ids=sorted(seen), public_membership_complete=evidence_complete)
        state.update(public_sweep=sweep_id, public_cursor=next_cursor, revision=int(state["revision"]) + 1)
        if not next_cursor:
            if membership_enabled:
                _reconcile_public_memberships(grant, authority, state)
            SlackOwnerConversationInventory.objects.filter(grant=grant, kind="public_channel").exclude(
                last_seen_sweep_id=uuid.UUID(sweep_id),
            ).delete()
            coverage = dict(state["coverage"])
            coverage["public_channel"] = "complete"
            state["coverage"] = coverage
            state["public_sweep"] = ""
            state["public_started_at"] = None
            state["public_membership_ids"] = []
            state["last_public_at"] = timezone.now().isoformat()
            if all(value == "complete" for value in coverage.values()):
                state["last_full_sweep_at"] = timezone.now().isoformat()
        _save_state(connection, state)
        return bool(next_cursor)


def _reconcile_public_memberships(grant, authority, state):
    from integrations.models import CommunityBridgeChannel, SlackChannelMembership
    from integrations.services import slack_channel_membership as membership
    from integrations.services import slack_member_access as access
    from integrations.services.community_bridge.member_channels import member_channel_in_scope, queue_refresh

    if not access.enabled() or not state.get("public_membership_complete"):
        return
    try:
        started = timezone.datetime.fromisoformat(state["public_started_at"])
    except (TypeError, ValueError, KeyError):
        return
    seen = set(state.get("public_membership_ids") or [])
    membership.reconcile_from_inventory(grant, seen, True, started_at=started, authority=authority)
    from integrations.services.slack_member_membership_epoch import membership_floor

    floor = membership_floor(grant)
    if floor is None:
        return
    current = SlackChannelMembership.objects.filter(
        grant=grant, slack_workspace_id=grant.slack_workspace_id,
        slack_channel_id__in=seen, state="joined", observed_at__gte=floor,
    ).values_list("slack_channel_id", flat=True)
    known = set(CommunityBridgeChannel.objects.filter(
        slack_workspace_id=grant.slack_workspace_id, slack_channel_id__in=seen,
    ).values_list("slack_channel_id", flat=True))
    for channel_id in current:
        if channel_id not in known and member_channel_in_scope(grant.slack_workspace_id, channel_id):
            queue_refresh(grant.slack_workspace_id, channel_id, reason="inventory_membership")


def public_inventory_allowed(grant, connection, authority) -> bool:
    """Keep existing metadata consent; add only the enabled public-source lane."""
    if has_metadata_consent(connection, authority):
        return True
    if not getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False):
        return False
    from integrations.services.slack_chat_catalog import public_channels_enabled

    if grant is None:
        from integrations.models import SlackDmMirrorGrant
        grant = SlackDmMirrorGrant.objects.select_related("user", "connection").filter(pk=authority.grant_id).first()
    return grant is not None and public_channels_enabled(grant, authority=authority)


def source_read_targets(grant, authority, existing_targets, *, include_routed=False):
    """Add consented source conversations to the account's unread sweep.

    Targets already represented by a routed room share the same Slack cache key,
    so the worker never spends quota reading a source conversation twice.
    """
    if not enabled() or not has_metadata_consent(grant.connection, authority):
        return []
    from integrations.services.slack_chat_read_state import ReadTarget

    routed = {target.slack_id: target for target in existing_targets}
    seen = set(routed)
    targets = []
    for row in grant.owner_conversation_inventory.filter(eligibility="eligible").order_by("slack_conversation_id"):
        existing = routed.get(row.slack_conversation_id)
        if existing is not None:
            existing.source_inventory = row
            # Merge scheduling metadata in this existing authorized directory
            # pass. A mirrored room retains its transport/device boundary and
            # shares one source cache entry and one provider probe.
            activity = _activity_ts({"latest": {"ts": row.source_activity_ts}})
            previous = _activity_ts({"latest": {"ts": existing.source_activity_ts}})
            if activity and (not previous or Decimal(activity) > Decimal(previous)):
                existing.source_activity_ts = activity
        if row.slack_conversation_id in seen and not include_routed:
            continue
        target = ReadTarget(
            channel_id=row.slack_conversation_id,
            slack_id=row.slack_conversation_id,
            kind=row.kind,
            conversation=row,
            source_activity_ts=row.source_activity_ts,
            source_inventory=row,
        )
        if target.read_scope not in authority.scopes:
            continue
        history_scope = {
            "mpim": "mpim:history", "private_channel": "groups:history",
            "public_channel": "channels:history",
        }.get(row.kind)
        if history_scope and history_scope not in authority.scopes:
            continue
        seen.add(row.slack_conversation_id)
        row.grant = grant
        targets.append(target)
    return targets


def record_read_activity_locked(grant, connection, authority, target, source_ts):
    """Reuse confirmed source activity to recover an existing stalled mirror."""
    row = target.source_inventory
    if row is None or not has_metadata_consent(connection, authority):
        return
    stamp = _activity_ts({"latest": {"ts": source_ts}})
    previous = _activity_ts({"latest": {"ts": row.source_activity_ts}})
    if not stamp or (previous and Decimal(stamp) <= Decimal(previous)):
        return
    updated = grant.owner_conversation_inventory.filter(
        pk=row.pk, slack_conversation_id=target.slack_id, eligibility="eligible",
        source_activity_ts=row.source_activity_ts,
    ).update(source_activity_ts=stamp)
    if updated:
        row.source_activity_ts = stamp
        from .message_sync.device_recovery import schedule_source_recovery_locked
        schedule_source_recovery_locked(grant, connection, target.slack_id)


def hydrate_source_names(authority, *, limit=4):
    """Resolve a bounded number of IM labels without waiting for history import."""
    if not enabled():
        return
    from integrations.services.slack_dm_mirror import (
        _call_slack_with_grant_authority,
        _lock_slack_grant_api_authority,
        _profile_from_slack_user,
    )

    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"users:read"})
        if not has_metadata_consent(connection, authority):
            return
        pending = list(grant.owner_conversation_inventory.filter(
            kind="im", display_name="",
        ).exclude(counterpart_slack_user_id="").order_by("id")[:limit])
    for row in pending:
        result = _call_slack_with_grant_authority(
            authority, "users_info", required_scopes={"users:read"},
            user=row.counterpart_slack_user_id,
        )
        person = result.get("user") if hasattr(result, "get") else None
        if not isinstance(person, dict) or str(person.get("id") or "") != row.counterpart_slack_user_id:
            continue
        name = _profile_from_slack_user(person).get("display_name") or row.counterpart_slack_user_id
        with transaction.atomic():
            grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"users:read"})
            if not has_metadata_consent(connection, authority):
                return
            updated = grant.owner_conversation_inventory.filter(
                pk=row.pk, kind="im", counterpart_slack_user_id=row.counterpart_slack_user_id,
            ).update(display_name=name[:255])
            if updated:
                state = state_for(connection, authority)
                state["revision"] = int(state["revision"]) + 1
                _save_state(connection, state)
