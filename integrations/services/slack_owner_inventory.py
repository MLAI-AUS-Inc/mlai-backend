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
        "public_sweep": "",
        "public_cursor": "",
        "last_full_sweep_at": None,
    }


def state_for(connection, authority) -> dict:
    raw = (connection.sync_cursor or {}).get(KEY)
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in _identity(authority).items()):
        return _new_state(authority)
    return raw


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
    state.update(private_sweep="", public_sweep="", public_cursor="", last_full_sweep_at=None)
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


def collect_public_page(authority) -> None:
    """Fetch at most one owner-membership page; never changes bridge mappings."""
    if not enabled():
        return
    from integrations.services.slack_dm_mirror import (
        _call_slack_with_grant_authority,
        _lock_slack_grant_api_authority,
    )

    with transaction.atomic():
        _, connection = _lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if not has_metadata_consent(connection, authority):
            return
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
            return
        last_public = state.get("last_public_at")
        if state["coverage"].get("public_channel") == "complete" and last_public:
            try:
                if timezone.now() - timezone.datetime.fromisoformat(last_public) < timedelta(minutes=5):
                    return
            except (ValueError, TypeError):
                pass
        cursor = str(state.get("public_cursor") or "")
        sweep_id = state.get("public_sweep") or uuid.uuid4().hex
    response = _call_slack_with_grant_authority(
        authority, "users_conversations", required_scopes={"channels:read"},
        types="public_channel", exclude_archived=False, limit=20, cursor=cursor,
    )
    rows = response.get("channels")
    if not isinstance(rows, list):
        raise ValueError("Slack returned a malformed public conversation page.")
    next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
    if next_cursor and next_cursor == cursor:
        raise ValueError("Slack public inventory pagination made no progress.")
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(authority, required_scopes={"channels:read"})
        if not has_metadata_consent(connection, authority):
            return
        state = state_for(connection, authority)
        if str(state.get("public_cursor") or "") != cursor:
            return
        _upsert_rows(grant, rows, uuid.UUID(sweep_id), expected_kinds={"public_channel"})
        state.update(public_sweep=sweep_id, public_cursor=next_cursor, revision=int(state["revision"]) + 1)
        if not next_cursor:
            SlackOwnerConversationInventory.objects.filter(grant=grant, kind="public_channel").exclude(
                last_seen_sweep_id=uuid.UUID(sweep_id),
            ).delete()
            coverage = dict(state["coverage"])
            coverage["public_channel"] = "complete"
            state["coverage"] = coverage
            state["public_sweep"] = ""
            state["last_public_at"] = timezone.now().isoformat()
            if all(value == "complete" for value in coverage.values()):
                state["last_full_sweep_at"] = timezone.now().isoformat()
        _save_state(connection, state)


def source_read_targets(grant, authority, existing_targets):
    """Add consented source conversations to the account's unread sweep.

    Targets already represented by a routed room share the same Slack cache key,
    so the worker never spends quota reading a source conversation twice.
    """
    if not enabled() or not has_metadata_consent(grant.connection, authority):
        return []
    from integrations.services.slack_chat_read_state import ReadTarget

    seen = {target.slack_id for target in existing_targets}
    targets = []
    for row in grant.owner_conversation_inventory.filter(eligibility="eligible").order_by("slack_conversation_id"):
        if row.slack_conversation_id in seen:
            continue
        target = ReadTarget(
            channel_id=row.slack_conversation_id,
            slack_id=row.slack_conversation_id,
            kind=row.kind,
            conversation=row,
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
