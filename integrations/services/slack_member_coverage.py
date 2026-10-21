"""Read-only, token-free coverage evidence from persisted owner inventories.

This report is an observation, not authorization to read a channel. In particular,
users.conversations membership can have changed since its recorded sweep.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import uuid

REQUIRED_SCOPES = ("channels:history", "channels:read", "chat:write")
INVENTORY_KEY = "slack_owner_inventory_v1"
CONSENT_KEY = "slack_owner_inventory_metadata_consent_v1"
OAUTH_GENERATION_KEY = "mlai_slack_oauth_generation"


def _scopes(raw):
    valid = isinstance(raw, list) and all(isinstance(item, str) and item.strip() for item in raw)
    return sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()}) if isinstance(raw, list) else [], valid


def _datetime(raw):
    try:
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _generation(raw):
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _sweep_id(raw):
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def build_coverage(*, workspace_id, grants, identity_links, channels, inventory, now, max_age_hours=24):
    """Build deterministic JSON-safe evidence; all inputs are token-free rows."""
    cutoff = now - timedelta(hours=max_age_hours)
    links = {(row["user_id"], row["slack_workspace_id"], row["slack_user_id"])
             for row in identity_links if row.get("revoked_at") is None and row.get("user_id") is not None}
    workspace_grants = [row for row in grants if row["slack_workspace_id"] == workspace_id]
    identities = Counter(row["slack_user_id"] for row in workspace_grants)
    eligible = {}
    members, excluded, missing = [], [], []
    for row in sorted(workspace_grants, key=lambda item: item["id"]):
        scopes, valid_scopes = _scopes(row.get("scopes"))
        absent = sorted(set(REQUIRED_SCOPES) - set(scopes))
        member = {"grant_id": row["id"], "user_id": row["user_id"], "slack_user_id": row["slack_user_id"]}
        reasons = []
        if row.get("status") != "active" or row.get("revoked_at") is not None:
            reasons.append("inactive_grant")
        if not row.get("user_active"):
            reasons.append("inactive_account")
        if (row["user_id"], workspace_id, row["slack_user_id"]) not in links:
            reasons.append("missing_identity_link")
        if (row.get("connection_user_id") != row["user_id"] or row.get("provider") != "slack"
                or row.get("connection_status") not in {"connected", "syncing"}
                or row.get("connection_workspace_id") != workspace_id
                or row.get("connection_slack_user_id") != row["slack_user_id"]):
            reasons.append("connection_identity_or_status")
        if not row["slack_user_id"] or identities[row["slack_user_id"]] != 1:
            reasons.append("duplicate_or_missing_slack_identity")
        if reasons:
            excluded.append({**member, "reasons": reasons})
            continue
        if absent or not valid_scopes:
            missing.append({**member, "missing_scopes": absent, "invalid_scopes": not valid_scopes})
            continue
        identity = {
            "grant_id": row["id"], "connection_id": row["connection_id"],
            "workspace_id": workspace_id, "slack_user_id": row["slack_user_id"],
            "consent_generation": row["consented_at"].isoformat(),
            "consent_version": row["consent_version"],
            "oauth_generation": _generation(row.get("oauth_generation")), "scopes": scopes,
        }
        state = row.get("inventory_state")
        state = state if isinstance(state, dict) else {}
        current = (row.get("inventory_consent") == identity
                   and all(state.get(key) == value for key, value in identity.items()))
        last_public = _datetime(state.get("last_public_at"))
        sweep = str(state.get("public_sweep") or "") if current else ""
        coverage = state.get("coverage")
        coverage = coverage.get("public_channel") if isinstance(coverage, dict) else None
        if not current:
            status = "not_current_or_not_consented"
        elif (sweep and _sweep_id(sweep) is None) or (state.get("public_cursor") and not sweep):
            status = "invalid_sweep"
        elif sweep or state.get("public_cursor"):
            status = "partial"
        elif coverage != "complete":
            status = "permission_required" if coverage == "permission_required" else "pending"
        elif last_public is None or not cutoff <= last_public <= now:
            status = "stale"
        else:
            status = "complete"
        members.append({**member, "required_scopes": list(REQUIRED_SCOPES),
                        "inventory_public_status": status,
                        "inventory_last_completed_at": last_public.isoformat() if last_public else None})
        eligible[row["id"]] = {"slack_user_id": row["slack_user_id"], "current": current,
                                "sweep": sweep, "status": status}

    observations = defaultdict(set)
    excluded_observations = defaultdict(Counter)
    selected = {}
    unexpected = []
    for row in sorted(channels, key=lambda item: item["slack_channel_id"]):
        if (row["slack_workspace_id"] != workspace_id or not row.get("enabled")
                or row.get("destination_platform") != "buzz"):
            continue
        if (row.get("lifecycle_source") or row.get("slack_visibility") != "public"
                or row.get("mirror_decision") != "mirror" or not row.get("destination_channel_id")):
            unexpected.append({"slack_channel_id": row["slack_channel_id"],
                               "lifecycle_source": row.get("lifecycle_source", ""),
                               "slack_visibility": row.get("slack_visibility", ""),
                               "mirror_decision": row.get("mirror_decision", ""),
                               "has_destination": bool(row.get("destination_channel_id"))})
            continue
        selected[row["slack_channel_id"]] = row
    for row in inventory:
        channel_id = row["slack_conversation_id"]
        if row.get("kind") != "public_channel" or channel_id not in selected:
            continue
        grant = eligible.get(row["grant_id"])
        if grant is None:
            continue
        seen = _datetime(row.get("last_seen_at"))
        if not grant["current"]:
            reason = "not_current_or_not_consented"
        elif grant["status"] not in {"partial", "complete"}:
            reason = grant["status"]
        elif grant["sweep"] and _sweep_id(row.get("last_seen_sweep_id")) != _sweep_id(grant["sweep"]):
            reason = "previous_sweep"
        elif seen is None or not cutoff <= seen <= now:
            reason = "stale_or_invalid_observation"
        elif row.get("eligibility") != "eligible" or row.get("source_archived") is True:
            reason = "ineligible_or_archived"
        else:
            observations[channel_id].add(grant["slack_user_id"])
            continue
        excluded_observations[channel_id][reason] += 1
    return {
        "workspace_id": workspace_id, "generated_at": now.isoformat(),
        "required_scopes": list(REQUIRED_SCOPES), "max_inventory_age_hours": max_age_hours,
        "evidence": "persisted_users.conversations_observations_not_live_membership_or_reader_authorization",
        "snapshot_consistency": "independent_selects; inventory_and_revocations_may_change_during_report",
        "scope_qualified_linked_member_count": len(members), "members": members,
        "members_missing_scopes": missing, "excluded_grants": excluded,
        "channels": [{"slack_channel_id": channel_id,
                      "destination_channel_id": row["destination_channel_id"],
                      "observed_member_count": len(observations[channel_id]),
                      "excluded_observations": dict(sorted(excluded_observations[channel_id].items()))}
                     for channel_id, row in selected.items()],
        "unexpected_enabled_mappings": unexpected,
        "inventory_gaps": dict(sorted(Counter(row["inventory_public_status"] for row in members
                                               if row["inventory_public_status"] != "complete").items())),
    }


def load_coverage(*, workspace_id, now, max_age_hours=24):
    """Only SELECT explicit non-credential fields; no Slack client or writes."""
    from integrations.models import (
        CommunityBridgeChannel, CommunityBridgeIdentityLink,
        SlackDmMirrorGrant, SlackOwnerConversationInventory,
    )
    aliases = {
        "id": "id", "user_id": "user_id", "slack_workspace_id": "slack_workspace_id",
        "slack_user_id": "slack_user_id", "status": "status", "revoked_at": "revoked_at",
        "consent_version": "consent_version", "consented_at": "consented_at", "connection_id": "connection_id",
        "user_active": "user__is_active", "connection_user_id": "connection__user_id",
        "provider": "connection__provider", "connection_status": "connection__status", "scopes": "connection__scopes",
        "external_account_id": "connection__external_account_id",
        "team_id": "connection__provider_metadata__team__id",
        "connection_slack_user_id": "connection__provider_metadata__authed_user__id",
        "oauth_generation": f"connection__provider_metadata__{OAUTH_GENERATION_KEY}",
        "inventory_consent": f"connection__sync_cursor__{CONSENT_KEY}",
        "inventory_state": f"connection__sync_cursor__{INVENTORY_KEY}",
    }
    grants = []
    for source in SlackDmMirrorGrant.objects.filter(slack_workspace_id=workspace_id).order_by("id").values(*aliases.values()):
        row = {key: source[value] for key, value in aliases.items()}
        row["connection_workspace_id"] = row.pop("team_id") or row.pop("external_account_id")
        grants.append(row)
    return build_coverage(
        workspace_id=workspace_id, now=now, max_age_hours=max_age_hours, grants=grants,
        identity_links=CommunityBridgeIdentityLink.objects.filter(slack_workspace_id=workspace_id).values(
            "user_id", "slack_workspace_id", "slack_user_id", "revoked_at"),
        channels=CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace_id, enabled=True,
                                                       destination_platform="buzz").values(
            "slack_workspace_id", "slack_channel_id", "enabled", "destination_platform", "destination_channel_id",
            "lifecycle_source", "slack_visibility", "mirror_decision"),
        inventory=SlackOwnerConversationInventory.objects.filter(grant__slack_workspace_id=workspace_id).values(
            "grant_id", "slack_conversation_id", "kind", "eligibility", "source_archived", "last_seen_at", "last_seen_sweep_id"),
    )
