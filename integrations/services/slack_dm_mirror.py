"""Owner-consented Slack direct-message migration and live mirroring.

DM content is deliberately kept out of the public community-bridge receipt,
message-link, analytics, and organization-memory tables. Queue bodies use the
same encrypted-at-rest field as OAuth credentials and are erased on completion.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from coincurve import PrivateKey
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.community_bridge.buzz import BuzzBridgeClient
from integrations.services.community_bridge.formatting import (
    emoji_to_slack_reaction,
    normalize_slack_files,
    reaction_object_id,
    sanitize_slack_text,
    slack_reaction_to_emoji,
)

logger = logging.getLogger(__name__)
DIRECT_DM_SCOPES = {
    "im:read",
    "im:history",
    "im:write",
    "chat:write",
    "users:read",
    "reactions:read",
    "reactions:write",
    "files:read",
}
GROUP_DM_SCOPES = {"mpim:read", "mpim:history", "mpim:write"}
REQUIRED_SCOPES = DIRECT_DM_SCOPES | GROUP_DM_SCOPES
_last_grant_discovery_scan = 0.0
_history_scan_available_at = 0.0
GRANT_DISCOVERY_INTERVAL_SECONDS = 300
HISTORY_STATE_PREFIX = "history-state:"
HISTORY_MAIN_STATE_ID = f"{HISTORY_STATE_PREFIX}main"
SLACK_ECHO_WINDOW_SECONDS = 300


class SlackDmMirrorError(RuntimeError):
    """Raised when a Slack DM grant cannot be activated safely."""

    code = "slack_dm_mirror_error"


class SlackDmMirrorAuthorizationError(SlackDmMirrorError):
    """Raised when a queued private operation no longer has owner authority."""

    permanent = True


def slack_connection_for_user(user) -> ExternalServiceConnection | None:
    return (
        ExternalServiceConnection.objects.filter(
            user=user,
            provider=ExternalServiceProvider.SLACK,
            status__in=(
                ExternalServiceConnectionStatus.CONNECTED,
                ExternalServiceConnectionStatus.SYNCING,
            ),
        )
        .order_by("-updated_at")
        .first()
    )


def active_grant_for_user(user) -> SlackDmMirrorGrant:
    grant = (
        SlackDmMirrorGrant.objects.select_related("connection", "user")
        .filter(user=user)
        .order_by("-updated_at")
        .first()
    )
    if (
        grant is None
        or grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
    ):
        raise SlackDmMirrorError("Connect or resume Slack DM mirroring first.")
    if not DIRECT_DM_SCOPES.issubset(set(grant.connection.scopes or [])):
        raise SlackDmMirrorError("Re-authorize Slack to use direct messages.")
    return grant


def search_slack_users(
    grant: SlackDmMirrorGrant,
    *,
    query: str = "",
    limit: int = 20,
    cursor: str = "",
) -> dict[str, Any]:
    """Search the owner's internal Slack human directory without emails."""

    query_text = str(query or "").strip().casefold()
    if len(query_text) > 100:
        raise SlackDmMirrorError("Slack user search is limited to 100 characters.")
    result_limit = max(1, min(int(limit), 50))
    slack_cursor, offset = _decode_directory_cursor(cursor)
    client = WebClient(token=grant.connection.access_token)
    users: list[dict[str, str]] = []
    next_cursor = ""
    pages = 0
    while len(users) < result_limit and pages < 20:
        page_cursor = slack_cursor
        response = client.users_list(limit=200, cursor=page_cursor)
        pages += 1
        members = [
            member
            for member in response.get("members") or []
            if isinstance(member, dict)
            and _is_eligible_slack_user(
                member,
                workspace_id=grant.slack_workspace_id,
                owner_slack_user_id=grant.slack_user_id,
            )
        ]
        matches = [
            _serialize_slack_user(member)
            for member in members
            if _slack_user_matches(member, query_text)
        ]
        matches = matches[offset:]
        remaining = result_limit - len(users)
        users.extend(matches[:remaining])
        consumed = min(len(matches), remaining)
        if consumed < len(matches):
            next_cursor = _encode_directory_cursor(page_cursor, offset + consumed)
            break
        slack_cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        offset = 0
        if not slack_cursor:
            break
        if len(users) >= result_limit:
            next_cursor = _encode_directory_cursor(slack_cursor, 0)
            break
    if not next_cursor and slack_cursor:
        next_cursor = _encode_directory_cursor(slack_cursor, 0)
    return {"users": users, "next_cursor": next_cursor}


def open_slack_dm(
    grant: SlackDmMirrorGrant,
    *,
    slack_user_ids: list[str],
    authenticated_public_key: str,
) -> dict[str, Any]:
    """Open one owner-scoped Slack IM/MPIM and provision its private mirror."""

    requested_ids = []
    for value in slack_user_ids:
        slack_user_id = str(value or "").strip()
        if slack_user_id and slack_user_id not in requested_ids:
            requested_ids.append(slack_user_id)
    if not 1 <= len(requested_ids) <= 8:
        raise SlackDmMirrorError("Choose between one and eight Slack users.")
    if grant.slack_user_id in requested_ids:
        raise SlackDmMirrorError("Do not include your own Slack user ID.")
    required_scopes = DIRECT_DM_SCOPES if len(requested_ids) == 1 else GROUP_DM_SCOPES
    if not required_scopes.issubset(set(grant.connection.scopes or [])):
        raise SlackDmMirrorError(
            "Re-authorize Slack to start a group DM."
            if len(requested_ids) > 1
            else "Re-authorize Slack to start a direct message."
        )
    _, identity_repaired, _ = ensure_owner_identity(
        grant,
        authenticated_public_key=authenticated_public_key,
    )
    client = WebClient(token=grant.connection.access_token)
    profile_cache: dict[str, dict[str, str]] = {}
    for slack_user_id in requested_ids:
        response = client.users_info(user=slack_user_id)
        raw_user = (
            response.get("user") if isinstance(response.get("user"), dict) else {}
        )
        if not _is_eligible_slack_user(
            raw_user,
            workspace_id=grant.slack_workspace_id,
            owner_slack_user_id=grant.slack_user_id,
        ):
            raise SlackDmMirrorError(
                "Slack DMs can only be started with internal human users."
            )
        profile_cache[slack_user_id] = _profile_from_slack_user(raw_user)
    open_response = client.conversations_open(
        users=",".join(requested_ids),
        return_im=True,
    )
    raw_channel = (
        open_response.get("channel")
        if isinstance(open_response.get("channel"), dict)
        else {}
    )
    channel_id = str(raw_channel.get("id") or "").strip()
    returned_counterpart = str(raw_channel.get("user") or "").strip()
    returned_members = {
        str(value or "").strip()
        for value in raw_channel.get("members") or []
        if str(value or "").strip()
    }
    expected_members = {grant.slack_user_id, *requested_ids}
    if (
        not channel_id
        or _is_external_shared_conversation(raw_channel)
        or (len(requested_ids) == 1 and not channel_id.startswith("D"))
        or (len(requested_ids) > 1 and not channel_id.startswith("G"))
        or (
            len(requested_ids) == 1
            and returned_counterpart
            and returned_counterpart != requested_ids[0]
        )
        or (returned_members and returned_members != expected_members)
    ):
        raise SlackDmMirrorError("Slack returned an unsupported conversation.")
    profile_cache[grant.slack_user_id] = _slack_profile(
        client,
        grant.slack_user_id,
        profile_cache,
    )
    participant_ids = sorted({grant.slack_user_id, *requested_ids})
    conversation, _ = SlackDmMirrorConversation.objects.update_or_create(
        grant=grant,
        slack_conversation_id=channel_id,
        defaults={
            "slack_workspace_id": grant.slack_workspace_id,
            "participant_slack_ids": participant_ids,
            "participant_profiles": {
                slack_user_id: profile_cache[slack_user_id]
                for slack_user_id in participant_ids
            },
        },
    )
    _provision_owner_conversation(
        conversation,
        reset_history=identity_repaired,
        required_owner_public_key=authenticated_public_key,
    )
    conversation.refresh_from_db()
    active_device_count = CommunityChatDevice.objects.filter(
        user_id=grant.user_id,
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    ).count()
    shadow_count = max(0, len(conversation.participant_slack_ids or []) - 1)
    included_owner_devices = (
        len(conversation.participant_buzz_pubkeys or []) - shadow_count
    )
    participant_profiles = conversation.participant_profiles or {}
    participant_identity_map = conversation.participant_identity_map or {}
    return {
        "slack_conversation_id": conversation.slack_conversation_id,
        "mlai_channel_id": str(conversation.mlai_channel_id or ""),
        "display_name": _conversation_name(conversation),
        "participant_slack_ids": list(conversation.participant_slack_ids or []),
        "participant_pubkeys": list(conversation.participant_buzz_pubkeys or []),
        "participants": [
            {
                "slack_user_id": slack_user_id,
                "buzz_pubkey": str(participant_identity_map.get(slack_user_id) or ""),
                "display_name": str(
                    (participant_profiles.get(slack_user_id) or {}).get("display_name")
                    or slack_user_id
                ),
                "avatar_url": str(
                    (participant_profiles.get(slack_user_id) or {}).get("avatar_url")
                    or ""
                ),
                "is_owner": slack_user_id == grant.slack_user_id,
            }
            for slack_user_id in conversation.participant_slack_ids or []
        ],
        "owner_device_pubkeys": sorted(
            set(conversation.participant_buzz_pubkeys or [])
            - {
                str(pubkey or "")
                for slack_user_id, pubkey in participant_identity_map.items()
                if slack_user_id != grant.slack_user_id
            }
        ),
        "identity_repaired": identity_repaired,
        "history_status": (
            "complete" if conversation.history_backfilled_at is not None else "queued"
        ),
        "device_capacity": {
            "active": active_device_count,
            "included": included_owner_devices,
            "limited": included_owner_devices < active_device_count,
            "authenticated_device_included": (
                str(authenticated_public_key or "").strip().lower()
                in set(conversation.participant_buzz_pubkeys or [])
            ),
        },
    }


def status_payload(
    user, *, authenticated_public_key: str | None = None
) -> dict[str, Any]:
    connection = slack_connection_for_user(user)
    grant = (
        SlackDmMirrorGrant.objects.filter(user=user)
        .select_related("connection")
        .order_by("-updated_at")
        .first()
    )
    conversations = SlackDmMirrorConversation.objects.none()
    if grant is not None:
        conversations = grant.conversations.all()
    counts = {
        key: conversations.filter(status=value).count()
        for key, value in {
            "live": SlackDmMirrorConversationStatus.LIVE,
            "waiting": SlackDmMirrorConversationStatus.AWAITING_SETUP,
            "error": SlackDmMirrorConversationStatus.ERROR,
        }.items()
    }
    active_device_count = CommunityChatDevice.objects.filter(
        user=user,
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    ).count()
    counts["device_capacity_limited"] = sum(
        1
        for participant_ids in conversations.values_list(
            "participant_slack_ids", flat=True
        )
        if max(0, len(participant_ids or []) - 1) + active_device_count > 9
    )
    backfill_deliveries = SlackDmMirrorDelivery.objects.none()
    if grant is not None:
        backfill_deliveries = SlackDmMirrorDelivery.objects.filter(
            conversation__grant=grant,
            source_platform=CommunityBridgePlatform.SLACK,
            metadata__backfill=True,
        )
    incomplete_statuses = (
        CommunityBridgeDeliveryStatus.PENDING,
        CommunityBridgeDeliveryStatus.PROCESSING,
        CommunityBridgeDeliveryStatus.FAILED,
        CommunityBridgeDeliveryStatus.DEAD,
    )
    incomplete_conversation_ids = backfill_deliveries.filter(
        status__in=incomplete_statuses,
    ).values_list("conversation_id", flat=True)
    complete = (
        conversations.filter(history_backfilled_at__isnull=False)
        .exclude(
            id__in=incomplete_conversation_ids,
        )
        .count()
    )
    backfill_counts = {
        "complete": complete,
        "pending": conversations.count() - complete,
        "queued_messages": backfill_deliveries.filter(
            status__in=(
                CommunityBridgeDeliveryStatus.PENDING,
                CommunityBridgeDeliveryStatus.PROCESSING,
            )
        ).count(),
        "failed_messages": backfill_deliveries.filter(
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            )
        ).count(),
    }
    return {
        "connected": connection is not None,
        "needs_reauthorization": bool(
            connection is not None
            and not REQUIRED_SCOPES.issubset(set(connection.scopes or []))
        ),
        "enabled": bool(grant and grant.status == SlackDmMirrorGrantStatus.ACTIVE),
        "status": grant.status if grant else "not_connected",
        "workspace_name": connection.account_label if connection else "",
        "workspace_id": grant.slack_workspace_id if grant else "",
        "slack_user_id": grant.slack_user_id if grant else "",
        "history_days": grant.history_days if grant else _history_days(),
        "consent_version": (
            grant.consent_version if grant else SlackDmMirrorGrant.CONSENT_VERSION
        ),
        "last_discovery_at": grant.last_discovery_at if grant else None,
        "last_synced_at": grant.last_synced_at if grant else None,
        "last_error": grant.last_error if grant else "",
        "identity": _identity_status(
            grant,
            authenticated_public_key=authenticated_public_key,
        ),
        "conversations": counts,
        "backfill": backfill_counts,
        "group_dms_enabled": bool(
            connection is not None
            and GROUP_DM_SCOPES.issubset(set(connection.scopes or []))
        ),
        "privacy": {
            "requires_both_participants": False,
            "owner_controlled": True,
            "included_in_roo": False,
            "included_in_analytics": False,
            "history_is_bounded": not bool(grant and grant.history_days == 0),
            "full_history": bool(grant and grant.history_days == 0),
        },
    }


def activate_connection(connection: ExternalServiceConnection) -> SlackDmMirrorGrant:
    """Record consent, bind the Slack identity, discover IMs, and provision eligible DMs."""

    if connection.provider != ExternalServiceProvider.SLACK:
        raise SlackDmMirrorError("Connection is not a Slack connection.")
    workspace_id, slack_user_id = _connection_identity(connection)
    missing = DIRECT_DM_SCOPES - set(connection.scopes or [])
    if missing:
        raise SlackDmMirrorError(
            f"Slack grant is missing scopes: {', '.join(sorted(missing))}."
        )
    if _preferred_device(connection.user_id) is None:
        raise SlackDmMirrorError("Verify an MLAI Chat device before linking Slack DMs.")
    existing_grant = SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace_id,
        slack_user_id=slack_user_id,
    ).first()
    if existing_grant is not None and existing_grant.user_id != connection.user_id:
        raise SlackDmMirrorError(
            "This Slack identity is already linked to another MLAI account."
        )

    now = timezone.now()
    grant, _ = SlackDmMirrorGrant.objects.update_or_create(
        slack_workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        defaults={
            "user": connection.user,
            "connection": connection,
            "status": SlackDmMirrorGrantStatus.ACTIVE,
            "consent_version": SlackDmMirrorGrant.CONSENT_VERSION,
            "history_days": (
                existing_grant.history_days
                if existing_grant is not None
                else _history_days()
            ),
            "consented_at": now,
            "paused_at": None,
            "revoked_at": None,
            "last_error": "",
        },
    )
    ensure_owner_identity(
        grant,
        allow_preferred_fallback=True,
    )
    # OAuth callbacks must stay fast even for workspaces with hundreds of DMs.
    # The paced worker sees the null discovery marker and provisions them.
    grant.last_discovery_at = None
    grant.save(update_fields=("last_discovery_at", "updated_at"))
    return grant


def pause_grant(grant: SlackDmMirrorGrant) -> None:
    now = timezone.now()
    grant.status = SlackDmMirrorGrantStatus.PAUSED
    grant.paused_at = now
    grant.save(update_fields=("status", "paused_at", "updated_at"))
    grant.conversations.filter(
        status=SlackDmMirrorConversationStatus.LIVE,
    ).update(status=SlackDmMirrorConversationStatus.PAUSED, updated_at=now)


def resume_grant(grant: SlackDmMirrorGrant) -> None:
    connection = grant.connection
    if (
        connection.status
        not in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        or not str(connection.access_token or "").strip()
        or not DIRECT_DM_SCOPES.issubset(set(connection.scopes or []))
    ):
        raise SlackDmMirrorError("Re-authorize Slack before resuming DM mirroring.")
    grant.status = SlackDmMirrorGrantStatus.ACTIVE
    grant.paused_at = None
    grant.revoked_at = None
    grant.last_error = ""
    grant.last_discovery_at = None
    grant.save(
        update_fields=(
            "status",
            "paused_at",
            "revoked_at",
            "last_error",
            "last_discovery_at",
            "updated_at",
        )
    )


def backfill_grant(
    grant: SlackDmMirrorGrant,
    *,
    full_history: bool = False,
) -> int:
    """Mark owner mirrors for a paced, idempotent history re-scan."""

    if grant.status != SlackDmMirrorGrantStatus.ACTIVE or grant.revoked_at is not None:
        raise SlackDmMirrorError("Resume Slack mirroring before starting a backfill.")
    if full_history and grant.history_days != 0:
        grant.history_days = 0
    grant.last_discovery_at = None
    grant.save(update_fields=("history_days", "last_discovery_at", "updated_at"))
    now = timezone.now()
    conversations = grant.conversations.filter(
        status=SlackDmMirrorConversationStatus.LIVE,
    )
    conversation_ids = list(conversations.values_list("id", flat=True))
    _clear_history_scan_states(conversation_ids)
    conversations.update(
        history_backfilled_at=None,
        oldest_synced_ts="",
        last_error="",
        updated_at=now,
    )
    SlackDmMirrorDelivery.objects.filter(
        conversation_id__in=conversation_ids,
        source_platform=CommunityBridgePlatform.SLACK,
        status__in=(
            CommunityBridgeDeliveryStatus.FAILED,
            CommunityBridgeDeliveryStatus.DEAD,
        ),
    ).update(available_at=now, updated_at=now)
    return len(conversation_ids)


def revoke_grant(grant: SlackDmMirrorGrant) -> None:
    now = timezone.now()
    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .select_related("connection")
            .get(pk=grant.pk)
        )
        connection = grant.connection
        access_token = str(connection.access_token or "").strip()
        channel_ids = list(
            grant.conversations.exclude(mlai_channel_id__isnull=True)
            .order_by("mlai_channel_id")
            .values_list("mlai_channel_id", flat=True)
        )
        grant.status = SlackDmMirrorGrantStatus.REVOKED
        grant.revoked_at = now
        grant.last_error = ""
        grant.save(
            update_fields=("status", "revoked_at", "last_error", "updated_at")
        )
        CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=grant.slack_workspace_id,
            slack_user_id=grant.slack_user_id,
            revoked_at__isnull=True,
        ).update(
            revoked_at=now,
            revocation_reason="Slack DM mirroring disconnected",
        )
        conversation_ids = list(grant.conversations.values_list("id", flat=True))
        grant.conversations.update(
            status=SlackDmMirrorConversationStatus.PAUSED,
            updated_at=now,
        )
        private_deliveries = SlackDmMirrorDelivery.objects.filter(
            conversation_id__in=conversation_ids,
        )
        private_deliveries.update(encrypted_text="", updated_at=now)
        private_deliveries.filter(
            status__in=(
                CommunityBridgeDeliveryStatus.PENDING,
                CommunityBridgeDeliveryStatus.PROCESSING,
                CommunityBridgeDeliveryStatus.FAILED,
            ),
        ).update(
            status=CommunityBridgeDeliveryStatus.DEAD,
            last_error="Consent revoked",
            updated_at=now,
        )
        connection.status = ExternalServiceConnectionStatus.DISCONNECTED
        connection.access_token = ""
        connection.refresh_token = ""
        connection.last_error = ""
        connection.provider_metadata = {}
        connection.sync_cursor = {}
        connection.save(
            update_fields=(
                "status",
                "access_token",
                "refresh_token",
                "last_error",
                "provider_metadata",
                "sync_cursor",
                "updated_at",
            )
        )

    if access_token:
        try:
            WebClient(token=access_token).auth_revoke()
        except Exception as exc:
            # Local revocation is the privacy boundary. Slack may already have
            # revoked the token, so a remote error must not retain local access.
            logger.warning(
                "slack_dm_mirror_remote_revoke_failed connection_id=%s error=%s",
                connection.pk,
                exc.__class__.__name__,
            )

    try:
        for channel_id in channel_ids:
            BuzzBridgeClient.unregister_private_conversation(str(channel_id))
    except Exception as exc:
        # Local authority and credentials are already revoked, but the request
        # must fail until the adapter's durable registration is also gone. A
        # retry is safe because adapter unregistration is idempotent.
        SlackDmMirrorGrant.objects.filter(pk=grant.pk).update(
            last_error="MLAI Chat private registration revocation is pending",
            updated_at=timezone.now(),
        )
        logger.warning(
            "slack_dm_mirror_adapter_unregister_failed grant_id=%s error=%s",
            grant.pk,
            exc.__class__.__name__,
        )
        raise


def discover_conversations(
    grant: SlackDmMirrorGrant, *, force_backfill: bool = False
) -> int:
    """Discover and provision every direct or group DM visible to the owner."""

    _, identity_repaired, _ = ensure_owner_identity(
        grant,
        allow_preferred_fallback=True,
    )
    client = WebClient(token=grant.connection.access_token)
    include_group_dms = GROUP_DM_SCOPES.issubset(set(grant.connection.scopes or []))
    conversation_types = "im,mpim" if include_group_dms else "im"
    cursor = ""
    discovered = 0
    failures: list[str] = []
    profile_cache: dict[str, dict[str, str]] = {}
    for stored_profiles in grant.conversations.values_list(
        "participant_profiles", flat=True
    ):
        if not isinstance(stored_profiles, dict):
            continue
        for slack_user_id, profile in stored_profiles.items():
            if isinstance(profile, dict):
                profile_cache.setdefault(str(slack_user_id), profile)
    while True:
        response = client.conversations_list(
            types=conversation_types,
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        for raw in response.get("channels") or []:
            if not isinstance(raw, dict):
                continue
            if _is_external_shared_conversation(raw):
                SlackDmMirrorConversation.objects.filter(
                    grant=grant,
                    slack_conversation_id=str(raw.get("id") or "").strip(),
                ).update(
                    status=SlackDmMirrorConversationStatus.PAUSED,
                    last_error="Slack Connect conversations are not eligible",
                    updated_at=timezone.now(),
                )
                continue
            channel_id = str(raw.get("id") or "").strip()
            conversation = None
            try:
                conversation = _discover_conversation(
                    grant,
                    client,
                    raw,
                    profile_cache=profile_cache,
                    force_backfill=force_backfill,
                    reset_history=identity_repaired,
                )
                if conversation is not None:
                    discovered += 1
            except Exception as exc:
                error_text = f"{exc.__class__.__name__}: {exc}"[:2000]
                failures.append(f"{channel_id or 'unknown'}: {error_text}")
                if conversation is None and channel_id:
                    conversation = SlackDmMirrorConversation.objects.filter(
                        grant=grant,
                        slack_conversation_id=channel_id,
                    ).first()
                if conversation is not None:
                    conversation.last_error = error_text
                    # A transient refresh failure must not take an already-live
                    # DM offline; queued delivery retries can still succeed.
                    if conversation.status != SlackDmMirrorConversationStatus.LIVE:
                        conversation.status = SlackDmMirrorConversationStatus.ERROR
                    conversation.save(
                        update_fields=("status", "last_error", "updated_at")
                    )
                logger.warning(
                    "slack_dm_mirror_conversation_discovery_failed "
                    "grant_id=%s conversation_id=%s error=%s",
                    grant.pk,
                    channel_id,
                    exc,
                )
        cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if not cursor:
            break
    grant.last_discovery_at = timezone.now()
    grant.last_error = "; ".join(failures)[:2000]
    grant.save(update_fields=("last_discovery_at", "last_error", "updated_at"))
    return discovered


def _discover_conversation(
    grant: SlackDmMirrorGrant,
    client: WebClient,
    raw: dict[str, Any],
    *,
    profile_cache: dict[str, dict[str, str]],
    force_backfill: bool,
    reset_history: bool,
) -> SlackDmMirrorConversation | None:
    channel_id = str(raw.get("id") or "").strip()
    if not channel_id:
        return None
    participant_ids = _conversation_participant_ids(
        client,
        raw,
        owner_slack_user_id=grant.slack_user_id,
    )
    if not participant_ids:
        return None
    conversation, _ = SlackDmMirrorConversation.objects.update_or_create(
        grant=grant,
        slack_conversation_id=channel_id,
        defaults={
            "slack_workspace_id": grant.slack_workspace_id,
            "participant_slack_ids": participant_ids,
        },
    )
    conversation.participant_profiles = {
        slack_user_id: _slack_profile(client, slack_user_id, profile_cache)
        for slack_user_id in participant_ids
    }
    conversation.save(update_fields=("participant_profiles", "updated_at"))
    _provision_owner_conversation(
        conversation,
        force_backfill=force_backfill,
        reset_history=reset_history,
    )
    return conversation


def _normalize_private_slack_event(
    payload: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "message").strip()
    if event_type in {"reaction_added", "reaction_removed"}:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        target_message_id = str(item.get("ts") or "").strip()
        author_id = str(event.get("user") or "").strip()
        reaction = str(event.get("reaction") or "").strip().lower()
        emoji = slack_reaction_to_emoji(reaction)
        if (
            str(item.get("type") or "").strip() != "message"
            or not target_message_id
            or not author_id
            or not emoji
        ):
            return None
        try:
            _slack_ts_sort_key(target_message_id)
        except SlackDmMirrorError:
            return None
        operation = (
            CommunityBridgeDeliveryType.REACTION_ADD
            if event_type == "reaction_added"
            else CommunityBridgeDeliveryType.REACTION_REMOVE
        )
        event_timestamp = str(event.get("event_ts") or target_message_id).strip()
        semantic_id = reaction_object_id(
            message_id=target_message_id,
            reaction=reaction,
            author_id=author_id,
        )
        return {
            "operation": operation,
            "source_message_id": _slack_delivery_source_id(
                payload,
                operation=operation,
                target_message_id=target_message_id,
                event_timestamp=event_timestamp,
                author_id=author_id,
                reaction=reaction,
            ),
            "source_author_id": author_id,
            "text": emoji
            if operation == CommunityBridgeDeliveryType.REACTION_ADD
            else "",
            "metadata": {
                "event_ts": event_timestamp,
                "target_source_message_id": target_message_id,
                "reaction_object_id": semantic_id,
                "slack_reaction": reaction,
            },
            "slack_target_ts": target_message_id,
            "slack_reaction": reaction,
            "slack_text": "",
        }
    if event_type != "message" or event.get("bot_id"):
        return None

    subtype = str(event.get("subtype") or "").strip()
    if subtype in {"", "thread_broadcast"}:
        source_message_id = str(event.get("ts") or "").strip()
        author_id = str(event.get("user") or "").strip()
        if not source_message_id or not author_id:
            return None
        try:
            _slack_ts_sort_key(source_message_id)
        except SlackDmMirrorError:
            return None
        text = _slack_message_text(event)
        return {
            "operation": CommunityBridgeDeliveryType.CREATE,
            "source_message_id": source_message_id,
            "source_author_id": author_id,
            "text": text,
            "metadata": {
                "event_ts": source_message_id,
                "thread_ts": str(event.get("thread_ts") or "").strip(),
            },
            "slack_target_ts": source_message_id,
            "slack_reaction": "",
            "slack_text": text,
            "client_msg_id": str(event.get("client_msg_id") or "").strip(),
        }
    if subtype == "message_changed":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if message.get("bot_id"):
            return None
        target_message_id = str(message.get("ts") or "").strip()
        author_id = str(message.get("user") or "").strip()
        event_timestamp = str(
            event.get("event_ts")
            or (
                message.get("edited", {}).get("ts")
                if isinstance(message.get("edited"), dict)
                else ""
            )
            or target_message_id
        ).strip()
        if not target_message_id or not author_id:
            return None
        try:
            _slack_ts_sort_key(target_message_id)
        except SlackDmMirrorError:
            return None
        text = _slack_message_text(message)
        return {
            "operation": CommunityBridgeDeliveryType.EDIT,
            "source_message_id": _slack_delivery_source_id(
                payload,
                operation=CommunityBridgeDeliveryType.EDIT,
                target_message_id=target_message_id,
                event_timestamp=event_timestamp,
                author_id=author_id,
                text=text,
            ),
            "source_author_id": author_id,
            "text": text,
            "metadata": {
                "event_ts": event_timestamp,
                "target_source_message_id": target_message_id,
                "thread_ts": str(message.get("thread_ts") or "").strip(),
            },
            "slack_target_ts": target_message_id,
            "slack_reaction": "",
            "slack_text": text,
        }
    if subtype == "message_deleted":
        previous = (
            event.get("previous_message")
            if isinstance(event.get("previous_message"), dict)
            else {}
        )
        if previous.get("bot_id"):
            return None
        target_message_id = str(
            event.get("deleted_ts") or previous.get("ts") or ""
        ).strip()
        author_id = str(previous.get("user") or "").strip()
        event_timestamp = str(event.get("event_ts") or target_message_id).strip()
        if not target_message_id or not author_id:
            return None
        try:
            _slack_ts_sort_key(target_message_id)
        except SlackDmMirrorError:
            return None
        return {
            "operation": CommunityBridgeDeliveryType.DELETE,
            "source_message_id": _slack_delivery_source_id(
                payload,
                operation=CommunityBridgeDeliveryType.DELETE,
                target_message_id=target_message_id,
                event_timestamp=event_timestamp,
                author_id=author_id,
            ),
            "source_author_id": author_id,
            "text": "",
            "metadata": {
                "event_ts": event_timestamp,
                "target_source_message_id": target_message_id,
                "thread_ts": str(previous.get("thread_ts") or "").strip(),
            },
            "slack_target_ts": target_message_id,
            "slack_reaction": "",
            "slack_text": "",
        }
    return None


def _slack_delivery_source_id(
    payload: dict[str, Any],
    *,
    operation: str,
    target_message_id: str,
    event_timestamp: str,
    author_id: str,
    reaction: str = "",
    text: str = "",
) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    if event_id and len(event_id) <= 100:
        return event_id
    material = "\0".join(
        (
            operation,
            target_message_id,
            event_timestamp,
            author_id,
            reaction,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    ).encode("utf-8")
    return f"slack-event:{hashlib.sha256(material).hexdigest()}"


def _buzz_delivery_source_id(
    payload: dict[str, Any],
    *,
    operation: str,
    source_message_id: str,
) -> str:
    if operation == CommunityBridgeDeliveryType.CREATE:
        return source_message_id
    receipt_key = str(payload.get("receipt_key") or "").strip()
    if not receipt_key:
        return source_message_id
    return f"buzz-event:{hashlib.sha256(receipt_key.encode('utf-8')).hexdigest()}"


def _slack_message_text(message: dict[str, Any]) -> str:
    attachments = list(normalize_slack_files(message.get("files") or []))
    for item in message.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        url = str(
            item.get("title_link")
            or item.get("from_url")
            or item.get("original_url")
            or item.get("image_url")
            or item.get("thumb_url")
            or ""
        ).strip()
        if not url.startswith(("https://", "http://")):
            continue
        attachments.append(
            {
                "title": str(item.get("title") or item.get("fallback") or url).strip(),
                "url": url,
            }
        )
    return _append_attachment_links(
        sanitize_slack_text(str(message.get("text") or "")),
        attachments,
    )


def _append_attachment_links(text: str, attachments: Any) -> str:
    body = str(text or "")
    rendered: list[str] = []
    seen: set[str] = set()
    for item in attachments if isinstance(attachments, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        url = _safe_attachment_url(item.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join(str(item.get("title") or url).split())[:255]
        rendered.append(f"{title}: <{url}>")
    if not rendered:
        return body
    return "\n\n".join(part for part in (body, "\n".join(rendered)) if part)


def _safe_attachment_url(value: Any) -> str:
    url = str(value or "").strip()
    if (
        not url
        or len(url) > 2048
        or any(character.isspace() or ord(character) < 32 for character in url)
        or any(character in '<>"' for character in url)
    ):
        return ""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        return ""
    return url


def _slack_echo_key(
    *,
    operation: str,
    target_message_id: str,
    author_id: str,
    reaction: str = "",
    text: str = "",
) -> str:
    material = "\0".join(
        (
            operation,
            target_message_id,
            author_id,
            reaction,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _is_slack_echo(
    conversation: SlackDmMirrorConversation,
    normalized: dict[str, Any],
) -> bool:
    operation = normalized["operation"]
    if operation == CommunityBridgeDeliveryType.CREATE:
        message_id = normalized["slack_target_ts"]
        client_message_id = str(normalized.get("client_msg_id") or "").strip()
        query = SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
        )
        if query.filter(
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__slack_ts=message_id,
        ).exists():
            return True
        return bool(
            client_message_id
            and query.filter(
                status__in=(
                    CommunityBridgeDeliveryStatus.PROCESSING,
                    CommunityBridgeDeliveryStatus.COMPLETED,
                ),
                metadata__client_msg_id=client_message_id,
            ).exists()
        )
    echo_key = _slack_echo_key(
        operation=operation,
        target_message_id=normalized["slack_target_ts"],
        author_id=normalized["source_author_id"],
        reaction=normalized["slack_reaction"],
        text=normalized["slack_text"],
    )
    return SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        status__in=(
            CommunityBridgeDeliveryStatus.PROCESSING,
            CommunityBridgeDeliveryStatus.COMPLETED,
        ),
        metadata__slack_echo_key=echo_key,
        updated_at__gte=timezone.now() - timedelta(seconds=SLACK_ECHO_WINDOW_SECONDS),
    ).exists()


def ingest_slack_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_type = str(event.get("type") or "message").strip()
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    raw_channel_id = (
        item.get("channel")
        if event_type.startswith("reaction_")
        else event.get("channel")
    )
    channel_id = str(raw_channel_id or "").strip()
    workspace_id = str(payload.get("team_id") or "").strip()
    if any(
        bool(event.get(flag))
        for flag in ("is_ext_shared_channel", "is_external_shared", "is_shared")
    ):
        SlackDmMirrorConversation.objects.filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
        ).update(
            status=SlackDmMirrorConversationStatus.PAUSED,
            last_error="Slack Connect conversations are not eligible",
            updated_at=timezone.now(),
        )
        return {"status": "ignored"}
    known_group_dm = bool(
        channel_id.startswith("G")
        and SlackDmMirrorConversation.objects.filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
        ).exists()
    )
    is_group_dm_event = (
        channel_id.startswith("G")
        and str(event.get("channel_type") or "").strip().lower() == "mpim"
    )
    if not channel_id.startswith("D") and not known_group_dm and not is_group_dm_event:
        return None
    normalized = _normalize_private_slack_event(payload, event)
    if normalized is None:
        return {"status": "ignored"}
    conversations = list(
        SlackDmMirrorConversation.objects.select_related("grant").filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
            grant__status=SlackDmMirrorGrantStatus.ACTIVE,
            grant__revoked_at__isnull=True,
        )
    )
    if not conversations:
        # Keep the Events API response fast. Mark active grants for expedited
        # worker discovery; conversations.history will recover this event once
        # the new owner mirror is provisioned.
        SlackDmMirrorGrant.objects.filter(
            slack_workspace_id=workspace_id,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        ).update(last_discovery_at=None, updated_at=timezone.now())
        return {"status": "discovery_queued"}
    source_message_id = normalized["source_message_id"]
    author_id = normalized["source_author_id"]
    operation = normalized["operation"]
    metadata = normalized["metadata"]
    text = normalized["text"]
    enqueued: list[str] = []
    duplicates = 0
    echoes = 0
    for conversation in conversations:
        if author_id not in set(conversation.participant_slack_ids or []):
            continue
        if _is_slack_echo(conversation, normalized):
            echoes += 1
            continue
        delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=source_message_id,
            operation=operation,
            defaults={
                "source_author_id": author_id,
                "encrypted_text": text,
                "metadata": {
                    **metadata,
                    "participant_hash": conversation.participant_hash,
                },
                "available_at": timezone.now(),
            },
        )
        if created:
            enqueued.append(str(delivery.pk))
        elif (
            delivery.status
            in (
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            )
            and "identity changed" not in delivery.last_error.casefold()
        ):
            delivery.source_author_id = author_id
            delivery.encrypted_text = text
            delivery.metadata = {
                **metadata,
                "participant_hash": conversation.participant_hash,
            }
            delivery.status = CommunityBridgeDeliveryStatus.PENDING
            delivery.attempts = 0
            delivery.available_at = timezone.now()
            delivery.completed_at = None
            delivery.last_error = ""
            delivery.save(
                update_fields=(
                    "source_author_id",
                    "encrypted_text",
                    "metadata",
                    "status",
                    "attempts",
                    "available_at",
                    "completed_at",
                    "last_error",
                    "updated_at",
                )
            )
            enqueued.append(str(delivery.pk))
        else:
            duplicates += 1
    if enqueued:
        return {"status": "enqueued", "delivery_ids": enqueued}
    if echoes:
        return {"status": "echo_ignored", "count": echoes}
    return {"status": "duplicate" if duplicates else "ignored", "count": duplicates}


def ingest_mlai_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Queue one owner-authored MLAI private operation for Slack."""

    channel_id = str(payload.get("source_channel_id") or "").strip()
    conversation = SlackDmMirrorConversation.objects.filter(
        mlai_channel_id=channel_id,
        status=SlackDmMirrorConversationStatus.LIVE,
    ).first()
    if conversation is None:
        return None
    normalized = payload.get("normalized_event")
    if not isinstance(normalized, dict):
        return {"status": "ignored"}
    operation = str(normalized.get("delivery_type") or "").strip()
    if operation not in {
        CommunityBridgeDeliveryType.CREATE,
        CommunityBridgeDeliveryType.EDIT,
        CommunityBridgeDeliveryType.DELETE,
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    }:
        return {"status": "ignored"}
    message_id = str(normalized.get("source_message_id") or "").strip().lower()
    parent_message_id = (
        str(normalized.get("source_parent_message_id") or "").strip().lower()
    )
    author_pubkey = str(normalized.get("source_author_id") or "").strip().lower()
    owner_device_pubkeys = _conversation_owner_device_pubkeys(conversation)
    if (
        not message_id
        or len(message_id) > 100
        or len(parent_message_id) > 100
        or author_pubkey not in owner_device_pubkeys
        or _active_verified_device(conversation.grant.user_id, author_pubkey) is None
    ):
        return {"status": "ignored"}
    raw_text = str(normalized.get("text") or "")
    if operation == CommunityBridgeDeliveryType.DELETE:
        raw_text = ""
    if operation in {
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    } and (not parent_message_id or not emoji_to_slack_reaction(raw_text)):
        return {"status": "ignored"}
    if operation == CommunityBridgeDeliveryType.REACTION_REMOVE:
        # Keep the Slack reaction name in metadata while satisfying the private
        # adapter's content-free delete contract on the opposite direction.
        queued_text = raw_text
    else:
        queued_text = _append_attachment_links(
            raw_text,
            normalized.get("attachments") or [],
        )
    grant = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(
            pk=conversation.grant_id,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .first()
    )
    if grant is None:
        return {"status": "ignored"}
    delivery_source_id = _buzz_delivery_source_id(
        payload,
        operation=operation,
        source_message_id=message_id,
    )
    target_source_message_id = ""
    if operation in {
        CommunityBridgeDeliveryType.EDIT,
        CommunityBridgeDeliveryType.DELETE,
    }:
        target_source_message_id = message_id
    elif operation in {
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    }:
        target_source_message_id = parent_message_id
    delivery_metadata = {
        "source_event_id": message_id,
        "source_parent_message_id": parent_message_id,
        "target_source_message_id": target_source_message_id,
        "reaction_object_id": (
            message_id
            if operation
            in {
                CommunityBridgeDeliveryType.REACTION_ADD,
                CommunityBridgeDeliveryType.REACTION_REMOVE,
            }
            else ""
        ),
    }
    delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        source_message_id=delivery_source_id,
        operation=operation,
        defaults={
            "source_author_id": author_pubkey,
            "encrypted_text": queued_text,
            "metadata": delivery_metadata,
            "available_at": timezone.now(),
        },
    )
    if not created and delivery.status == CommunityBridgeDeliveryStatus.COMPLETED:
        return {"status": "duplicate"}
    if not created and delivery.status in (
        CommunityBridgeDeliveryStatus.FAILED,
        CommunityBridgeDeliveryStatus.DEAD,
    ):
        delivery.source_author_id = author_pubkey
        delivery.encrypted_text = queued_text
        delivery.metadata = delivery_metadata
        delivery.status = CommunityBridgeDeliveryStatus.PENDING
        delivery.attempts = 0
        delivery.available_at = timezone.now()
        delivery.completed_at = None
        delivery.last_error = ""
        delivery.save(
            update_fields=(
                "source_author_id",
                "encrypted_text",
                "metadata",
                "status",
                "attempts",
                "available_at",
                "completed_at",
                "last_error",
                "updated_at",
            )
        )
    return {
        "status": "enqueued" if created else "queued",
        "delivery_id": str(delivery.pk),
    }


def _conversation_owner_device_pubkeys(
    conversation: SlackDmMirrorConversation,
) -> set[str]:
    identity_map = conversation.participant_identity_map or {}
    shadow_pubkeys = {
        str(public_key or "").lower()
        for slack_user_id, public_key in identity_map.items()
        if slack_user_id != conversation.grant.slack_user_id
    }
    return {
        str(public_key or "").lower()
        for public_key in conversation.participant_buzz_pubkeys or []
    } - shadow_pubkeys


def process_ready_deliveries(limit: int = 20) -> int:
    now = timezone.now()
    SlackDmMirrorDelivery.objects.filter(
        status=CommunityBridgeDeliveryStatus.PROCESSING,
        updated_at__lt=now - timedelta(minutes=5),
    ).update(
        status=CommunityBridgeDeliveryStatus.PENDING,
        available_at=now,
        last_error="Recovered an interrupted private delivery",
        updated_at=now,
    )
    processed = 0
    delivery_limit = max(1, min(int(limit), 100))
    for _ in range(delivery_limit):
        with transaction.atomic():
            delivery = (
                SlackDmMirrorDelivery.objects.select_for_update(skip_locked=True)
                .select_related("conversation__grant__connection")
                .filter(
                    status=CommunityBridgeDeliveryStatus.PENDING,
                    available_at__lte=timezone.now(),
                    conversation__status=SlackDmMirrorConversationStatus.LIVE,
                    conversation__grant__status=SlackDmMirrorGrantStatus.ACTIVE,
                    conversation__grant__revoked_at__isnull=True,
                )
                .order_by("available_at", "id")
                .first()
            )
            if delivery is None:
                break
            delivery.status = CommunityBridgeDeliveryStatus.PROCESSING
            delivery.save(update_fields=("status", "updated_at"))
        try:
            _deliver_private(delivery)
        except Exception as exc:  # worker boundary; retry with bounded backoff
            delivery.attempts += 1
            permanent = bool(getattr(exc, "permanent", False))
            delivery.status = (
                CommunityBridgeDeliveryStatus.DEAD
                if permanent or delivery.attempts >= 5
                else CommunityBridgeDeliveryStatus.PENDING
            )
            if permanent:
                delivery.encrypted_text = ""
            retry_after = _slack_retry_after_seconds(exc)
            delivery.available_at = timezone.now() + timedelta(
                seconds=max(retry_after, min(60, 2**delivery.attempts))
            )
            delivery.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            delivery.save(
                update_fields=(
                    "attempts",
                    "status",
                    "encrypted_text",
                    "available_at",
                    "last_error",
                    "updated_at",
                )
            )
            logger.exception(
                "slack_dm_mirror_delivery_failed delivery_id=%s", delivery.pk
            )
            continue
        processed += 1
    return processed


def discover_grants_if_due() -> None:
    """Periodically discover new IM channels without blocking Slack webhooks."""

    global _last_grant_discovery_scan
    now_monotonic = time.monotonic()
    if now_monotonic - _last_grant_discovery_scan < 5:
        return
    _last_grant_discovery_scan = now_monotonic
    cutoff = timezone.now() - timedelta(seconds=GRANT_DISCOVERY_INTERVAL_SECONDS)
    grants = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .filter(Q(last_discovery_at__isnull=True) | Q(last_discovery_at__lt=cutoff))[
            :10
        ]
    )
    for grant in grants:
        try:
            discover_conversations(grant)
        except Exception as exc:
            grant.last_discovery_at = timezone.now()
            grant.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            grant.save(update_fields=("last_discovery_at", "last_error", "updated_at"))
            logger.warning(
                "slack_dm_mirror_discovery_failed grant_id=%s error=%s",
                grant.pk,
                exc,
            )


def _provision_owner_conversation(
    conversation: SlackDmMirrorConversation,
    *,
    force_backfill: bool = False,
    reset_history: bool = False,
    required_owner_public_key: str | None = None,
) -> None:
    """Provision a mirror readable only by the consenting owner.

    The counterpart is represented by a deterministic shadow key. Even when
    that person has an MLAI account, their real key is never added to this
    owner-controlled mirror without their own independent Slack link.
    """

    participant_ids = sorted(set(conversation.participant_slack_ids or []))
    grant = conversation.grant
    if (
        grant.slack_user_id not in participant_ids
        or len(participant_ids) < 2
        or len(participant_ids) > 9
    ):
        raise SlackDmMirrorError("Slack DM participant set is invalid.")
    owner_identity = CommunityBridgeIdentityLink.objects.filter(
        slack_workspace_id=conversation.slack_workspace_id,
        slack_user_id=grant.slack_user_id,
        revoked_at__isnull=True,
    ).first()
    if owner_identity is None:
        conversation.status = SlackDmMirrorConversationStatus.AWAITING_SETUP
        conversation.save(update_fields=("status", "updated_at"))
        return
    current_owner_key = str(
        (conversation.participant_identity_map or {}).get(grant.slack_user_id) or ""
    ).lower()
    requested_owner_key = str(required_owner_public_key or "").strip().lower()
    if (
        requested_owner_key
        and _active_verified_device(grant.user_id, requested_owner_key) is None
    ):
        raise SlackDmMirrorError(
            "The authenticated MLAI Chat device is not active and verified."
        )
    if (
        current_owner_key
        and _active_verified_device(grant.user_id, current_owner_key) is not None
    ):
        attribution_key = current_owner_key
    else:
        attribution_key = owner_identity.buzz_pubkey
    counterpart_ids = [
        participant_id
        for participant_id in participant_ids
        if participant_id != grant.slack_user_id
    ]
    owner_capacity = 9 - len(counterpart_ids)
    if requested_owner_key and owner_capacity == 1:
        # A maximum-size MPIM can carry only one owner device. Keep the
        # explicitly authenticated device usable for this conversation without
        # mutating the grant-wide preferred identity link.
        attribution_key = requested_owner_key
    owner_device_pubkeys = _owner_device_pubkeys(
        grant.user_id,
        priority_pubkeys=(
            requested_owner_key,
            attribution_key,
            owner_identity.buzz_pubkey,
        ),
        limit=owner_capacity,
    )
    if attribution_key not in owner_device_pubkeys:
        raise SlackDmMirrorError(
            "This group DM has no capacity for the authenticated MLAI Chat device."
        )
    identity_map = {grant.slack_user_id: attribution_key}
    identity_map.update(
        {
            participant_id: _shadow_pubkey(conversation, participant_id)
            for participant_id in counterpart_ids
        }
    )
    pubkeys = sorted({*identity_map.values(), *owner_device_pubkeys})
    participant_hash = hashlib.sha256(
        b"".join(bytes.fromhex(value) for value in pubkeys)
    ).hexdigest()
    participant_set_changed = bool(
        conversation.participant_hash
        and conversation.participant_hash != participant_hash
    )
    needs_provision = bool(
        participant_set_changed
        or reset_history
        or conversation.status != SlackDmMirrorConversationStatus.LIVE
        or not conversation.mlai_channel_id
    )
    if participant_set_changed or reset_history:
        _mark_conversation_history_due(
            conversation,
            reason="Private conversation participants changed",
            reset_deliveries=True,
        )
    elif force_backfill:
        _mark_conversation_history_due(
            conversation,
            reason="History backfill requested",
            reset_deliveries=False,
        )
    conversation.participant_buzz_pubkeys = pubkeys
    conversation.participant_identity_map = identity_map
    conversation.participant_hash = participant_hash
    conversation.last_error = ""
    if not needs_provision:
        conversation.save(
            update_fields=(
                "participant_buzz_pubkeys",
                "participant_identity_map",
                "participant_hash",
                "last_error",
                "updated_at",
            )
        )
        return
    conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
    conversation.save(
        update_fields=(
            "participant_buzz_pubkeys",
            "participant_identity_map",
            "participant_hash",
            "status",
            "last_error",
            "history_backfilled_at",
            "updated_at",
        )
    )
    provisioned = BuzzBridgeClient.provision_private_conversation(
        pubkeys,
        callback_author_pubkeys=owner_device_pubkeys,
        conversation_name=_conversation_name(conversation),
    )
    conversation.mlai_channel_id = provisioned["channel_id"]
    conversation.status = SlackDmMirrorConversationStatus.LIVE
    conversation.save(update_fields=("mlai_channel_id", "status", "updated_at"))


def _mark_conversation_history_due(
    conversation: SlackDmMirrorConversation,
    *,
    reason: str,
    reset_deliveries: bool,
) -> None:
    now = timezone.now()
    _clear_history_scan_states([conversation.pk])
    conversation.history_backfilled_at = None
    conversation.oldest_synced_ts = ""
    conversation.last_error = ""
    conversation.save(
        update_fields=(
            "history_backfilled_at",
            "oldest_synced_ts",
            "last_error",
            "updated_at",
        )
    )
    if reset_deliveries:
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
        ).exclude(
            source_message_id__startswith=HISTORY_STATE_PREFIX,
        ).update(
            status=CommunityBridgeDeliveryStatus.DEAD,
            encrypted_text="",
            completed_at=None,
            last_error=reason[:2000],
            updated_at=now,
        )


def _clear_history_scan_states(conversation_ids: list[int]) -> None:
    if not conversation_ids:
        return
    SlackDmMirrorDelivery.objects.filter(
        conversation_id__in=conversation_ids,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id__startswith=HISTORY_STATE_PREFIX,
    ).delete()


def process_due_history_backfills(limit: int = 1) -> int:
    """Scan at most one Slack history page per due conversation.

    The worker calls this on a paced loop. Timestamp boundaries are persisted
    in the existing conversation marker, so no synchronous request needs to
    walk hundreds of pages and no schema change is required.
    """

    if time.monotonic() < _history_scan_available_at:
        return 0
    processed = 0
    scan_limit = max(1, min(int(limit), 5))
    for _ in range(scan_limit):
        now = timezone.now()
        with transaction.atomic():
            conversation = (
                SlackDmMirrorConversation.objects.select_for_update(skip_locked=True)
                .select_related("grant__connection")
                .filter(
                    history_backfilled_at__isnull=True,
                    status=SlackDmMirrorConversationStatus.LIVE,
                    grant__status=SlackDmMirrorGrantStatus.ACTIVE,
                    grant__revoked_at__isnull=True,
                )
                .exclude(
                    last_error__startswith="history_scan_processing:",
                    updated_at__gte=now - timedelta(minutes=5),
                )
                .order_by("updated_at", "id")
                .first()
            )
            if conversation is None:
                break
            conversation.last_error = f"history_scan_processing:{now.isoformat()}"
            conversation.save(update_fields=("last_error", "updated_at"))
        try:
            _enqueue_history_page(conversation, conversation.grant)
        except Exception as exc:
            _apply_slack_retry_after(exc)
            conversation.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            conversation.save(update_fields=("last_error", "updated_at"))
            logger.warning(
                "slack_dm_mirror_history_scan_failed conversation_id=%s error=%s",
                conversation.pk,
                exc,
            )
            continue
        processed += 1
    return processed


def _enqueue_history_page(
    conversation: SlackDmMirrorConversation,
    grant: SlackDmMirrorGrant,
) -> int:
    thread_state = _next_incomplete_thread_state(conversation)
    if thread_state is not None:
        return _enqueue_reply_page(conversation, grant, thread_state)
    if _history_state(conversation, HISTORY_MAIN_STATE_ID) is not None:
        _finish_history_scan(conversation)
        return 0

    request_kwargs: dict[str, Any] = {
        "channel": conversation.slack_conversation_id,
        "limit": 200,
    }
    if grant.history_days > 0:
        request_kwargs["oldest"] = str(
            max(0, int(time.time()) - grant.history_days * 86_400)
        )
        request_kwargs["inclusive"] = True
    if conversation.oldest_synced_ts:
        request_kwargs["latest"] = conversation.oldest_synced_ts
        request_kwargs["inclusive"] = False
    response = WebClient(token=grant.connection.access_token).conversations_history(
        **request_kwargs
    )
    raw_messages = [
        message
        for message in response.get("messages") or []
        if isinstance(message, dict)
    ]
    boundary_ids = []
    for message in raw_messages:
        message_id = str(message.get("ts") or "").strip()
        try:
            _slack_ts_sort_key(message_id)
        except SlackDmMirrorError:
            continue
        boundary_ids.append(message_id)
    history = []
    participant_ids = set(conversation.participant_slack_ids or [])
    for message in raw_messages:
        if message.get("bot_id"):
            continue
        message_id = str(message.get("ts") or "").strip()
        author_id = str(message.get("user") or "").strip()
        if not message_id or author_id not in participant_ids:
            continue
        _slack_ts_sort_key(message_id)
        history.append(message)
    history.sort(key=lambda message: _slack_ts_sort_key(str(message.get("ts") or "")))
    held_until = timezone.now() + timedelta(days=365)
    for message in history:
        _enqueue_history_message(
            conversation,
            message,
            held_until=held_until,
        )
        if (
            int(message.get("reply_count") or 0) > 0
            or bool(message.get("latest_reply"))
            or int(message.get("reply_users_count") or 0) > 0
        ):
            _ensure_thread_state(
                conversation,
                str(message.get("ts") or "").strip(),
            )

    next_cursor = str(
        (response.get("response_metadata") or {}).get("next_cursor") or ""
    ).strip()
    has_more = bool(response.get("has_more") or next_cursor)
    update_fields = ["last_error", "updated_at"]
    if boundary_ids:
        oldest_in_page = min(boundary_ids, key=_slack_ts_sort_key)
        newest_in_page = max(boundary_ids, key=_slack_ts_sort_key)
        conversation.oldest_synced_ts = oldest_in_page
        if not conversation.latest_synced_ts:
            conversation.latest_synced_ts = newest_in_page
        update_fields.extend(("oldest_synced_ts", "latest_synced_ts"))
    if has_more and boundary_ids:
        conversation.history_backfilled_at = None
    elif has_more:
        raise SlackDmMirrorError("Slack history pagination made no progress.")
    else:
        _ensure_history_state(
            conversation,
            source_message_id=HISTORY_MAIN_STATE_ID,
            metadata={"history_scan_state": "main", "complete": True},
        )
    conversation.last_error = ""
    conversation.save(update_fields=tuple(update_fields))
    if not has_more and _next_incomplete_thread_state(conversation) is None:
        _finish_history_scan(conversation)
    return len(history)


def _enqueue_reply_page(
    conversation: SlackDmMirrorConversation,
    grant: SlackDmMirrorGrant,
    state: SlackDmMirrorDelivery,
) -> int:
    metadata = dict(state.metadata or {})
    parent_message_id = str(metadata.get("parent_ts") or "").strip()
    if not parent_message_id:
        raise SlackDmMirrorError("Slack thread scan state is invalid.")
    request_kwargs: dict[str, Any] = {
        "channel": conversation.slack_conversation_id,
        "ts": parent_message_id,
        "limit": 200,
    }
    cursor = str(metadata.get("cursor") or "").strip()
    if cursor:
        request_kwargs["cursor"] = cursor
    response = WebClient(token=grant.connection.access_token).conversations_replies(
        **request_kwargs
    )
    participant_ids = set(conversation.participant_slack_ids or [])
    messages = []
    for message in response.get("messages") or []:
        if not isinstance(message, dict) or message.get("bot_id"):
            continue
        message_id = str(message.get("ts") or "").strip()
        author_id = str(message.get("user") or "").strip()
        if (
            not message_id
            or message_id == parent_message_id
            or author_id not in participant_ids
        ):
            continue
        _slack_ts_sort_key(message_id)
        messages.append(message)
    messages.sort(key=lambda message: _slack_ts_sort_key(str(message.get("ts") or "")))
    held_until = timezone.now() + timedelta(days=365)
    for message in messages:
        message = dict(message)
        message["thread_ts"] = str(message.get("thread_ts") or parent_message_id)
        _enqueue_history_message(
            conversation,
            message,
            held_until=held_until,
        )
    next_cursor = str(
        (response.get("response_metadata") or {}).get("next_cursor") or ""
    ).strip()
    has_more = bool(response.get("has_more") or next_cursor)
    if has_more and not next_cursor:
        raise SlackDmMirrorError("Slack thread pagination made no progress.")
    metadata["cursor"] = next_cursor
    metadata["complete"] = not has_more
    state.metadata = metadata
    state.save(update_fields=("metadata", "updated_at"))
    conversation.last_error = ""
    conversation.save(update_fields=("last_error", "updated_at"))
    if not has_more and _next_incomplete_thread_state(conversation) is None:
        _finish_history_scan(conversation)
    return len(messages)


def _enqueue_history_message(
    conversation: SlackDmMirrorConversation,
    message: dict[str, Any],
    *,
    held_until,
) -> None:
    message_id = str(message.get("ts") or "").strip()
    author_id = str(message.get("user") or "").strip()
    text = _slack_message_text(message)
    metadata = {
        "backfill": True,
        "event_ts": message_id,
        "thread_ts": str(message.get("thread_ts") or ""),
        "participant_hash": conversation.participant_hash,
    }
    _upsert_history_delivery(
        conversation,
        source_message_id=message_id,
        author_id=author_id,
        operation=CommunityBridgeDeliveryType.CREATE,
        text=text,
        metadata=metadata,
        held_until=held_until,
    )
    edited = message.get("edited") if isinstance(message.get("edited"), dict) else {}
    edited_timestamp = str(edited.get("ts") or "").strip()
    if edited_timestamp:
        edit_source_id = _slack_delivery_source_id(
            {},
            operation=CommunityBridgeDeliveryType.EDIT,
            target_message_id=message_id,
            event_timestamp=edited_timestamp,
            author_id=author_id,
            text=text,
        )
        _upsert_history_delivery(
            conversation,
            source_message_id=edit_source_id,
            author_id=author_id,
            operation=CommunityBridgeDeliveryType.EDIT,
            text=text,
            metadata={
                **metadata,
                "event_ts": edited_timestamp,
                "target_source_message_id": message_id,
            },
            held_until=held_until,
        )
    for reaction in message.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        slack_reaction = str(reaction.get("name") or "").strip().lower()
        emoji = slack_reaction_to_emoji(slack_reaction)
        if not emoji:
            continue
        for reaction_author_id in reaction.get("users") or []:
            reaction_author_id = str(reaction_author_id or "").strip()
            if reaction_author_id not in set(conversation.participant_slack_ids or []):
                continue
            semantic_id = reaction_object_id(
                message_id=message_id,
                reaction=slack_reaction,
                author_id=reaction_author_id,
            )
            _upsert_history_delivery(
                conversation,
                source_message_id=semantic_id,
                author_id=reaction_author_id,
                operation=CommunityBridgeDeliveryType.REACTION_ADD,
                text=emoji,
                metadata={
                    "backfill": True,
                    "event_ts": message_id,
                    "target_source_message_id": message_id,
                    "reaction_object_id": semantic_id,
                    "slack_reaction": slack_reaction,
                    "participant_hash": conversation.participant_hash,
                },
                held_until=held_until,
            )


def _upsert_history_delivery(
    conversation: SlackDmMirrorConversation,
    *,
    source_message_id: str,
    author_id: str,
    operation: str,
    text: str,
    metadata: dict[str, Any],
    held_until,
) -> SlackDmMirrorDelivery:
    delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=source_message_id,
        operation=operation,
        defaults={
            "source_author_id": author_id,
            "encrypted_text": text,
            "metadata": metadata,
            "available_at": held_until,
        },
    )
    if created:
        return delivery
    if delivery.status in (
        CommunityBridgeDeliveryStatus.FAILED,
        CommunityBridgeDeliveryStatus.DEAD,
    ):
        delivery.source_author_id = author_id
        delivery.encrypted_text = text
        delivery.metadata = metadata
        delivery.status = CommunityBridgeDeliveryStatus.PENDING
        delivery.attempts = 0
        delivery.available_at = held_until
        delivery.completed_at = None
        delivery.last_error = ""
        delivery.save(
            update_fields=(
                "source_author_id",
                "encrypted_text",
                "metadata",
                "status",
                "attempts",
                "available_at",
                "completed_at",
                "last_error",
                "updated_at",
            )
        )
    elif delivery.status == CommunityBridgeDeliveryStatus.PENDING:
        delivery.metadata = metadata
        delivery.save(update_fields=("metadata", "updated_at"))
    return delivery


def _history_state(
    conversation: SlackDmMirrorConversation,
    source_message_id: str,
) -> SlackDmMirrorDelivery | None:
    return SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=source_message_id,
        operation=CommunityBridgeDeliveryType.CREATE,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
    ).first()


def _ensure_history_state(
    conversation: SlackDmMirrorConversation,
    *,
    source_message_id: str,
    metadata: dict[str, Any],
) -> SlackDmMirrorDelivery:
    state, _ = SlackDmMirrorDelivery.objects.update_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=source_message_id,
        operation=CommunityBridgeDeliveryType.CREATE,
        defaults={
            "source_author_id": "",
            "encrypted_text": "",
            "metadata": metadata,
            "status": CommunityBridgeDeliveryStatus.COMPLETED,
            "available_at": timezone.now(),
            "completed_at": timezone.now(),
            "last_error": "",
        },
    )
    return state


def _ensure_thread_state(
    conversation: SlackDmMirrorConversation,
    parent_message_id: str,
) -> SlackDmMirrorDelivery:
    return _ensure_history_state(
        conversation,
        source_message_id=f"{HISTORY_STATE_PREFIX}thread:{parent_message_id}",
        metadata={
            "history_scan_state": "thread",
            "parent_ts": parent_message_id,
            "cursor": "",
            "complete": False,
        },
    )


def _next_incomplete_thread_state(
    conversation: SlackDmMirrorConversation,
) -> SlackDmMirrorDelivery | None:
    states = SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id__startswith=f"{HISTORY_STATE_PREFIX}thread:",
        operation=CommunityBridgeDeliveryType.CREATE,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
    ).order_by("id")
    return next(
        (state for state in states if not bool((state.metadata or {}).get("complete"))),
        None,
    )


@transaction.atomic
def _finish_history_scan(conversation: SlackDmMirrorConversation) -> None:
    """Commit completion, release, and scan-state cleanup as one unit."""

    conversation.history_backfilled_at = timezone.now()
    conversation.last_error = ""
    conversation.save(
        update_fields=("history_backfilled_at", "last_error", "updated_at")
    )
    _release_history_deliveries(conversation)
    _clear_history_scan_states([conversation.pk])


def _release_history_deliveries(conversation: SlackDmMirrorConversation) -> None:
    deliveries = list(
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            status=CommunityBridgeDeliveryStatus.PENDING,
            metadata__backfill=True,
        )
    )
    deliveries.sort(key=_delivery_slack_sort_key)
    available_at = timezone.now()
    for offset, delivery in enumerate(deliveries):
        delivery.available_at = available_at + timedelta(microseconds=offset)
    if deliveries:
        SlackDmMirrorDelivery.objects.bulk_update(deliveries, ("available_at",))


def _delivery_slack_sort_key(
    delivery: SlackDmMirrorDelivery,
) -> tuple[int, int, int, int]:
    metadata = delivery.metadata or {}
    timestamp = str(
        metadata.get("event_ts")
        or metadata.get("target_source_message_id")
        or delivery.source_message_id
    ).strip()
    try:
        seconds, fraction = _slack_ts_sort_key(timestamp)
    except SlackDmMirrorError:
        seconds, fraction = int(delivery.created_at.timestamp()), 0
    priority = {
        CommunityBridgeDeliveryType.CREATE: 0,
        CommunityBridgeDeliveryType.EDIT: 1,
        CommunityBridgeDeliveryType.REACTION_ADD: 2,
        CommunityBridgeDeliveryType.REACTION_REMOVE: 3,
        CommunityBridgeDeliveryType.DELETE: 4,
    }.get(delivery.operation, 5)
    return seconds, fraction, priority, delivery.pk


def _apply_slack_retry_after(exc: Exception) -> None:
    global _history_scan_available_at
    retry_after = _slack_retry_after_seconds(exc)
    if retry_after:
        _history_scan_available_at = time.monotonic() + retry_after


def _slack_retry_after_seconds(exc: Exception) -> int:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        return max(0, int(headers.get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0


def _deliver_private(delivery: SlackDmMirrorDelivery) -> None:
    # The row may have been claimed before the owner revoked consent, a device
    # was revoked, or the destination participant set changed. Re-read the
    # authorization boundary and encrypted body immediately before any network
    # call so an in-memory claim cannot bypass those changes.
    delivery = (
        SlackDmMirrorDelivery.objects.select_related(
            "conversation__grant__connection"
        )
        .filter(pk=delivery.pk)
        .first()
    )
    if (
        delivery is None
        or delivery.status != CommunityBridgeDeliveryStatus.PROCESSING
    ):
        raise SlackDmMirrorAuthorizationError(
            "The private delivery is no longer authorized."
        )
    if delivery.source_platform == CommunityBridgePlatform.SLACK:
        _deliver_to_mlai(delivery)
        return
    if delivery.source_platform == CommunityBridgePlatform.BUZZ:
        _deliver_to_slack(delivery)
        return
    raise SlackDmMirrorError(
        f"Unsupported private delivery source: {delivery.source_platform}"
    )


def _deliver_to_slack(delivery: SlackDmMirrorDelivery) -> None:
    conversation = delivery.conversation
    grant = conversation.grant
    if (
        conversation.status != SlackDmMirrorConversationStatus.LIVE
        or grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
    ):
        raise SlackDmMirrorError("Slack DM mirroring is not active.")
    source_author_pubkey = str(delivery.source_author_id or "").strip().lower()
    if (
        source_author_pubkey not in _conversation_owner_device_pubkeys(conversation)
        or _active_verified_device(grant.user_id, source_author_pubkey) is None
    ):
        raise SlackDmMirrorAuthorizationError(
            "The originating MLAI Chat device is no longer authorized."
        )
    operation = delivery.operation
    source_metadata = dict(delivery.metadata or {})
    client = WebClient(token=grant.connection.access_token)
    client_message_id = ""
    slack_ts = ""
    reaction = ""
    if operation == CommunityBridgeDeliveryType.CREATE:
        client_message_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"mlai-chat-slack-dm:{delivery.pk}")
        )
        parent_source_id = str(
            source_metadata.get("source_parent_message_id") or ""
        ).strip()
        thread_ts = ""
        if parent_source_id:
            thread_ts = _slack_destination_message_id(
                conversation,
                parent_source_id,
            )
            if not thread_ts:
                raise SlackDmMirrorError(
                    "The private thread parent has not reached Slack yet."
                )
        source_metadata.update(
            {
                "client_msg_id": client_message_id,
                "thread_ts": thread_ts,
            }
        )
        delivery.metadata = source_metadata
        delivery.save(update_fields=("metadata", "updated_at"))
        request_kwargs = {
            "channel": conversation.slack_conversation_id,
            "text": delivery.encrypted_text,
            "client_msg_id": client_message_id,
            "unfurl_links": True,
            "unfurl_media": True,
        }
        if thread_ts:
            request_kwargs["thread_ts"] = thread_ts
        response = client.chat_postMessage(**request_kwargs)
        slack_ts = str(response.get("ts") or "").strip()
        _slack_ts_sort_key(slack_ts)
    else:
        target_source_id = str(
            source_metadata.get("target_source_message_id")
            or source_metadata.get("source_event_id")
            or delivery.source_message_id
        ).strip()
        slack_ts = _slack_destination_message_id(conversation, target_source_id)
        if not slack_ts:
            raise SlackDmMirrorError(
                "The original private message has not reached Slack yet."
            )
        if operation in {
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }:
            reaction = emoji_to_slack_reaction(delivery.encrypted_text)
            if not reaction:
                raise SlackDmMirrorError("The reaction is not supported by Slack.")
        echo_key = _slack_echo_key(
            operation=operation,
            target_message_id=slack_ts,
            author_id=grant.slack_user_id,
            reaction=reaction,
            text=(
                delivery.encrypted_text
                if operation == CommunityBridgeDeliveryType.EDIT
                else ""
            ),
        )
        source_metadata.update(
            {
                "slack_ts": slack_ts,
                "slack_echo_key": echo_key,
                "slack_reaction": reaction,
                "slack_reaction_object_id": (
                    reaction_object_id(
                        message_id=slack_ts,
                        reaction=reaction,
                        author_id=grant.slack_user_id,
                    )
                    if reaction
                    else ""
                ),
            }
        )
        delivery.metadata = source_metadata
        delivery.save(update_fields=("metadata", "updated_at"))
        try:
            if operation == CommunityBridgeDeliveryType.EDIT:
                response = client.chat_update(
                    channel=conversation.slack_conversation_id,
                    ts=slack_ts,
                    text=delivery.encrypted_text,
                )
                returned_ts = str(response.get("ts") or slack_ts).strip()
                _slack_ts_sort_key(returned_ts)
                slack_ts = returned_ts
            elif operation == CommunityBridgeDeliveryType.DELETE:
                client.chat_delete(
                    channel=conversation.slack_conversation_id,
                    ts=slack_ts,
                )
            elif operation == CommunityBridgeDeliveryType.REACTION_ADD:
                client.reactions_add(
                    channel=conversation.slack_conversation_id,
                    timestamp=slack_ts,
                    name=reaction,
                )
            elif operation == CommunityBridgeDeliveryType.REACTION_REMOVE:
                client.reactions_remove(
                    channel=conversation.slack_conversation_id,
                    timestamp=slack_ts,
                    name=reaction,
                )
            else:
                raise SlackDmMirrorError(
                    f"Unsupported private Slack operation: {operation}"
                )
        except SlackApiError as exc:
            error_code = str(exc.response.get("error") or "")
            idempotent_errors = {
                CommunityBridgeDeliveryType.DELETE: {"message_not_found"},
                CommunityBridgeDeliveryType.REACTION_ADD: {"already_reacted"},
                CommunityBridgeDeliveryType.REACTION_REMOVE: {"no_reaction"},
            }
            if error_code not in idempotent_errors.get(operation, set()):
                raise
    now = timezone.now()
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.metadata = {
        **source_metadata,
        "slack_ts": slack_ts,
        "client_msg_id": client_message_id,
        "slack_reaction": reaction,
    }
    delivery.completed_at = now
    delivery.last_error = ""
    delivery.save(
        update_fields=(
            "status",
            "encrypted_text",
            "metadata",
            "completed_at",
            "last_error",
            "updated_at",
        )
    )
    conversation.last_synced_at = now
    conversation.save(update_fields=("last_synced_at", "updated_at"))
    grant.last_synced_at = now
    grant.save(update_fields=("last_synced_at", "updated_at"))


def _slack_destination_message_id(
    conversation: SlackDmMirrorConversation,
    mlai_message_id: str,
) -> str:
    normalized_id = str(mlai_message_id or "").strip().lower()
    if not normalized_id:
        return ""
    outgoing = (
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=normalized_id,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if outgoing is not None:
        return str((outgoing.metadata or {}).get("slack_ts") or "").strip()
    mirrored = (
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__destination_message_id=normalized_id,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    return str(mirrored.source_message_id if mirrored is not None else "").strip()


def _deliver_to_mlai(delivery: SlackDmMirrorDelivery) -> None:
    conversation = delivery.conversation
    grant = conversation.grant
    if (
        conversation.status != SlackDmMirrorConversationStatus.LIVE
        or not conversation.mlai_channel_id
        or grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
    ):
        raise SlackDmMirrorAuthorizationError(
            "Slack DM mirroring is no longer authorized."
        )
    queued_participant_hash = str(
        (delivery.metadata or {}).get("participant_hash") or ""
    ).strip()
    if (
        queued_participant_hash
        and queued_participant_hash != conversation.participant_hash
    ):
        raise SlackDmMirrorAuthorizationError(
            "The private conversation participants changed."
        )
    linked_pubkey = str(
        (conversation.participant_identity_map or {}).get(delivery.source_author_id)
        or ""
    ).lower()
    if not linked_pubkey:
        raise SlackDmMirrorError("Slack author is not part of this owner mirror.")
    profile = (conversation.participant_profiles or {}).get(
        delivery.source_author_id
    ) or {}
    source_metadata = delivery.metadata or {}
    target_message_id = ""
    parent_message_id = ""
    if delivery.operation == CommunityBridgeDeliveryType.CREATE:
        source_parent_id = str(source_metadata.get("thread_ts") or "").strip()
        if source_parent_id and source_parent_id != delivery.source_message_id:
            parent_message_id = _private_destination_message_id(
                conversation,
                source_parent_id,
            )
            if not parent_message_id:
                raise SlackDmMirrorError(
                    "The private thread parent has not reached MLAI Chat yet."
                )
    elif delivery.operation == CommunityBridgeDeliveryType.REACTION_REMOVE:
        reaction_object = str(source_metadata.get("reaction_object_id") or "").strip()
        target_message_id = _private_destination_operation_message_id(
            conversation,
            source_message_id=reaction_object,
            operation=CommunityBridgeDeliveryType.REACTION_ADD,
            metadata_key="reaction_object_id",
        )
        if not target_message_id:
            raise SlackDmMirrorError(
                "The private reaction has not reached MLAI Chat yet."
            )
    else:
        target_source_message_id = str(
            source_metadata.get("target_source_message_id")
            or delivery.source_message_id
        ).strip()
        target_message_id = _private_destination_message_id(
            conversation,
            target_source_message_id,
        )
        if not target_message_id:
            raise SlackDmMirrorError(
                "The original private message has not reached MLAI Chat yet."
            )
    result = BuzzBridgeClient.deliver_private(
        delivery_id=str(delivery.pk),
        created_at=_delivery_created_at(delivery),
        operation=delivery.operation,
        channel_id=str(conversation.mlai_channel_id),
        participant_pubkeys=list(conversation.participant_buzz_pubkeys or []),
        text=delivery.encrypted_text,
        source_workspace_id=conversation.slack_workspace_id,
        source_channel_id=conversation.slack_conversation_id,
        source_message_id=delivery.source_message_id,
        source_author_id=delivery.source_author_id,
        source_author_display_name=str(
            profile.get("display_name") or delivery.source_author_id
        ),
        source_author_avatar_url=str(profile.get("avatar_url") or ""),
        linked_pubkey=linked_pubkey,
        target_message_id=target_message_id,
        parent_message_id=parent_message_id,
    )
    now = timezone.now()
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.metadata = {
        **source_metadata,
        "participant_hash": conversation.participant_hash,
    }
    if delivery.operation in {
        CommunityBridgeDeliveryType.CREATE,
        CommunityBridgeDeliveryType.REACTION_ADD,
    } and isinstance(result, dict):
        delivery.metadata.update(
            {
                "destination_message_id": str(result.get("message_id") or ""),
                "destination_parent_message_id": str(
                    result.get("parent_message_id") or ""
                ),
            }
        )
    delivery.completed_at = now
    delivery.last_error = ""
    delivery.save(
        update_fields=(
            "status",
            "encrypted_text",
            "metadata",
            "completed_at",
            "last_error",
            "updated_at",
        )
    )
    conversation.last_synced_at = now
    event_timestamp = str(source_metadata.get("event_ts") or "").strip()
    if not event_timestamp and delivery.operation == CommunityBridgeDeliveryType.CREATE:
        event_timestamp = delivery.source_message_id
    try:
        _slack_ts_sort_key(event_timestamp)
    except SlackDmMirrorError:
        event_timestamp = ""
    if event_timestamp:
        conversation.latest_synced_ts = event_timestamp
    conversation.save(
        update_fields=("last_synced_at", "latest_synced_ts", "updated_at")
    )
    conversation.grant.last_synced_at = now
    conversation.grant.save(update_fields=("last_synced_at", "updated_at"))


def _private_destination_message_id(
    conversation: SlackDmMirrorConversation,
    source_message_id: str,
) -> str:
    normalized_source_id = str(source_message_id or "").strip()
    delivery = (
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=normalized_source_id,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if delivery is not None:
        return str(
            (delivery.metadata or {}).get("destination_message_id") or ""
        ).strip()
    outgoing = (
        SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__slack_ts=normalized_source_id,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if outgoing is None:
        return ""
    return str(
        (outgoing.metadata or {}).get("source_event_id") or outgoing.source_message_id
    )


def _private_destination_operation_message_id(
    conversation: SlackDmMirrorConversation,
    *,
    source_message_id: str,
    operation: str,
    metadata_key: str = "",
) -> str:
    filters: dict[str, Any] = {
        "conversation": conversation,
        "source_platform": CommunityBridgePlatform.SLACK,
        "operation": operation,
        "status": CommunityBridgeDeliveryStatus.COMPLETED,
    }
    if metadata_key:
        filters[f"metadata__{metadata_key}"] = str(source_message_id or "").strip()
    else:
        filters["source_message_id"] = str(source_message_id or "").strip()
    delivery = (
        SlackDmMirrorDelivery.objects.filter(**filters)
        .order_by("-completed_at", "-id")
        .first()
    )
    if delivery is None:
        if metadata_key != "reaction_object_id":
            return ""
        outgoing = (
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.BUZZ,
                operation=CommunityBridgeDeliveryType.REACTION_ADD,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                metadata__slack_reaction_object_id=str(source_message_id or "").strip(),
            )
            .order_by("-completed_at", "-id")
            .first()
        )
        if outgoing is None:
            return ""
        return str(
            (outgoing.metadata or {}).get("source_event_id")
            or outgoing.source_message_id
        ).strip()
    return str((delivery.metadata or {}).get("destination_message_id") or "").strip()


def _delivery_created_at(delivery: SlackDmMirrorDelivery) -> int:
    metadata = delivery.metadata or {}
    candidates = (
        metadata.get("event_ts"),
        metadata.get("target_source_message_id"),
        delivery.source_message_id,
    )
    for value in candidates:
        try:
            return _slack_ts_sort_key(str(value or ""))[0]
        except SlackDmMirrorError:
            continue
    return int(delivery.created_at.timestamp())


def _slack_ts_sort_key(value: str) -> tuple[int, int]:
    seconds, separator, fraction = str(value or "").partition(".")
    if (
        separator != "."
        or not seconds
        or not fraction
        or not seconds.isdigit()
        or not fraction.isdigit()
    ):
        raise SlackDmMirrorError("Slack returned an invalid message timestamp.")
    return int(seconds), int(fraction)


def _connection_identity(connection: ExternalServiceConnection) -> tuple[str, str]:
    metadata = connection.provider_metadata or {}
    team = metadata.get("team") if isinstance(metadata.get("team"), dict) else {}
    authed_user = (
        metadata.get("authed_user")
        if isinstance(metadata.get("authed_user"), dict)
        else {}
    )
    workspace_id = str(team.get("id") or connection.external_account_id or "").strip()
    slack_user_id = str(authed_user.get("id") or "").strip()
    if not workspace_id or not slack_user_id:
        raise SlackDmMirrorError(
            "Slack OAuth response did not identify the workspace and user."
        )
    return workspace_id, slack_user_id


def ensure_owner_identity(
    grant: SlackDmMirrorGrant,
    *,
    authenticated_public_key: str | None = None,
    allow_preferred_fallback: bool = False,
) -> tuple[CommunityBridgeIdentityLink, bool, bool | None]:
    """Return the owner identity and repair only a stale device binding.

    An identity that still points at an active verified device is deliberately
    stable. A request authenticated by a different active device must not move
    the mirror implicitly; revoking the old device makes the next discovery or
    start-DM request perform the safe rebind.
    """

    requested_key = str(authenticated_public_key or "").strip().lower()
    with transaction.atomic():
        locked_grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .select_related("connection", "user")
            .get(pk=grant.pk)
        )
        requested_device = None
        if requested_key:
            requested_device = _active_verified_device(
                locked_grant.user_id,
                requested_key,
            )
            if requested_device is None:
                raise SlackDmMirrorError(
                    "The authenticated MLAI Chat device is not active and verified."
                )

        link = (
            CommunityBridgeIdentityLink.objects.select_for_update()
            .filter(
                slack_workspace_id=locked_grant.slack_workspace_id,
                slack_user_id=locked_grant.slack_user_id,
            )
            .first()
        )
        if link is not None and link.user_id not in (None, locked_grant.user_id):
            raise SlackDmMirrorError(
                "This Slack identity is already linked to another MLAI account."
            )
        linked_device = None
        if link is not None and link.revoked_at is None:
            linked_device = _active_verified_device(
                locked_grant.user_id,
                link.buzz_pubkey,
            )
        if linked_device is not None:
            authenticated_matches = (
                None if not requested_key else link.buzz_pubkey == requested_key
            )
            return link, False, authenticated_matches

        target_device = requested_device
        if target_device is None and allow_preferred_fallback:
            target_device = _preferred_device(locked_grant.user_id)
        if target_device is None:
            raise SlackDmMirrorError(
                "Verify an MLAI Chat device before linking Slack DMs."
            )

        now = timezone.now()
        display_name = (
            locked_grant.user.full_name
            or locked_grant.user.email
            or locked_grant.slack_user_id
        )
        values = {
            "user": locked_grant.user,
            "buzz_pubkey": target_device.public_key,
            "display_name": display_name,
            "verification_method": (
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            "verification_reference": f"community-chat-device:{target_device.pk}",
            "verified_at": now,
            "revoked_at": None,
            "revocation_reason": "",
        }
        if link is None:
            link = CommunityBridgeIdentityLink.objects.create(
                slack_workspace_id=locked_grant.slack_workspace_id,
                slack_user_id=locked_grant.slack_user_id,
                **values,
            )
        else:
            for field, value in values.items():
                setattr(link, field, value)
            link.save(update_fields=(*values.keys(), "updated_at"))
        conversation_ids = list(
            SlackDmMirrorConversation.objects.filter(grant=locked_grant).values_list(
                "id", flat=True
            )
        )
        _clear_history_scan_states(conversation_ids)
        SlackDmMirrorConversation.objects.filter(id__in=conversation_ids).update(
            history_backfilled_at=None,
            oldest_synced_ts="",
            last_error="",
            updated_at=now,
        )
        SlackDmMirrorDelivery.objects.filter(
            conversation_id__in=conversation_ids,
            source_platform=CommunityBridgePlatform.SLACK,
        ).exclude(
            source_message_id__startswith=HISTORY_STATE_PREFIX,
        ).update(
            status=CommunityBridgeDeliveryStatus.DEAD,
            encrypted_text="",
            completed_at=None,
            last_error="Owner device identity changed; awaiting Slack history requeue",
            updated_at=now,
        )
        locked_grant.last_discovery_at = None
        locked_grant.save(update_fields=("last_discovery_at", "updated_at"))
        grant.last_discovery_at = None
        return link, True, True if requested_key else None


def _identity_status(
    grant: SlackDmMirrorGrant | None,
    *,
    authenticated_public_key: str | None = None,
) -> dict[str, Any]:
    if grant is None:
        return {
            "state": "not_linked",
            "active": False,
            "repair_required": False,
            "authenticated_device_matches": None,
            "authenticated_device_active": None,
            "active_device_count": 0,
        }
    link = CommunityBridgeIdentityLink.objects.filter(
        slack_workspace_id=grant.slack_workspace_id,
        slack_user_id=grant.slack_user_id,
    ).first()
    active = bool(
        link is not None
        and link.revoked_at is None
        and link.user_id == grant.user_id
        and _active_verified_device(grant.user_id, link.buzz_pubkey) is not None
    )
    requested_key = str(authenticated_public_key or "").strip().lower()
    authenticated_matches = None
    if requested_key:
        authenticated_matches = bool(
            active and link and link.buzz_pubkey == requested_key
        )
    authenticated_device_active = (
        None
        if not requested_key
        else _active_verified_device(grant.user_id, requested_key) is not None
    )
    return {
        "state": "active" if active else "repair_required",
        "active": active,
        "repair_required": not active,
        "authenticated_device_matches": authenticated_matches,
        "authenticated_device_active": authenticated_device_active,
        "active_device_count": CommunityChatDevice.objects.filter(
            user_id=grant.user_id,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        ).count(),
    }


def _active_verified_device(
    user_id: int,
    public_key: str,
) -> CommunityChatDevice | None:
    return CommunityChatDevice.objects.filter(
        user_id=user_id,
        public_key=str(public_key or "").strip().lower(),
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    ).first()


def _owner_device_pubkeys(
    user_id: int,
    *,
    priority_pubkeys: tuple[str, ...] = (),
    limit: int = 8,
) -> list[str]:
    devices = list(
        CommunityChatDevice.objects.filter(
            user_id=user_id,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        ).order_by("-last_seen_at", "-verified_at", "-created_at")
    )
    active = {device.public_key.lower(): device for device in devices}
    ordered = []
    for value in (*priority_pubkeys, *(device.public_key for device in devices)):
        public_key = str(value or "").strip().lower()
        if public_key in active and public_key not in ordered:
            ordered.append(public_key)
    return ordered[: max(1, min(int(limit), 8))]


def _preferred_device(user_id: int) -> CommunityChatDevice | None:
    return (
        CommunityChatDevice.objects.filter(
            user_id=user_id,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        )
        .order_by("-last_seen_at", "-verified_at", "-created_at")
        .first()
    )


def _history_days() -> int:
    return max(0, min(int(getattr(settings, "SLACK_DM_MIRROR_HISTORY_DAYS", 30)), 90))


def _shadow_pubkey(conversation: SlackDmMirrorConversation, counterpart_id: str) -> str:
    secret = str(
        getattr(settings, "SLACK_DM_MIRROR_SHADOW_SECRET", "")
        or getattr(settings, "SECRET_KEY", "")
    ).encode("utf-8")
    if not secret:
        raise SlackDmMirrorError("Slack DM shadow-key secret is not configured.")
    context = ":".join(
        (
            conversation.slack_workspace_id,
            conversation.slack_conversation_id,
            conversation.grant.slack_user_id,
            counterpart_id,
        )
    ).encode("utf-8")
    for counter in range(256):
        candidate = hmac.new(
            secret, context + bytes((counter,)), hashlib.sha256
        ).digest()
        try:
            return PrivateKey(candidate).public_key_xonly.format().hex()
        except ValueError:
            continue
    raise SlackDmMirrorError("Could not derive a valid Slack DM shadow key.")


def _slack_profile(
    client: WebClient,
    slack_user_id: str,
    cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    cached = cache.get(slack_user_id)
    if cached is not None:
        return cached
    response = client.users_info(user=slack_user_id)
    user = response.get("user") if isinstance(response.get("user"), dict) else {}
    profile = _profile_from_slack_user(user)
    cache[slack_user_id] = profile
    return profile


def _profile_from_slack_user(user: dict[str, Any]) -> dict[str, str]:
    raw_profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    display_name = str(
        raw_profile.get("display_name")
        or raw_profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id")
        or "Slack user"
    ).strip()
    avatar_url = str(
        raw_profile.get("image_192")
        or raw_profile.get("image_72")
        or raw_profile.get("image_48")
        or ""
    ).strip()
    profile = {"display_name": display_name[:255], "avatar_url": avatar_url[:2000]}
    return profile


def _is_eligible_slack_user(
    user: dict[str, Any],
    *,
    workspace_id: str,
    owner_slack_user_id: str,
) -> bool:
    slack_user_id = str(user.get("id") or "").strip()
    if not slack_user_id or slack_user_id in {owner_slack_user_id, "USLACKBOT"}:
        return False
    if any(
        bool(user.get(flag))
        for flag in ("deleted", "is_bot", "is_app_user", "is_stranger")
    ) or user.get("bot_id"):
        return False
    team_id = str(user.get("team_id") or user.get("team") or "").strip()
    return not team_id or team_id == workspace_id


def _serialize_slack_user(user: dict[str, Any]) -> dict[str, str]:
    profile = _profile_from_slack_user(user)
    return {
        "slack_user_id": str(user.get("id") or "").strip(),
        "display_name": profile["display_name"],
        "avatar_url": profile["avatar_url"],
    }


def _slack_user_matches(user: dict[str, Any], query_text: str) -> bool:
    if not query_text:
        return True
    raw_profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    haystack = " ".join(
        str(value or "")
        for value in (
            raw_profile.get("display_name"),
            raw_profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        )
    ).casefold()
    return query_text in haystack


def _encode_directory_cursor(slack_cursor: str, offset: int) -> str:
    payload = json.dumps(
        {"cursor": str(slack_cursor or ""), "offset": int(offset)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_directory_cursor(value: str) -> tuple[str, int]:
    cursor = str(value or "").strip()
    if not cursor:
        return "", 0
    if len(cursor) > 2000:
        raise SlackDmMirrorError("Slack directory cursor is invalid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        slack_cursor = str(payload.get("cursor") or "").strip()
        offset = int(payload.get("offset") or 0)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise SlackDmMirrorError("Slack directory cursor is invalid.") from exc
    if len(slack_cursor) > 1000 or not 0 <= offset <= 200:
        raise SlackDmMirrorError("Slack directory cursor is invalid.")
    return slack_cursor, offset


def _is_external_shared_conversation(raw_conversation: dict[str, Any]) -> bool:
    return any(
        bool(raw_conversation.get(flag))
        for flag in (
            "is_ext_shared",
            "is_external_shared",
            "is_org_shared",
            "is_shared",
        )
    )


def _conversation_participant_ids(
    client: WebClient,
    raw_conversation: dict[str, Any],
    *,
    owner_slack_user_id: str,
) -> list[str]:
    channel_id = str(raw_conversation.get("id") or "").strip()
    if channel_id.startswith("D"):
        counterpart = str(raw_conversation.get("user") or "").strip()
        participant_ids = sorted({owner_slack_user_id, counterpart} - {""})
        return participant_ids if len(participant_ids) == 2 else []
    if not channel_id.startswith("G") or not raw_conversation.get("is_mpim"):
        return []

    participant_ids = {
        str(value or "").strip()
        for value in raw_conversation.get("members") or []
        if str(value or "").strip()
    }
    cursor = ""
    while not participant_ids or cursor:
        response = client.conversations_members(
            channel=channel_id,
            limit=200,
            cursor=cursor,
        )
        participant_ids.update(
            str(value or "").strip()
            for value in response.get("members") or []
            if str(value or "").strip()
        )
        cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if not cursor:
            break
    if owner_slack_user_id not in participant_ids or not 2 <= len(participant_ids) <= 9:
        return []
    return sorted(participant_ids)


def _conversation_name(conversation: SlackDmMirrorConversation) -> str:
    profiles = conversation.participant_profiles or {}
    counterpart_ids = [
        value
        for value in conversation.participant_slack_ids or []
        if value != conversation.grant.slack_user_id
    ]
    if not counterpart_ids:
        return "Slack DM"
    display_names = []
    for counterpart_id in counterpart_ids:
        profile = profiles.get(counterpart_id) or {}
        display_names.append(str(profile.get("display_name") or counterpart_id))
    return ", ".join(display_names)[:255]
