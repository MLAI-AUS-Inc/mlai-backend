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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from coincurve import PrivateKey
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.fields import (
    CredentialEncryptionError,
    decrypt_credential_value,
    encrypt_credential_value,
)
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
from integrations.services.slack_dm_registration_ledger import (
    PRIVATE_REGISTRATION_REVOCATION_PENDING,
    REGISTRATION_CLEANUP_LEASE_SECONDS,
    REGISTRATION_STATE_ACTIVE,
    REGISTRATION_STATE_AMBIGUOUS,
    REGISTRATION_STATE_CLEANUP_PENDING,
    REGISTRATION_STATE_CLEANUP_PROCESSING,
    REGISTRATION_STATE_PREFIX,
    REGISTRATION_STATE_PROVISIONING,
    RegistrationCleanupPending,
    adopt_current_registration_generation_locked as _adopt_current_registration_generation_locked,
    conversation_name as _conversation_name,
    conversation_owner_device_pubkeys as _conversation_owner_device_pubkeys,
    create_registration_row_locked as _create_registration_row_locked,
    ensure_current_registration_row_locked as _ensure_current_registration_row_locked,
    finalize_registration_attempt as _finalize_registration_attempt,
    grant_consent_generation as _grant_consent_generation,
    mark_registration_cleanup_pending_locked as _mark_registration_cleanup_pending_locked,
    prepare_conversation_registration_cleanup_locked as _prepare_conversation_registration_cleanup_locked,
    prepare_generation_transition_locked as _prepare_generation_transition_locked,
    prepare_registration_cleanup_locked as _prepare_registration_cleanup_locked,
    reconcile_registration_cleanup as _reconcile_registration_cleanup_ledger,
    record_ambiguous_registration_attempt as _record_ambiguous_registration_attempt,
    registration_cleanup_pending_locked as _registration_cleanup_pending_locked,
    registration_channel_id as _registration_channel_id,
    registration_generation as _registration_generation,
    registration_participant_hash as _registration_participant_hash,
    registration_slack_participant_ids as _registration_slack_participant_ids,
    registration_state as _registration_state,
    save_registration_state_locked as _save_registration_state_locked,
    update_registration_cleanup_summary_locked as _update_registration_cleanup_summary_locked,
)
from integrations.services.slack_oauth_authority import (
    SLACK_OAUTH_GENERATION_KEY,
    advance_slack_oauth_generation_locked,
    connection_slack_oauth_generation,
    current_slack_oauth_generation,
)
from startup_updates.models import (
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
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
_history_expiration_cursor = 0
_history_expiration_scan_available_at = 0.0
GRANT_DISCOVERY_INTERVAL_SECONDS = 300
HISTORY_RECONCILIATION_INTERVAL_SECONDS = 3600
HISTORY_EXPIRATION_SCAN_INTERVAL_SECONDS = 60
MAX_HISTORY_DAYS = 30
HISTORY_PAGE_LIMIT = 1000
HISTORY_REQUESTS_PER_MINUTE = 50
HISTORY_REQUEST_INTERVAL_SECONDS = 60 / HISTORY_REQUESTS_PER_MINUTE
PROFILE_BULK_PRELOAD_THRESHOLD = 4
MAX_PRIVATE_DELIVERY_BATCH = 20
MAX_PRIVATE_DELIVERY_BATCH_TEXT_BYTES = 700_000
HISTORY_STATE_PREFIX = "history-state:"
HISTORY_MAIN_STATE_ID = f"{HISTORY_STATE_PREFIX}main"
DISCOVERY_CHECKPOINT_KEY = "slack_dm_mirror_discovery_v1"
PENDING_EVENT_CHECKPOINT_KEY = "slack_dm_mirror_pending_events_v1"
MAX_PENDING_UNKNOWN_EVENTS = 5000
MAX_DISCOVERY_CONVERSATIONS = 100_000
SLACK_ECHO_WINDOW_SECONDS = 3600
DEPENDENCY_ARRIVAL_GRACE_SECONDS = 86_400
HISTORY_RECONCILE_CANDIDATE_KEY = "history_reconcile_candidate"
HISTORY_RECONCILE_EPOCH_KEY = "history_reconcile_epoch"
HISTORY_RECONCILE_OLDEST_KEY = "history_reconcile_oldest"
SLACK_CONNECT_INELIGIBLE_REASON = "Slack Connect conversations are not eligible"
SLACK_PARTICIPANTS_INELIGIBLE_REASON = (
    "Slack conversation participants are no longer eligible"
)
SLACK_CONVERSATION_UNAVAILABLE_REASON = (
    "Slack conversation is archived or no longer available"
)


class SlackDmMirrorError(RuntimeError):
    """Raised when a Slack DM grant cannot be activated safely."""

    code = "slack_dm_mirror_error"


class SlackDmMirrorAuthorizationError(SlackDmMirrorError):
    """Raised when a queued private operation no longer has owner authority."""

    permanent = True


class SlackDmMirrorDependencyPending(SlackDmMirrorError):
    """A private operation is waiting for an earlier mirrored operation."""


class SlackDmMirrorCredentialError(SlackDmMirrorAuthorizationError):
    """The current Slack OAuth credential cannot be refreshed or used."""


class SlackDmMirrorUpstreamError(SlackDmMirrorError):
    """Slack could not be reached or returned a malformed response."""


SLACK_AUTH_ERROR_CODES = {
    "account_inactive",
    "invalid_auth",
    "missing_scope",
    "no_permission",
    "not_allowed_token_type",
    "not_authed",
    "org_login_required",
    "token_expired",
    "token_revoked",
}


def _is_slack_auth_error(exc: Exception) -> bool:
    if isinstance(exc, SlackDmMirrorCredentialError):
        return True
    if not isinstance(exc, SlackApiError):
        return False
    try:
        code = str(exc.response.get("error") or "").strip().lower()
    except (AttributeError, TypeError):
        return False
    return code in SLACK_AUTH_ERROR_CODES


@dataclass(frozen=True)
class _SlackGrantApiAuthority:
    """Immutable authority expected by one bounded sequence of Slack reads."""

    grant_id: int
    user_id: int
    connection_id: int
    consent_generation: str
    consent_version: str
    workspace_id: str
    slack_user_id: str
    oauth_generation: int
    access_token: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class _SlackHistoryScanAuthority:
    """Durable identity of one conversation history scan attempt."""

    epoch: str
    participant_hash: str
    mlai_channel_id: str
    registration_id: str
    registration_generation: str
    history_days: int
    oldest: str


def _normalized_slack_scopes(values: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(value or "").strip()
                for value in values or []
                if str(value or "").strip()
            }
        )
    )


def _capture_slack_grant_api_authority(
    grant: SlackDmMirrorGrant,
    *,
    refresh_token: bool = True,
) -> _SlackGrantApiAuthority:
    """Capture exact values that every subsequent Slack page must revalidate."""

    if refresh_token:
        grant = _refresh_slack_grant_token_if_due(grant)
    connection = grant.connection
    return _SlackGrantApiAuthority(
        grant_id=grant.pk,
        user_id=grant.user_id,
        connection_id=grant.connection_id,
        consent_generation=_grant_consent_generation(grant),
        consent_version=str(grant.consent_version or ""),
        workspace_id=str(grant.slack_workspace_id or "").strip(),
        slack_user_id=str(grant.slack_user_id or "").strip(),
        oauth_generation=connection_slack_oauth_generation(connection),
        access_token=str(connection.access_token or "").strip(),
        scopes=_normalized_slack_scopes(connection.scopes),
    )


def _refresh_slack_grant_token_if_due(
    grant: SlackDmMirrorGrant,
) -> SlackDmMirrorGrant:
    connection = grant.connection
    expires_at = connection.token_expires_at
    if expires_at is None or expires_at > timezone.now() + timedelta(minutes=2):
        return grant
    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(
            pk=grant.user_id
        )
        locked_grants = list(
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(user_id=locked_user.pk)
            .order_by("id")
        )
        locked_grant = next(
            (item for item in locked_grants if item.pk == grant.pk),
            None,
        )
        locked_connections = list(
            ExternalServiceConnection.objects.select_for_update()
            .filter(
                user_id=locked_user.pk,
                provider=ExternalServiceProvider.SLACK,
            )
            .order_by("id")
        )
        locked_connection = next(
            (item for item in locked_connections if item.pk == grant.connection_id),
            None,
        )
        if (
            locked_grant is None
            or locked_connection is None
            or locked_grant.status != SlackDmMirrorGrantStatus.ACTIVE
            or locked_grant.revoked_at is not None
            or locked_grant.connection_id != locked_connection.pk
        ):
            raise SlackDmMirrorAuthorizationError(
                "Slack consent changed before token refresh."
            )
        locked_expiry = locked_connection.token_expires_at
        if locked_expiry is None or locked_expiry > timezone.now() + timedelta(
            minutes=2
        ):
            return (
                SlackDmMirrorGrant.objects.select_related("connection")
                .get(pk=locked_grant.pk)
            )
        refresh_token = str(locked_connection.refresh_token or "").strip()
        if not refresh_token:
            raise SlackDmMirrorCredentialError(
                "Slack token expired and must be re-authorized."
            )
        try:
            response = requests.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": settings.SLACK_CLIENT_ID,
                    "client_secret": settings.SLACK_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=(3, 20),
            )
            response.raise_for_status()
            token_data = response.json()
        except requests.RequestException as exc:
            raise SlackDmMirrorUpstreamError(
                f"Slack token refresh failed: {exc.__class__.__name__}"
            ) from exc
        except ValueError as exc:
            raise SlackDmMirrorUpstreamError(
                "Slack token refresh returned invalid JSON."
            ) from exc
        if not isinstance(token_data, dict) or not token_data.get("ok", False):
            raise SlackDmMirrorCredentialError(
                str(
                    (token_data or {}).get("error")
                    or "Slack token refresh was rejected."
                )
            )
        authed_user = (
            token_data.get("authed_user")
            if isinstance(token_data.get("authed_user"), dict)
            else {}
        )
        access_token = str(
            authed_user.get("access_token")
            or token_data.get("access_token")
            or ""
        ).strip()
        replacement_refresh_token = str(
            authed_user.get("refresh_token")
            or token_data.get("refresh_token")
            or ""
        ).strip()
        if not access_token or not replacement_refresh_token:
            raise SlackDmMirrorCredentialError(
                "Slack token refresh returned incomplete credentials."
            )
        returned_team = token_data.get("team")
        returned_team_id = (
            str(returned_team.get("id") or "").strip()
            if isinstance(returned_team, dict)
            else ""
        )
        returned_user_id = str(authed_user.get("id") or "").strip()
        if returned_team_id and returned_team_id != locked_grant.slack_workspace_id:
            raise SlackDmMirrorCredentialError(
                "Slack token refresh returned the wrong workspace."
            )
        if returned_user_id and returned_user_id != locked_grant.slack_user_id:
            raise SlackDmMirrorCredentialError(
                "Slack token refresh returned the wrong user."
            )
        advance_slack_oauth_generation_locked(
            locked_user,
            connections=locked_connections,
        )
        locked_connection.access_token = access_token
        locked_connection.refresh_token = replacement_refresh_token
        locked_connection.token_type = str(
            authed_user.get("token_type")
            or token_data.get("token_type")
            or locked_connection.token_type
            or "Bearer"
        )
        try:
            expires_in = max(
                1,
                int(authed_user.get("expires_in") or token_data.get("expires_in")),
            )
        except (TypeError, ValueError):
            raise SlackDmMirrorCredentialError(
                "Slack token refresh omitted its expiry."
            )
        locked_connection.token_expires_at = timezone.now() + timedelta(
            seconds=expires_in
        )
        raw_scope = str(authed_user.get("scope") or token_data.get("scope") or "")
        if raw_scope:
            locked_connection.scopes = sorted(
                {value.strip() for value in raw_scope.split(",") if value.strip()}
            )
        locked_connection.status = ExternalServiceConnectionStatus.CONNECTED
        locked_connection.last_error = ""
        locked_connection.save(
            update_fields=(
                "access_token",
                "refresh_token",
                "token_type",
                "token_expires_at",
                "scopes",
                "status",
                "last_error",
                "updated_at",
            )
        )
    return SlackDmMirrorGrant.objects.select_related("connection").get(pk=grant.pk)


def _slack_sdk_timeout_seconds() -> int:
    try:
        configured = int(
            float(getattr(settings, "SLACK_API_READ_TIMEOUT_SECONDS", 8) or 8)
        )
    except (TypeError, ValueError):
        configured = 8
    return max(1, min(configured, 30))


def _slack_grant_api_authority_is_current(
    grant: SlackDmMirrorGrant,
    connection: ExternalServiceConnection | None,
    authority: _SlackGrantApiAuthority,
    *,
    required_scopes: set[str] | frozenset[str],
) -> bool:
    current_token = str(connection.access_token or "").strip() if connection else ""
    current_scopes = _normalized_slack_scopes(connection.scopes) if connection else ()
    try:
        current_identity = _connection_identity(connection) if connection else ()
    except SlackDmMirrorError:
        current_identity = ()
    return bool(
        grant.status == SlackDmMirrorGrantStatus.ACTIVE
        and grant.revoked_at is None
        and grant.connection_id == authority.connection_id
        and grant.user_id == authority.user_id
        and _grant_consent_generation(grant) == authority.consent_generation
        and str(grant.consent_version or "") == authority.consent_version
        and str(grant.slack_workspace_id or "").strip() == authority.workspace_id
        and str(grant.slack_user_id or "").strip() == authority.slack_user_id
        and connection is not None
        and connection.user_id == authority.user_id
        and connection.provider == ExternalServiceProvider.SLACK
        and connection.status
        in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        and current_token
        and hmac.compare_digest(current_token, authority.access_token)
        and current_scopes == authority.scopes
        and set(required_scopes).issubset(set(current_scopes))
        and connection_slack_oauth_generation(connection) == authority.oauth_generation
        and current_identity == (authority.workspace_id, authority.slack_user_id)
    )


def _lock_slack_grant_api_authority(
    authority: _SlackGrantApiAuthority,
    *,
    required_scopes: set[str] | frozenset[str],
) -> tuple[SlackDmMirrorGrant, ExternalServiceConnection]:
    """Lock and revalidate one exact authority in the global privacy order."""

    get_user_model().objects.select_for_update().get(pk=authority.user_id)
    locked_grants = list(
        SlackDmMirrorGrant.objects.select_for_update()
        .filter(user_id=authority.user_id)
        .order_by("id")
    )
    grant = next(
        (item for item in locked_grants if item.pk == authority.grant_id),
        None,
    )
    if grant is None:
        raise SlackDmMirrorAuthorizationError(
            "Slack DM mirroring authority no longer exists."
        )
    connection = (
        ExternalServiceConnection.objects.select_for_update()
        .filter(
            pk=authority.connection_id,
            user_id=authority.user_id,
            provider=ExternalServiceProvider.SLACK,
        )
        .first()
    )
    authority_current = _slack_grant_api_authority_is_current(
        grant, connection, authority, required_scopes=required_scopes
    ) and (
        current_slack_oauth_generation(authority.user_id)
        == authority.oauth_generation
    )
    if not authority_current or connection is None:
        raise SlackDmMirrorAuthorizationError(
            "Slack consent changed before the private response could be stored."
        )
    return grant, connection


def _call_slack_with_grant_authority(
    authority: _SlackGrantApiAuthority,
    method: str,
    *,
    required_scopes: set[str] | frozenset[str],
    **kwargs,
) -> Any:
    """Make one bounded Slack call while holding the global user consent fence.

    Disconnect takes the same user -> all grants -> connection locks. It
    therefore either clears the credential before this method can validate it,
    or waits until the old-token call has completed. Each page enters a new
    lease, so a disconnect that wins between pages prevents the next request.
    """

    with transaction.atomic():
        _, connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=required_scopes,
        )
        client = WebClient(
            token=str(connection.access_token or "").strip(),
            timeout=_slack_sdk_timeout_seconds(),
        )
        return getattr(client, method)(**kwargs)


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
        .filter(
            user=user,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .order_by("-updated_at")
        .first()
    )
    if grant is None:
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
    authority = _capture_slack_grant_api_authority(grant)
    users: list[dict[str, str]] = []
    next_cursor = ""
    pages = 0
    while len(users) < result_limit and pages < 20:
        page_cursor = slack_cursor
        response = _call_slack_with_grant_authority(
            authority,
            "users_list",
            required_scopes=DIRECT_DM_SCOPES,
            limit=200,
            cursor=page_cursor,
        )
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


def _store_conversation_membership_intent(
    grant_id: int,
    *,
    authority: _SlackGrantApiAuthority,
    required_scopes: set[str] | frozenset[str],
    slack_conversation_id: str,
    participant_slack_ids: list[str],
    participant_profiles: dict[str, dict[str, str]] | None = None,
) -> tuple[SlackDmMirrorConversation, bool]:
    """Expose a Slack membership snapshot only after fencing old authority."""

    normalized_ids = sorted(
        {
            str(value or "").strip()
            for value in participant_slack_ids
            if str(value or "").strip()
        }
    )
    with transaction.atomic():
        grant, _ = _lock_slack_grant_api_authority(
            authority,
            required_scopes=required_scopes,
        )
        if grant.pk != grant_id:
            raise SlackDmMirrorAuthorizationError(
                "Slack DM mirroring is no longer active."
            )
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(
                grant=grant,
                slack_conversation_id=slack_conversation_id,
            )
            .first()
        )
        created = conversation is None
        if conversation is None:
            conversation = SlackDmMirrorConversation.objects.create(
                grant=grant,
                slack_workspace_id=grant.slack_workspace_id,
                slack_conversation_id=slack_conversation_id,
                participant_slack_ids=normalized_ids,
                participant_profiles=participant_profiles or {},
            )
            return conversation, False

        conversation.grant = grant
        previous_ids = sorted(conversation.participant_slack_ids or [])
        membership_changed = previous_ids != normalized_ids
        if membership_changed:
            _prepare_conversation_registration_cleanup_locked(
                grant,
                conversation,
                reason="Slack private conversation membership changed",
            )
            now = timezone.now()
            _clear_history_scan_states([conversation.pk])
            private_deliveries = SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
            ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
            private_deliveries.update(encrypted_text="", updated_at=now)
            private_deliveries.filter(
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.PROCESSING,
                    CommunityBridgeDeliveryStatus.FAILED,
                )
            ).update(
                status=CommunityBridgeDeliveryStatus.DEAD,
                completed_at=None,
                last_error="Slack membership changed; awaiting private reprovision",
                updated_at=now,
            )
            conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
            conversation.mlai_channel_id = None
            conversation.history_backfilled_at = None
            conversation.oldest_synced_ts = ""
            conversation.latest_synced_ts = ""
            conversation.last_error = ""
        conversation.slack_workspace_id = grant.slack_workspace_id
        conversation.participant_slack_ids = normalized_ids
        if participant_profiles is not None:
            conversation.participant_profiles = participant_profiles
        elif membership_changed:
            existing_profiles = conversation.participant_profiles or {}
            conversation.participant_profiles = {
                slack_user_id: existing_profiles[slack_user_id]
                for slack_user_id in normalized_ids
                if slack_user_id in existing_profiles
            }
        conversation.save(
            update_fields=(
                "slack_workspace_id",
                "participant_slack_ids",
                "participant_profiles",
                "status",
                "mlai_channel_id",
                "history_backfilled_at",
                "oldest_synced_ts",
                "latest_synced_ts",
                "last_error",
                "updated_at",
            )
        )
    return conversation, not created and membership_changed


def _store_conversation_profiles(
    grant_id: int,
    conversation_id: int,
    *,
    authority: _SlackGrantApiAuthority,
    required_scopes: set[str] | frozenset[str],
    participant_slack_ids: list[str],
    participant_profiles: dict[str, dict[str, str]],
) -> SlackDmMirrorConversation:
    """Persist profiles only if the locked membership intent is still current."""

    expected_ids = sorted(participant_slack_ids)
    with transaction.atomic():
        grant, _ = _lock_slack_grant_api_authority(
            authority,
            required_scopes=required_scopes,
        )
        if grant.pk != grant_id:
            raise SlackDmMirrorAuthorizationError(
                "Slack DM mirroring is no longer active."
            )
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=conversation_id,
            grant=grant,
        )
        if sorted(conversation.participant_slack_ids or []) != expected_ids:
            raise SlackDmMirrorAuthorizationError(
                "Slack private conversation membership changed during profile lookup."
            )
        conversation.grant = grant
        conversation.participant_profiles = participant_profiles
        conversation.save(update_fields=("participant_profiles", "updated_at"))
        return conversation


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
    required_scopes = (
        DIRECT_DM_SCOPES
        if len(requested_ids) == 1
        else DIRECT_DM_SCOPES | GROUP_DM_SCOPES
    )
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
    grant = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(
            pk=grant.pk,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .first()
    )
    if grant is None:
        raise SlackDmMirrorAuthorizationError("Slack DM mirroring is no longer active.")
    if (
        grant.connection.provider != ExternalServiceProvider.SLACK
        or grant.connection.status
        not in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        or not str(grant.connection.access_token or "").strip()
        or not required_scopes.issubset(set(grant.connection.scopes or []))
        or _connection_identity(grant.connection)
        != (grant.slack_workspace_id, grant.slack_user_id)
    ):
        raise SlackDmMirrorError("Re-authorize Slack before starting this DM.")
    authority = _capture_slack_grant_api_authority(grant)
    profile_cache: dict[str, dict[str, str]] = {}
    for slack_user_id in requested_ids:
        response = _call_slack_with_grant_authority(
            authority,
            "users_info",
            required_scopes=required_scopes,
            user=slack_user_id,
        )
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
    open_response = _call_slack_with_grant_authority(
        authority,
        "conversations_open",
        required_scopes=required_scopes,
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
        authority,
        grant.slack_user_id,
        profile_cache,
        required_scopes=required_scopes,
    )
    participant_ids = sorted({grant.slack_user_id, *requested_ids})
    conversation, _ = _store_conversation_membership_intent(
        grant.pk,
        authority=authority,
        required_scopes=required_scopes,
        slack_conversation_id=channel_id,
        participant_slack_ids=participant_ids,
        participant_profiles={
            slack_user_id: profile_cache[slack_user_id]
            for slack_user_id in participant_ids
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
    grants = SlackDmMirrorGrant.objects.filter(user=user).select_related("connection")
    grant = (
        grants.filter(
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .order_by("-updated_at")
        .first()
        or grants.order_by("-updated_at").first()
    )
    connection = (
        grant.connection
        if grant is not None
        and grant.connection.status
        in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        else slack_connection_for_user(user)
    )
    conversations = SlackDmMirrorConversation.objects.none()
    if grant is not None:
        conversations = grant.conversations.all()
    backfill_conversations = conversations.filter(
        status=SlackDmMirrorConversationStatus.LIVE,
    )
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
        for participant_ids in backfill_conversations.values_list(
            "participant_slack_ids", flat=True
        )
        if max(0, len(participant_ids or []) - 1) + active_device_count > 9
    )
    backfill_deliveries = SlackDmMirrorDelivery.objects.none()
    if grant is not None:
        backfill_deliveries = SlackDmMirrorDelivery.objects.filter(
            conversation__grant=grant,
            conversation__status=SlackDmMirrorConversationStatus.LIVE,
            source_platform=CommunityBridgePlatform.SLACK,
            metadata__backfill=True,
        ).filter(
            Q(metadata__history_recovery_superseded__isnull=True)
            | Q(metadata__history_recovery_superseded=False)
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
        backfill_conversations.filter(history_backfilled_at__isnull=False)
        .exclude(
            id__in=incomplete_conversation_ids,
        )
        .count()
    )
    backfill_counts = {
        "complete": complete,
        "pending": backfill_conversations.count() - complete,
        "imported_messages": backfill_deliveries.filter(
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
        .filter(
            Q(metadata__history_outside_window__isnull=True)
            | Q(metadata__history_outside_window=False)
        )
        .count(),
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
    history_days = _bounded_history_days(grant.history_days if grant else None)
    return {
        "connected": connection is not None,
        "needs_reauthorization": bool(
            (grant is not None or connection is not None)
            and (
                connection is None
                or not REQUIRED_SCOPES.issubset(set(connection.scopes or []))
                or connection.status
                not in (
                    ExternalServiceConnectionStatus.CONNECTED,
                    ExternalServiceConnectionStatus.SYNCING,
                )
                or not str(connection.access_token or "").strip()
            )
        ),
        "enabled": bool(grant and grant.status == SlackDmMirrorGrantStatus.ACTIVE),
        "status": grant.status if grant else "not_connected",
        "workspace_name": connection.account_label if connection else "",
        "workspace_id": grant.slack_workspace_id if grant else "",
        "slack_user_id": grant.slack_user_id if grant else "",
        "history_days": history_days,
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
            "history_is_bounded": True,
            "full_history": False,
        },
    }


def _complete_registration_cleanup_before_activation(
    grant_id: int,
    *,
    retire_current: bool = False,
) -> None:
    """Finish prior-consent cleanup before a grant can become active again.

    A fresh OAuth consent advances the consent generation.  Reconcile every
    uncertain attempt first, then adopt known current registrations into the
    new generation.  A plain pause/resume keeps those registrations unchanged.
    """

    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant_id)
        conversations = list(
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(grant=grant)
            .order_by("id")
        )
        if grant.status == SlackDmMirrorGrantStatus.REVOKED:
            _prepare_registration_cleanup_locked(
                grant,
                conversations,
                reason="Complete prior Slack DM consent cleanup before reactivation",
            )
        elif retire_current:
            _prepare_generation_transition_locked(grant, conversations)
        pending = _registration_cleanup_pending_locked(grant.pk)
    if pending:
        _reconcile_registration_cleanup(grant_id, raise_on_pending=True)
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant_id)
        if _registration_cleanup_pending_locked(grant.pk):
            raise SlackDmMirrorError(
                "Previous MLAI Chat private registration cleanup is still pending."
            )


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
    if existing_grant is not None:
        _complete_registration_cleanup_before_activation(
            existing_grant.pk,
            retire_current=True,
        )

    activation_error: SlackDmMirrorError | None = None
    with transaction.atomic():
        # The user row is the first-activation serialization point: the
        # disconnect endpoint takes the same lock even when no grant exists
        # yet, so a successful disconnect cannot be followed by a stale grant
        # INSERT from an OAuth callback that was already in flight.
        get_user_model().objects.select_for_update().get(pk=connection.user_id)
        grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(
                slack_workspace_id=workspace_id,
                slack_user_id=slack_user_id,
            )
            .first()
        )
        locked_connection = (
            ExternalServiceConnection.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(pk=connection.pk)
        )
        if (
            locked_connection.provider != ExternalServiceProvider.SLACK
            or locked_connection.user_id != connection.user_id
            or locked_connection.status
            not in (
                ExternalServiceConnectionStatus.CONNECTED,
                ExternalServiceConnectionStatus.SYNCING,
            )
            or not str(locked_connection.access_token or "").strip()
            or not DIRECT_DM_SCOPES.issubset(set(locked_connection.scopes or []))
            or _connection_identity(locked_connection) != (workspace_id, slack_user_id)
        ):
            raise SlackDmMirrorError(
                "Slack was disconnected or changed while DM mirroring was activating."
            )
        # Another activation using this connection may have created the grant
        # while this call waited for the connection row.
        if grant is None:
            grant = (
                SlackDmMirrorGrant.objects.select_for_update()
                .filter(
                    slack_workspace_id=workspace_id,
                    slack_user_id=slack_user_id,
                )
                .first()
            )
        if grant is not None and grant.user_id != locked_connection.user_id:
            raise SlackDmMirrorError(
                "This Slack identity is already linked to another MLAI account."
            )
        if _preferred_device(locked_connection.user_id) is None:
            raise SlackDmMirrorError(
                "Verify an MLAI Chat device before linking Slack DMs."
            )
        now = timezone.now()
        if grant is None:
            grant = SlackDmMirrorGrant.objects.create(
                user=locked_connection.user,
                connection=locked_connection,
                slack_workspace_id=workspace_id,
                slack_user_id=slack_user_id,
                status=SlackDmMirrorGrantStatus.ACTIVE,
                consent_version=SlackDmMirrorGrant.CONSENT_VERSION,
                history_days=_history_days(),
                consented_at=now,
            )
        else:
            conversations = list(
                SlackDmMirrorConversation.objects.select_for_update()
                .filter(grant=grant)
                .order_by("id")
            )
            # Close the gap after the network-free preflight: an older
            # provision call may have completed with an ambiguous POST result
            # while this activation waited for the grant lock. Fence that
            # exact prior-generation attempt and commit the fence before
            # reporting that renewed consent must wait.
            _prepare_generation_transition_locked(grant, conversations)
            if _registration_cleanup_pending_locked(grant.pk):
                activation_error = SlackDmMirrorError(
                    "Previous MLAI Chat private registration cleanup is still pending."
                )
            else:
                grant.user = locked_connection.user
                grant.connection = locked_connection
                grant.status = SlackDmMirrorGrantStatus.ACTIVE
                grant.consent_version = SlackDmMirrorGrant.CONSENT_VERSION
                grant.consented_at = now
                grant.paused_at = None
                grant.revoked_at = None
                grant.last_error = ""
                grant.save(
                    update_fields=(
                        "user",
                        "connection",
                        "status",
                        "consent_version",
                        "consented_at",
                        "paused_at",
                        "revoked_at",
                        "last_error",
                        "updated_at",
                    )
                )
                _normalize_grant_history_window_locked(grant)
                _adopt_current_registration_generation_locked(grant)
                if _registration_cleanup_pending_locked(grant.pk):
                    activation_error = SlackDmMirrorError(
                        "Private registrations changed while Slack consent was being renewed."
                    )
                else:
                    _restart_incomplete_history_scans_locked(conversations)
    if activation_error is not None:
        raise activation_error
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
    _complete_registration_cleanup_before_activation(grant.pk)
    resume_error: SlackDmMirrorError | None = None
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant.pk)
        connection = (
            ExternalServiceConnection.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(pk=grant.connection_id)
        )
        if (
            connection.provider != ExternalServiceProvider.SLACK
            or connection.user_id != grant.user_id
            or connection.status
            not in (
                ExternalServiceConnectionStatus.CONNECTED,
                ExternalServiceConnectionStatus.SYNCING,
            )
            or not str(connection.access_token or "").strip()
            or not DIRECT_DM_SCOPES.issubset(set(connection.scopes or []))
            or _connection_identity(connection)
            != (grant.slack_workspace_id, grant.slack_user_id)
        ):
            raise SlackDmMirrorError("Re-authorize Slack before resuming DM mirroring.")
        conversations = list(
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(grant=grant)
            .order_by("id")
        )
        _prepare_generation_transition_locked(grant, conversations)
        if _registration_cleanup_pending_locked(grant.pk):
            resume_error = SlackDmMirrorError(
                "Previous MLAI Chat private registration cleanup is still pending."
            )
        else:
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
            _normalize_grant_history_window_locked(grant)
    if resume_error is not None:
        raise resume_error


@transaction.atomic
def backfill_grant(
    grant: SlackDmMirrorGrant,
    *,
    full_history: bool = False,
) -> int:
    """Mark owner mirrors for a paced, idempotent history re-scan."""

    grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant.pk)
    if grant.status != SlackDmMirrorGrantStatus.ACTIVE or grant.revoked_at is not None:
        raise SlackDmMirrorError("Resume Slack mirroring before starting a backfill.")
    # `full_history` is retained only so older MLAI Chat releases can keep
    # calling this method while they upgrade. An owner-triggered import is
    # always bounded now; a legacy `backfill_all` request must never remove
    # Slack's `oldest` timestamp.
    grant.history_days = _history_days()
    grant.last_discovery_at = None
    grant.save(update_fields=("history_days", "last_discovery_at", "updated_at"))
    now = timezone.now()
    conversations = list(
        SlackDmMirrorConversation.objects.select_for_update()
        .filter(
            grant=grant,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        .order_by("id")
    )
    conversation_ids = [conversation.pk for conversation in conversations]
    _clear_history_scan_states(conversation_ids)
    _clear_permanent_recovery_fences_locked(conversation_ids)
    for conversation in conversations:
        conversation.grant = grant
        _mark_history_reconciliation_candidates_locked(conversation)
    SlackDmMirrorConversation.objects.filter(pk__in=conversation_ids).update(
        history_backfilled_at=None,
        oldest_synced_ts="",
        latest_synced_ts="",
        last_error="",
        updated_at=now,
    )
    _mark_backfill_rows_for_recovery_locked(conversation_ids, now=now)
    return len(conversation_ids)


@transaction.atomic
def _schedule_automatic_history_reconciliation(
    grant: SlackDmMirrorGrant,
    *,
    reason: str,
) -> int:
    """Queue current-state scans without clearing explicit recovery fences."""

    grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant.pk)
    if grant.status != SlackDmMirrorGrantStatus.ACTIVE or grant.revoked_at is not None:
        return 0
    grant.last_discovery_at = None
    grant.save(update_fields=("last_discovery_at", "updated_at"))
    conversations = list(
        SlackDmMirrorConversation.objects.select_for_update()
        .filter(
            grant=grant,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        .order_by("id")
    )
    for conversation in conversations:
        conversation.grant = grant
        _mark_conversation_history_due(
            conversation,
            reason=reason,
            reset_deliveries=False,
            reconcile_current_state=True,
        )
    return len(conversations)


def _reconcile_registration_cleanup(
    grant_id: int,
    *,
    raise_on_pending: bool,
    limit: int = 100,
) -> None:
    try:
        _reconcile_registration_cleanup_ledger(
            grant_id,
            raise_on_pending=raise_on_pending,
            limit=limit,
        )
    except RegistrationCleanupPending as exc:
        raise SlackDmMirrorError(str(exc)) from exc


def _clear_slack_connection_locked(
    connection: ExternalServiceConnection,
) -> str:
    """Erase a locked Slack credential row and return its prior token."""

    access_token = str(connection.access_token or "").strip()
    # Import lazily: startup-update services also use connector helpers. The
    # caller already holds user/grant/connection locks, and cleanup continues
    # in canonical order by locking run/output rows after them.
    from startup_updates.services import (
        clear_slack_connection_startup_lineage_locked,
    )

    clear_slack_connection_startup_lineage_locked(connection)
    # A generic connector page may have completed while DELETE was queued on
    # this authority lock. Remove response-derived data before the disconnect
    # returns so that winning either side of the race has one privacy outcome.
    SlackThreadArtifact.objects.filter(connection=connection).delete()
    SlackMessageArtifact.objects.filter(connection=connection).delete()
    SlackChannelSelection.objects.filter(connection=connection).delete()
    connection.status = ExternalServiceConnectionStatus.DISCONNECTED
    connection.access_token = ""
    connection.refresh_token = ""
    connection.last_error = ""
    disconnect_generation = (connection.provider_metadata or {}).get(
        SLACK_OAUTH_GENERATION_KEY
    )
    connection.provider_metadata = (
        {SLACK_OAUTH_GENERATION_KEY: disconnect_generation}
        if disconnect_generation is not None
        else {}
    )
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
    return access_token


def _revoke_grant_locally_locked(
    grant: SlackDmMirrorGrant,
    *,
    now,
) -> tuple[ExternalServiceConnection, str]:
    """Apply the local privacy boundary while the user and grant are locked."""

    connection = ExternalServiceConnection.objects.select_for_update().get(
        pk=grant.connection_id
    )
    conversations = list(
        SlackDmMirrorConversation.objects.select_for_update()
        .filter(grant=grant)
        .order_by("id")
    )
    grant.status = SlackDmMirrorGrantStatus.REVOKED
    grant.revoked_at = now
    grant.last_error = ""
    grant.save(update_fields=("status", "revoked_at", "last_error", "updated_at"))
    CommunityBridgeIdentityLink.objects.filter(
        slack_workspace_id=grant.slack_workspace_id,
        slack_user_id=grant.slack_user_id,
        revoked_at__isnull=True,
    ).update(
        revoked_at=now,
        revocation_reason="Slack DM mirroring disconnected",
    )
    conversation_ids = [conversation.pk for conversation in conversations]
    for conversation in conversations:
        conversation.status = SlackDmMirrorConversationStatus.PAUSED
        conversation.save(update_fields=("status", "updated_at"))
    _prepare_registration_cleanup_locked(
        grant,
        conversations,
        reason="Slack DM mirroring consent was revoked",
    )
    private_deliveries = SlackDmMirrorDelivery.objects.filter(
        conversation_id__in=conversation_ids,
    ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
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
    return connection, _clear_slack_connection_locked(connection)


def _revoke_remote_token(connection_id: int, access_token: str) -> None:
    if not access_token:
        return
    try:
        WebClient(token=access_token).auth_revoke()
    except Exception as exc:
        # Local revocation is the privacy boundary. Slack may already have
        # revoked the token, so a remote error must not retain local access.
        logger.warning(
            "slack_dm_mirror_remote_revoke_failed connection_id=%s error=%s",
            connection_id,
            exc.__class__.__name__,
        )


def _finish_grant_registration_revoke(grant_id: int) -> None:
    try:
        # Revocation's synchronous privacy boundary is the committed local
        # grant/token/body fence above. Adapter registrations contain only
        # public keys and channel authority, so attempt one unregister here and
        # leave the durable content-free ledger for the periodic reconciler.
        # A large account or temporary adapter outage
        # must not turn an already-effective local disconnect into a 5xx that
        # the caller can no longer authenticate to retry.
        _reconcile_registration_cleanup(
            grant_id,
            raise_on_pending=False,
            limit=1,
        )
    except Exception as exc:
        # The content-free ledger remains the source of truth for every known
        # and ambiguous registration. Retrying this operation or the worker's
        # reconciliation is safe because adapter unregistration is idempotent.
        logger.warning(
            "slack_dm_mirror_adapter_unregister_failed grant_id=%s error=%s",
            grant_id,
            exc.__class__.__name__,
        )


def revoke_grant(grant: SlackDmMirrorGrant) -> None:
    now = timezone.now()
    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(pk=grant.user_id)
        locked_grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant.pk)
        advance_slack_oauth_generation_locked(locked_user)
        connection, access_token = _revoke_grant_locally_locked(
            locked_grant,
            now=now,
        )
    _revoke_remote_token(connection.pk, access_token)
    _finish_grant_registration_revoke(locked_grant.pk)


def revoke_user_grant(user) -> None:
    """Disconnect all of a user's Slack mirrors and pre-grant credentials."""

    now = timezone.now()
    connection_tokens: list[tuple[int, str]] = []
    grant_ids: list[int] = []
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        grants = list(
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(user_id=user.pk)
            .order_by("id")
        )
        advance_slack_oauth_generation_locked(user)
        cleared_connection_ids: set[int] = set()
        for grant in grants:
            connection, access_token = _revoke_grant_locally_locked(grant, now=now)
            connection_tokens.append((connection.pk, access_token))
            cleared_connection_ids.add(connection.pk)
            grant_ids.append(grant.pk)
        # OAuth may have stored another credential while activation is between
        # preflight and INSERT. Clearing every remaining locked Slack
        # connection is a durable tombstone that activation rechecks under the
        # same user lock before it can create a grant.
        connections = list(
            ExternalServiceConnection.objects.select_for_update()
            .filter(user_id=user.pk, provider=ExternalServiceProvider.SLACK)
            .exclude(pk__in=cleared_connection_ids)
            .order_by("id")
        )
        connection_tokens.extend(
            (connection.pk, _clear_slack_connection_locked(connection))
            for connection in connections
        )
    for connection_id, access_token in connection_tokens:
        _revoke_remote_token(connection_id, access_token)
    for grant_id in grant_ids:
        _finish_grant_registration_revoke(grant_id)


def revoke_connection_grant(user, connection_id: int) -> None:
    """Disconnect every Slack authority exposed through the singular UI.

    Connector management is addressed by connection id, but Community Home has
    one Slack consent toggle and one status surface. Validate that the selected
    connection belongs to the caller, then close every grant/credential so an
    older ACTIVE grant cannot keep ingesting private DMs invisibly.
    """

    if not ExternalServiceConnection.objects.filter(
        pk=connection_id,
        user_id=user.pk,
        provider=ExternalServiceProvider.SLACK,
    ).exists():
        raise ExternalServiceConnection.DoesNotExist
    revoke_user_grant(user)


def _retire_ineligible_conversation(
    grant_id: int,
    slack_conversation_id: str,
    *,
    reason: str,
    reconcile_cleanup: bool = True,
    not_updated_after=None,
) -> bool:
    """Fence and erase one mirror whose Slack conversation lost eligibility.

    The local transaction is the privacy boundary: it serializes with private
    queue writers on grant -> conversation, records adapter cleanup before
    clearing the channel authority, and erases every non-control body. Adapter
    I/O is deliberately reconciled only after those locks have been released.
    """

    channel_id = str(slack_conversation_id or "").strip()
    if not channel_id:
        return False
    retirement_reason = str(reason or "Slack conversation is not eligible")[:2000]
    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(pk=grant_id)
            .first()
        )
        if grant is None:
            return False
        conversation_query = SlackDmMirrorConversation.objects.select_for_update().filter(
            grant=grant,
            slack_conversation_id=channel_id,
        )
        if not_updated_after is not None:
            conversation_query = conversation_query.filter(
                updated_at__lte=not_updated_after,
            ).exclude(deliveries__created_at__gt=not_updated_after)
        conversation = conversation_query.first()
        if conversation is None:
            return False

        _prepare_conversation_registration_cleanup_locked(
            grant,
            conversation,
            reason=retirement_reason,
        )
        _clear_history_scan_states([conversation.pk])
        now = timezone.now()
        SlackDmMirrorDelivery.objects.filter(conversation=conversation).exclude(
            source_message_id__startswith=REGISTRATION_STATE_PREFIX,
        ).update(
            encrypted_text="",
            status=CommunityBridgeDeliveryStatus.DEAD,
            completed_at=None,
            last_error=retirement_reason,
            updated_at=now,
        )

        # Registration rows retain the exact content-free deletion intent.
        # Everything that could authorize or resume private message movement is
        # removed from the conversation itself before the lock is released.
        conversation.status = SlackDmMirrorConversationStatus.PAUSED
        conversation.mlai_channel_id = None
        conversation.participant_slack_ids = []
        conversation.participant_buzz_pubkeys = []
        conversation.participant_identity_map = {}
        conversation.participant_profiles = {}
        conversation.participant_hash = ""
        conversation.oldest_synced_ts = ""
        conversation.latest_synced_ts = ""
        conversation.history_backfilled_at = None
        conversation.last_synced_at = None
        conversation.last_error = retirement_reason
        conversation.save(
            update_fields=(
                "status",
                "mlai_channel_id",
                "participant_slack_ids",
                "participant_buzz_pubkeys",
                "participant_identity_map",
                "participant_profiles",
                "participant_hash",
                "oldest_synced_ts",
                "latest_synced_ts",
                "history_backfilled_at",
                "last_synced_at",
                "last_error",
                "updated_at",
            )
        )

    if reconcile_cleanup:
        _reconcile_registration_cleanup(grant_id, raise_on_pending=False)
    return True


def _retire_ineligible_from_slack_response(
    authority: _SlackGrantApiAuthority,
    slack_conversation_id: str,
    *,
    reason: str,
    required_scopes: set[str] | frozenset[str],
    not_updated_after=None,
) -> bool:
    """Apply a response-derived retirement only to its exact consent epoch."""

    with transaction.atomic():
        grant, _ = _lock_slack_grant_api_authority(
            authority,
            required_scopes=required_scopes,
        )
        if grant.pk != authority.grant_id:
            raise SlackDmMirrorAuthorizationError(
                "Slack DM mirroring authority changed during discovery."
            )
        return _retire_ineligible_conversation(
            grant.pk,
            slack_conversation_id,
            reason=reason,
            reconcile_cleanup=False,
            not_updated_after=not_updated_after,
        )


def _discovery_checkpoint_identity(
    authority: _SlackGrantApiAuthority,
) -> dict[str, Any]:
    return {
        "grant_id": authority.grant_id,
        "consent_generation": authority.consent_generation,
        "oauth_generation": authority.oauth_generation,
        "workspace_id": authority.workspace_id,
        "slack_user_id": authority.slack_user_id,
    }


def _load_discovery_checkpoint(
    authority: _SlackGrantApiAuthority,
) -> tuple[str, set[str], list[str], Any]:
    fresh_started_at = timezone.now()
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        if grant.pk != authority.grant_id:
            raise SlackDmMirrorAuthorizationError(
                "Slack DM discovery authority changed."
            )
        raw = (connection.sync_cursor or {}).get(DISCOVERY_CHECKPOINT_KEY)
        if not isinstance(raw, dict):
            return "", set(), [], fresh_started_at
        identity = _discovery_checkpoint_identity(authority)
        if any(raw.get(key) != value for key, value in identity.items()):
            return "", set(), [], fresh_started_at
        cursor = str(raw.get("cursor") or "").strip()
        raw_seen = raw.get("seen_channel_ids")
        raw_failures = raw.get("failures")
        started_at = parse_datetime(str(raw.get("started_at") or ""))
        if (
            len(cursor) > 1000
            or not isinstance(raw_seen, list)
            or not isinstance(raw_failures, list)
            or started_at is None
            or timezone.is_naive(started_at)
            or started_at > fresh_started_at + timedelta(minutes=5)
        ):
            return "", set(), [], fresh_started_at
        if len(raw_seen) > MAX_DISCOVERY_CONVERSATIONS:
            raise SlackDmMirrorError(
                "Slack discovery checkpoint exceeds the supported conversation limit."
            )
        seen = {
            str(value or "").strip()
            for value in raw_seen
            if 0 < len(str(value or "").strip()) <= 100
        }
        failures = [
            str(value or "")[:500]
            for value in raw_failures[:100]
            if str(value or "").strip()
        ]
        return cursor, seen, failures, started_at


def _save_discovery_checkpoint(
    authority: _SlackGrantApiAuthority,
    *,
    cursor: str,
    seen_channel_ids: set[str],
    failures: list[str],
    started_at,
) -> None:
    if len(seen_channel_ids) > MAX_DISCOVERY_CONVERSATIONS:
        raise SlackDmMirrorError(
            "Slack discovery exceeds the supported conversation limit."
        )
    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        if grant.pk != authority.grant_id:
            raise SlackDmMirrorAuthorizationError(
                "Slack DM discovery authority changed."
            )
        sync_cursor = dict(connection.sync_cursor or {})
        sync_cursor[DISCOVERY_CHECKPOINT_KEY] = {
            **_discovery_checkpoint_identity(authority),
            "cursor": str(cursor or "")[:1000],
            "seen_channel_ids": sorted(seen_channel_ids),
            "failures": [str(value or "")[:500] for value in failures[-100:]],
            "started_at": started_at.isoformat(),
        }
        connection.sync_cursor = sync_cursor
        connection.save(update_fields=("sync_cursor", "updated_at"))


def _clear_discovery_checkpoint_locked(
    connection: ExternalServiceConnection,
) -> None:
    sync_cursor = dict(connection.sync_cursor or {})
    if DISCOVERY_CHECKPOINT_KEY not in sync_cursor:
        return
    sync_cursor.pop(DISCOVERY_CHECKPOINT_KEY, None)
    connection.sync_cursor = sync_cursor
    connection.save(update_fields=("sync_cursor", "updated_at"))


def _pending_event_key(channel_id: str, normalized: dict[str, Any]) -> str:
    material = "\0".join(
        (
            channel_id,
            str(normalized.get("source_message_id") or ""),
            str(normalized.get("operation") or ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _stage_unknown_slack_event(
    workspace_id: str,
    channel_id: str,
    normalized: dict[str, Any],
    *,
    authorized_user_ids: set[str],
) -> int:
    """Durably retain an unknown-DM event, encrypted, until discovery owns it."""

    staged = 0
    if not authorized_user_ids:
        raise SlackDmMirrorError(
            "Slack event recipient authority is missing; retry the webhook."
        )
    candidates = list(
        SlackDmMirrorGrant.objects.select_related("connection").filter(
            slack_workspace_id=workspace_id,
            slack_user_id__in=authorized_user_ids,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
    )
    event_key = _pending_event_key(channel_id, normalized)
    ciphertext = encrypt_credential_value(
        json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    )
    for candidate in candidates:
        authority = _capture_slack_grant_api_authority(
            candidate,
            refresh_token=False,
        )
        with transaction.atomic():
            grant, connection = _lock_slack_grant_api_authority(
                authority,
                required_scopes=DIRECT_DM_SCOPES,
            )
            sync_cursor = dict(connection.sync_cursor or {})
            raw_pending = sync_cursor.get(PENDING_EVENT_CHECKPOINT_KEY, [])
            pending = list(raw_pending) if isinstance(raw_pending, list) else []
            if not any(
                isinstance(item, dict) and item.get("key") == event_key
                for item in pending
            ):
                if len(pending) >= MAX_PENDING_UNKNOWN_EVENTS:
                    raise SlackDmMirrorError(
                        "Slack pending-event queue is full; retry the webhook."
                    )
                pending.append(
                    {
                        "key": event_key,
                        "channel_id": channel_id,
                        "ciphertext": ciphertext,
                        "staged_at": timezone.now().isoformat(),
                    }
                )
                sync_cursor[PENDING_EVENT_CHECKPOINT_KEY] = pending
                connection.sync_cursor = sync_cursor
                connection.save(update_fields=("sync_cursor", "updated_at"))
                staged += 1
            grant.last_discovery_at = None
            grant.save(update_fields=("last_discovery_at", "updated_at"))
    return staged


def _slack_event_authorized_user_ids(payload: dict[str, Any]) -> set[str]:
    user_ids: set[str] = set()
    for raw in payload.get("authorizations") or []:
        if not isinstance(raw, dict):
            continue
        user_id = str(raw.get("user_id") or "").strip()
        if user_id:
            user_ids.add(user_id)
    for key in ("authorized_users", "authed_users"):
        for raw_user_id in payload.get(key) or []:
            user_id = str(raw_user_id or "").strip()
            if user_id:
                user_ids.add(user_id)
    return user_ids


def _discard_staged_events_locked(
    connection: ExternalServiceConnection,
    *,
    channel_ids: set[str],
) -> None:
    sync_cursor = dict(connection.sync_cursor or {})
    raw_pending = sync_cursor.get(PENDING_EVENT_CHECKPOINT_KEY, [])
    if not isinstance(raw_pending, list):
        sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
    else:
        pending = [
            item
            for item in raw_pending
            if not (
                isinstance(item, dict)
                and str(item.get("channel_id") or "") in channel_ids
            )
        ]
        if pending:
            sync_cursor[PENDING_EVENT_CHECKPOINT_KEY] = pending
        else:
            sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
    connection.sync_cursor = sync_cursor
    connection.save(update_fields=("sync_cursor", "updated_at"))


def _discard_staged_events_for_channel(
    authority: _SlackGrantApiAuthority,
    channel_id: str,
) -> None:
    with transaction.atomic():
        _, connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        _discard_staged_events_locked(connection, channel_ids={channel_id})


def _staged_slack_channel_ids(
    connection: ExternalServiceConnection,
) -> set[str]:
    raw_pending = (connection.sync_cursor or {}).get(
        PENDING_EVENT_CHECKPOINT_KEY,
        [],
    )
    if not isinstance(raw_pending, list):
        return set()
    return {
        str(item.get("channel_id") or "").strip()
        for item in raw_pending
        if isinstance(item, dict) and str(item.get("channel_id") or "").strip()
    }


def _slack_conversation_activity_seconds(raw: dict[str, Any]) -> int | None:
    """Return Slack's explicit latest-message marker, if one is present.

    Slack's conversation ``updated`` field describes channel metadata rather
    than reliably proving the time of the latest message. Treating it as
    message activity can incorrectly skip an active DM forever. Missing
    ``latest`` therefore fails open to the bounded ``conversations.history``
    request, whose ``oldest`` parameter remains the privacy boundary.
    """

    latest = raw.get("latest")
    if isinstance(latest, dict):
        latest = latest.get("ts")
    latest_text = str(latest or "").strip()
    if latest_text:
        try:
            return _slack_ts_sort_key(latest_text)[0]
        except SlackDmMirrorError:
            pass
    return None


def _slack_conversation_is_recent(
    raw: dict[str, Any],
    *,
    activity_cutoff: int,
    staged_channel_ids: set[str],
) -> bool:
    channel_id = str(raw.get("id") or "").strip()
    if channel_id in staged_channel_ids:
        return True
    activity_seconds = _slack_conversation_activity_seconds(raw)
    # Missing activity is deliberately fail-open. The bounded history query is
    # the authoritative fallback and still cannot import content before cutoff.
    return activity_seconds is None or activity_seconds >= activity_cutoff


def _embedded_conversation_participant_ids(
    raw_channels: list[dict[str, Any]],
    *,
    owner_slack_user_id: str,
) -> set[str]:
    participant_ids = {owner_slack_user_id}
    for raw in raw_channels:
        direct_user_id = str(raw.get("user") or "").strip()
        if direct_user_id:
            participant_ids.add(direct_user_id)
        for value in raw.get("members") or []:
            user_id = str(value or "").strip()
            if user_id:
                participant_ids.add(user_id)
    return participant_ids


def _preload_slack_profiles(
    authority: _SlackGrantApiAuthority,
    participant_ids: set[str],
    cache: dict[str, dict[str, str]],
) -> None:
    missing_ids = {user_id for user_id in participant_ids if user_id not in cache}
    if len(missing_ids) < PROFILE_BULK_PRELOAD_THRESHOLD:
        return
    cursor = ""
    seen_cursors: set[str] = set()
    while missing_ids:
        response = _call_slack_with_grant_authority(
            authority,
            "users_list",
            required_scopes=DIRECT_DM_SCOPES,
            limit=200,
            cursor=cursor,
        )
        members = response.get("members")
        # Profile preloading is only an optimization. If Slack returns a
        # partial/unexpected page, leave the missing users to the established
        # per-user lookup path instead of failing discovery.
        if not isinstance(members, list):
            return
        for user in members:
            if not isinstance(user, dict):
                continue
            user_id = str(user.get("id") or "").strip()
            if user_id not in missing_ids:
                continue
            cache[user_id] = _profile_from_slack_user(user)
            missing_ids.discard(user_id)
        next_cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if not next_cursor:
            return
        if next_cursor in seen_cursors:
            raise SlackDmMirrorError("Slack user pagination made no progress.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _complete_inactive_conversation_history(
    authority: _SlackGrantApiAuthority,
    *,
    grant_id: int,
    channel_id: str,
) -> None:
    """Finish an explicitly requested scan whose latest activity is too old."""

    with transaction.atomic():
        grant, _ = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        if grant.pk != grant_id:
            return
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(
                grant=grant,
                slack_conversation_id=channel_id,
                status=SlackDmMirrorConversationStatus.LIVE,
            )
            .first()
        )
        if conversation is None or conversation.history_backfilled_at is not None:
            return
        rows = list(
            SlackDmMirrorDelivery.objects.select_for_update()
            .filter(
                conversation=conversation,
                metadata__backfill=True,
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.PROCESSING,
                    CommunityBridgeDeliveryStatus.FAILED,
                    CommunityBridgeDeliveryStatus.DEAD,
                ),
            )
            .order_by("id")
        )
        now = timezone.now()
        expired_rows = [
            row
            for row in rows
            if _backfill_delivery_is_outside_history_window(
                row,
                now=now,
                history_days=grant.history_days,
            )
        ]
        for row in expired_rows:
            _complete_outside_history_window_delivery_locked(row, now=now)
        _clear_history_scan_states([conversation.pk])
        conversation.history_backfilled_at = now
        conversation.oldest_synced_ts = ""
        conversation.latest_synced_ts = ""
        conversation.last_error = ""
        conversation.save(
            update_fields=(
                "history_backfilled_at",
                "oldest_synced_ts",
                "latest_synced_ts",
                "last_error",
                "updated_at",
            )
        )


def discover_conversations(
    grant: SlackDmMirrorGrant, *, force_backfill: bool = False
) -> int:
    """Discover owner-visible DMs with activity in the configured history window.

    Slack sometimes omits conversation activity metadata. Those conversations
    remain eligible so an incomplete list response can never hide a private DM.
    """

    _, identity_repaired, _ = ensure_owner_identity(
        grant,
        allow_preferred_fallback=True,
    )
    grant = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(
            pk=grant.pk,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
        .first()
    )
    if grant is None:
        raise SlackDmMirrorAuthorizationError("Slack DM mirroring is no longer active.")
    if (
        grant.connection.provider != ExternalServiceProvider.SLACK
        or grant.connection.status
        not in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        or not str(grant.connection.access_token or "").strip()
        or not DIRECT_DM_SCOPES.issubset(set(grant.connection.scopes or []))
        or _connection_identity(grant.connection)
        != (grant.slack_workspace_id, grant.slack_user_id)
    ):
        raise SlackDmMirrorError("Re-authorize Slack before discovering DMs.")
    authority = _capture_slack_grant_api_authority(grant)
    include_group_dms = GROUP_DM_SCOPES.issubset(set(grant.connection.scopes or []))
    conversation_types = "im,mpim" if include_group_dms else "im"
    (
        cursor,
        seen_channel_ids,
        failures,
        discovery_started_at,
    ) = _load_discovery_checkpoint(authority)
    discovered = 0
    profile_cache: dict[str, dict[str, str]] = {}
    for stored_profiles in grant.conversations.values_list(
        "participant_profiles", flat=True
    ):
        if not isinstance(stored_profiles, dict):
            continue
        for slack_user_id, profile in stored_profiles.items():
            if isinstance(profile, dict):
                profile_cache.setdefault(str(slack_user_id), profile)
    staged_channel_ids = _staged_slack_channel_ids(grant.connection)
    activity_cutoff = (
        int(time.time()) - _bounded_history_days(grant.history_days) * 86_400
    )
    while True:
        response = _call_slack_with_grant_authority(
            authority,
            "conversations_list",
            required_scopes=DIRECT_DM_SCOPES,
            types=conversation_types,
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        raw_channels = [
            raw for raw in response.get("channels") or [] if isinstance(raw, dict)
        ]
        raw_channels.sort(
            key=lambda raw: _slack_conversation_activity_seconds(raw) or -1,
            reverse=True,
        )
        eligible_channels = [
            raw
            for raw in raw_channels
            if _slack_conversation_is_recent(
                raw,
                activity_cutoff=activity_cutoff,
                staged_channel_ids=staged_channel_ids,
            )
        ]
        _preload_slack_profiles(
            authority,
            _embedded_conversation_participant_ids(
                eligible_channels,
                owner_slack_user_id=grant.slack_user_id,
            ),
            profile_cache,
        )
        for raw in raw_channels:
            if not isinstance(raw, dict):
                continue
            channel_id = str(raw.get("id") or "").strip()
            if not channel_id:
                continue
            seen_channel_ids.add(channel_id)
            if bool(raw.get("is_archived")):
                _retire_ineligible_from_slack_response(
                    authority,
                    channel_id,
                    reason=SLACK_CONVERSATION_UNAVAILABLE_REASON,
                    required_scopes=DIRECT_DM_SCOPES,
                )
                _discard_staged_events_for_channel(authority, channel_id)
                continue
            if _is_external_shared_conversation(raw):
                _retire_ineligible_from_slack_response(
                    authority,
                    channel_id,
                    reason=SLACK_CONNECT_INELIGIBLE_REASON,
                    required_scopes=DIRECT_DM_SCOPES,
                )
                _discard_staged_events_for_channel(authority, channel_id)
                continue
            conversation_is_recent = _slack_conversation_is_recent(
                raw,
                activity_cutoff=activity_cutoff,
                staged_channel_ids=staged_channel_ids,
            )
            inactive_existing_conversation = bool(
                not conversation_is_recent
                and SlackDmMirrorConversation.objects.filter(
                    grant=grant,
                    slack_conversation_id=channel_id,
                ).exists()
            )
            if not conversation_is_recent and not inactive_existing_conversation:
                # Do not create a destination or read participant profiles for
                # a DM Slack proves has no activity inside the import window.
                continue
            conversation = None
            try:
                conversation = _discover_conversation(
                    grant,
                    authority,
                    raw,
                    profile_cache=profile_cache,
                    force_backfill=force_backfill,
                    reset_history=identity_repaired,
                )
                if conversation is not None:
                    discovered += 1
                    if inactive_existing_conversation:
                        # Existing mirrors still refresh their device and
                        # participant boundary, but no out-of-window history is
                        # fetched or requeued.
                        _complete_inactive_conversation_history(
                            authority,
                            grant_id=grant.pk,
                            channel_id=channel_id,
                        )
                    else:
                        _drain_staged_events_for_conversation(
                            authority,
                            conversation.pk,
                        )
                else:
                    _discard_staged_events_for_channel(authority, channel_id)
            except Exception as exc:
                if _is_slack_auth_error(exc):
                    raise
                error_text = f"{exc.__class__.__name__}: {exc}"[:2000]
                failures.append(f"{channel_id or 'unknown'}: {error_text}")
                try:
                    with transaction.atomic():
                        locked_grant, _ = _lock_slack_grant_api_authority(
                            authority,
                            required_scopes=DIRECT_DM_SCOPES,
                        )
                        locked_conversation = None
                        if locked_grant.pk == grant.pk:
                            locked_conversation = (
                                SlackDmMirrorConversation.objects.select_for_update()
                                .filter(
                                    grant=locked_grant,
                                    slack_conversation_id=channel_id,
                                )
                                .first()
                            )
                        if locked_conversation is not None:
                            locked_conversation.last_error = error_text
                            # A transient refresh failure must not take an already-live
                            # DM offline, and a revoked mirror must remain paused.
                            if locked_conversation.status not in (
                                SlackDmMirrorConversationStatus.LIVE,
                                SlackDmMirrorConversationStatus.PAUSED,
                            ):
                                locked_conversation.status = (
                                    SlackDmMirrorConversationStatus.ERROR
                                )
                            locked_conversation.save(
                                update_fields=("status", "last_error", "updated_at")
                            )
                except SlackDmMirrorAuthorizationError:
                    # Consent changed after the response. Do not save the old
                    # error and do not issue another Slack page under that token.
                    return discovered
                logger.warning(
                    "slack_dm_mirror_conversation_discovery_failed "
                    "grant_id=%s conversation_id=%s error=%s",
                    grant.pk,
                    channel_id,
                    exc,
                )
        next_cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if next_cursor and next_cursor == cursor:
            raise SlackDmMirrorError("Slack discovery pagination made no progress.")
        cursor = next_cursor
        if cursor:
            # One bounded Slack list page per worker tick. Persist the exact
            # consent epoch and cumulative seen set before returning so a rate
            # limit or process restart resumes rather than starving late DMs.
            _save_discovery_checkpoint(
                authority,
                cursor=cursor,
                seen_channel_ids=seen_channel_ids,
                failures=failures,
                started_at=discovery_started_at,
            )
            _reconcile_registration_cleanup(grant.pk, raise_on_pending=False)
            return discovered
        break
    unavailable_conversations = (
        SlackDmMirrorConversation.objects.filter(grant=grant)
        .exclude(status=SlackDmMirrorConversationStatus.PAUSED)
        .exclude(slack_conversation_id__in=seen_channel_ids)
        .exclude(deliveries__created_at__gt=discovery_started_at)
    )
    unavailable_channel_ids = list(
        unavailable_conversations.filter(
            updated_at__lte=discovery_started_at,
        ).values_list("slack_conversation_id", flat=True)
    )
    for unavailable_channel_id in unavailable_channel_ids:
        _retire_ineligible_from_slack_response(
            authority,
            unavailable_channel_id,
            reason=SLACK_CONVERSATION_UNAVAILABLE_REASON,
            required_scopes=DIRECT_DM_SCOPES,
            not_updated_after=discovery_started_at,
        )
    # A complete Slack listing establishes every retirement above. Drain its
    # durable adapter work only after all local grant/conversation locks ended.
    _reconcile_registration_cleanup(grant.pk, raise_on_pending=False)
    with transaction.atomic():
        locked_grant, locked_connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        if locked_grant.pk == grant.pk:
            needs_followup = (
                SlackDmMirrorConversation.objects.filter(grant=locked_grant)
                .exclude(status=SlackDmMirrorConversationStatus.PAUSED)
                .exclude(slack_conversation_id__in=seen_channel_ids)
                .filter(
                    Q(updated_at__gt=discovery_started_at)
                    | Q(deliveries__created_at__gt=discovery_started_at)
                )
                .exists()
            )
            raw_pending = (locked_connection.sync_cursor or {}).get(
                PENDING_EVENT_CHECKPOINT_KEY,
                [],
            )
            if isinstance(raw_pending, list):
                retained_pending: list[dict[str, Any]] = []
                for item in raw_pending:
                    if not isinstance(item, dict):
                        continue
                    pending_channel_id = str(item.get("channel_id") or "")
                    if not pending_channel_id:
                        continue
                    staged_at = parse_datetime(str(item.get("staged_at") or ""))
                    staged_during_scan = bool(
                        staged_at is not None
                        and timezone.is_aware(staged_at)
                        and staged_at > discovery_started_at
                    )
                    if staged_during_scan or pending_channel_id in seen_channel_ids:
                        retained_pending.append(item)
                        needs_followup = True
                sync_cursor = dict(locked_connection.sync_cursor or {})
                if retained_pending:
                    sync_cursor[PENDING_EVENT_CHECKPOINT_KEY] = retained_pending
                else:
                    sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
                locked_connection.sync_cursor = sync_cursor
                locked_connection.save(update_fields=("sync_cursor", "updated_at"))
            elif PENDING_EVENT_CHECKPOINT_KEY in (locked_connection.sync_cursor or {}):
                sync_cursor = dict(locked_connection.sync_cursor or {})
                sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
                locked_connection.sync_cursor = sync_cursor
                locked_connection.save(update_fields=("sync_cursor", "updated_at"))
            _clear_discovery_checkpoint_locked(locked_connection)
            locked_grant.last_discovery_at = (
                None if needs_followup else timezone.now()
            )
            if locked_grant.last_error != PRIVATE_REGISTRATION_REVOCATION_PENDING:
                locked_grant.last_error = "; ".join(failures)[:2000]
            locked_grant.save(
                update_fields=("last_discovery_at", "last_error", "updated_at")
            )
    return discovered
def _discover_conversation(
    grant: SlackDmMirrorGrant,
    authority: _SlackGrantApiAuthority | WebClient,
    raw: dict[str, Any],
    *,
    profile_cache: dict[str, dict[str, str]],
    force_backfill: bool,
    reset_history: bool,
) -> SlackDmMirrorConversation | None:
    # Preserve the private test/helper call shape while never trusting a
    # caller-supplied raw client for production I/O.
    if not isinstance(authority, _SlackGrantApiAuthority):
        authority = _capture_slack_grant_api_authority(grant)
    channel_id = str(raw.get("id") or "").strip()
    if not channel_id:
        return None
    participant_ids = _conversation_participant_ids(
        authority,
        raw,
        owner_slack_user_id=grant.slack_user_id,
    )
    if not participant_ids:
        _retire_ineligible_from_slack_response(
            authority,
            channel_id,
            reason=SLACK_PARTICIPANTS_INELIGIBLE_REASON,
            required_scopes=(
                DIRECT_DM_SCOPES | GROUP_DM_SCOPES
                if channel_id.startswith("G")
                else DIRECT_DM_SCOPES
            ),
        )
        return None
    conversation, _ = _store_conversation_membership_intent(
        grant.pk,
        authority=authority,
        required_scopes=(
            DIRECT_DM_SCOPES | GROUP_DM_SCOPES
            if channel_id.startswith("G")
            else DIRECT_DM_SCOPES
        ),
        slack_conversation_id=channel_id,
        participant_slack_ids=participant_ids,
    )
    participant_profiles = {
        slack_user_id: _slack_profile(
            authority,
            slack_user_id,
            profile_cache,
            required_scopes=DIRECT_DM_SCOPES,
        )
        for slack_user_id in participant_ids
    }
    conversation = _store_conversation_profiles(
        grant.pk,
        conversation.pk,
        authority=authority,
        required_scopes=(
            DIRECT_DM_SCOPES | GROUP_DM_SCOPES
            if channel_id.startswith("G")
            else DIRECT_DM_SCOPES
        ),
        participant_slack_ids=participant_ids,
        participant_profiles=participant_profiles,
    )
    periodic_reconciliation_due = bool(
        conversation.history_backfilled_at is not None
        and conversation.history_backfilled_at
        <= timezone.now()
        - timedelta(seconds=HISTORY_RECONCILIATION_INTERVAL_SECONDS)
    )
    _provision_owner_conversation(
        conversation,
        force_backfill=force_backfill or periodic_reconciliation_due,
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
            "text": (
                emoji if operation == CommunityBridgeDeliveryType.REACTION_ADD else ""
            ),
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
    if subtype in {"", "thread_broadcast", "file_share", "me_message"}:
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
        if not target_message_id:
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
                    CommunityBridgeDeliveryStatus.PENDING,
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
    query = SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        metadata__slack_echo_key=echo_key,
    )
    # Pending and in-flight rows represent an unresolved, exact outbound
    # operation regardless of Slack's webhook retry delay. Only completed
    # operations use a bounded replay window.
    return query.filter(
        Q(
            status__in=(
                CommunityBridgeDeliveryStatus.PENDING,
                CommunityBridgeDeliveryStatus.PROCESSING,
            )
        )
        | Q(
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            updated_at__gte=timezone.now()
            - timedelta(seconds=SLACK_ECHO_WINDOW_SECONDS),
        )
    ).exists()


def _enqueue_normalized_slack_event_locked(
    conversation: SlackDmMirrorConversation,
    normalized: dict[str, Any],
) -> tuple[str, str]:
    """Persist one normalized event while its grant and conversation are locked."""

    source_message_id = str(normalized.get("source_message_id") or "").strip()
    author_id = str(normalized.get("source_author_id") or "").strip()
    operation = str(normalized.get("operation") or "").strip()
    metadata = normalized.get("metadata")
    if (
        not author_id
        and operation == CommunityBridgeDeliveryType.DELETE
        and isinstance(metadata, dict)
    ):
        target_message_id = str(
            metadata.get("target_source_message_id") or ""
        ).strip()
        original = (
            SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=target_message_id,
                operation=CommunityBridgeDeliveryType.CREATE,
            )
            .order_by("-id")
            .first()
        )
        if original is not None:
            author_id = str(original.source_author_id or "").strip()
        elif SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__slack_ts=target_message_id,
        ).exists():
            author_id = str(conversation.grant.slack_user_id or "").strip()
        normalized = {**normalized, "source_author_id": author_id}
    if not source_message_id or not author_id or not isinstance(metadata, dict):
        return "ignored", ""
    if author_id not in set(conversation.participant_slack_ids or []):
        return "ignored", ""
    if _is_slack_echo(conversation, normalized):
        return "echo", ""
    delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=source_message_id,
        operation=operation,
        defaults={
            "source_author_id": author_id,
            "encrypted_text": str(normalized.get("text") or ""),
            "metadata": {
                **metadata,
                "participant_hash": conversation.participant_hash,
            },
            "available_at": timezone.now(),
        },
    )
    if created:
        return "enqueued", str(delivery.pk)
    if (
        delivery.status
        in (
            CommunityBridgeDeliveryStatus.FAILED,
            CommunityBridgeDeliveryStatus.DEAD,
        )
        and "identity changed" not in delivery.last_error.casefold()
    ):
        delivery.source_author_id = author_id
        delivery.encrypted_text = str(normalized.get("text") or "")
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
        return "enqueued", str(delivery.pk)
    return "duplicate", ""


def _drain_staged_events_for_conversation(
    authority: _SlackGrantApiAuthority,
    conversation_id: int,
) -> int:
    """Atomically route and remove staged ciphertext after exact discovery."""

    with transaction.atomic():
        grant, connection = _lock_slack_grant_api_authority(
            authority,
            required_scopes=DIRECT_DM_SCOPES,
        )
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(
                pk=conversation_id,
                grant=grant,
                status=SlackDmMirrorConversationStatus.LIVE,
            )
            .first()
        )
        if conversation is None:
            return 0
        sync_cursor = dict(connection.sync_cursor or {})
        raw_pending = sync_cursor.get(PENDING_EVENT_CHECKPOINT_KEY, [])
        if not isinstance(raw_pending, list):
            sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
            connection.sync_cursor = sync_cursor
            connection.save(update_fields=("sync_cursor", "updated_at"))
            return 0
        retained: list[Any] = []
        routed = 0
        for item in raw_pending:
            if not isinstance(item, dict) or str(item.get("channel_id") or "") != str(
                conversation.slack_conversation_id
            ):
                retained.append(item)
                continue
            try:
                decoded = json.loads(
                    decrypt_credential_value(str(item.get("ciphertext") or ""))
                )
            except (CredentialEncryptionError, json.JSONDecodeError) as exc:
                raise SlackDmMirrorError(
                    "A staged private Slack event could not be decrypted."
                ) from exc
            if not isinstance(decoded, dict):
                raise SlackDmMirrorError("A staged private Slack event is malformed.")
            result, _ = _enqueue_normalized_slack_event_locked(
                conversation,
                decoded,
            )
            if result == "enqueued":
                routed += 1
        if retained:
            sync_cursor[PENDING_EVENT_CHECKPOINT_KEY] = retained
        else:
            sync_cursor.pop(PENDING_EVENT_CHECKPOINT_KEY, None)
        connection.sync_cursor = sync_cursor
        connection.save(update_fields=("sync_cursor", "updated_at"))
        return routed


def ingest_slack_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    if str(payload.get("type") or "").strip() == "app_rate_limited":
        workspace_id = str(payload.get("team_id") or "").strip()
        grants = list(
            SlackDmMirrorGrant.objects.filter(
                slack_workspace_id=workspace_id,
                status=SlackDmMirrorGrantStatus.ACTIVE,
                revoked_at__isnull=True,
            ).order_by("id")
        )
        scheduled = 0
        for grant in grants:
            scheduled += _schedule_automatic_history_reconciliation(
                grant,
                reason="Slack reported an application rate-limit window",
            )
        return {"status": "history_reconciliation_queued", "count": scheduled}
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
        grant_ids = list(
            SlackDmMirrorConversation.objects.filter(
                slack_workspace_id=workspace_id,
                slack_conversation_id=channel_id,
            )
            .values_list("grant_id", flat=True)
            .distinct()
        )
        for grant_id in grant_ids:
            _retire_ineligible_conversation(
                grant_id,
                channel_id,
                reason=SLACK_CONNECT_INELIGIBLE_REASON,
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
    if channel_id.startswith("G"):
        # MPIM membership can change independently of message delivery. Never
        # persist a body against a stale participant hash: stage the ciphertext
        # under the exact Slack event recipient and let the paced discovery path
        # re-fetch conversations.members before routing it.
        staged = _stage_unknown_slack_event(
            workspace_id,
            channel_id,
            normalized,
            authorized_user_ids=_slack_event_authorized_user_ids(payload),
        )
        return {"status": "discovery_queued", "staged": staged}
    authorized_user_ids = _slack_event_authorized_user_ids(payload)
    conversations = list(
        SlackDmMirrorConversation.objects.select_related("grant").filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
            status=SlackDmMirrorConversationStatus.LIVE,
            grant__status=SlackDmMirrorGrantStatus.ACTIVE,
            grant__revoked_at__isnull=True,
        )
    )
    if authorized_user_ids:
        conversations = [
            conversation
            for conversation in conversations
            if conversation.grant.slack_user_id in authorized_user_ids
        ]
    elif len(conversations) > 1:
        raise SlackDmMirrorError(
            "Slack event recipient authority is ambiguous; retry the webhook."
        )
    if not conversations:
        recoverable_unavailable_private = SlackDmMirrorConversation.objects.filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
            status=SlackDmMirrorConversationStatus.PAUSED,
            last_error=SLACK_CONVERSATION_UNAVAILABLE_REASON,
            grant__status=SlackDmMirrorGrantStatus.ACTIVE,
            grant__revoked_at__isnull=True,
        ).exists()
        known_inactive_private = SlackDmMirrorConversation.objects.filter(
            slack_workspace_id=workspace_id,
            slack_conversation_id=channel_id,
        ).filter(
            Q(status=SlackDmMirrorConversationStatus.PAUSED)
            | ~Q(grant__status=SlackDmMirrorGrantStatus.ACTIVE)
            | Q(grant__revoked_at__isnull=False)
        ).exists()
        if known_inactive_private and not recoverable_unavailable_private:
            # Preserve the private routing classification after disconnect or
            # retirement so a late adapter callback can never fall through to
            # the generic/public bridge ingestion path.
            return {"status": "ignored"}
        # Slack does not retry all event shapes indefinitely, and history cannot
        # reconstruct a message deleted before discovery. Retain the normalized
        # event as ciphertext under every exact workspace grant; discovery later
        # routes it only to mirrors whose participant set authorizes the sender.
        staged = _stage_unknown_slack_event(
            workspace_id,
            channel_id,
            normalized,
            authorized_user_ids=_slack_event_authorized_user_ids(payload),
        )
        return {"status": "discovery_queued", "staged": staged}
    enqueued: list[str] = []
    duplicates = 0
    echoes = 0
    for candidate in conversations:
        with transaction.atomic():
            grant = (
                SlackDmMirrorGrant.objects.select_for_update()
                .filter(
                    pk=candidate.grant_id,
                    status=SlackDmMirrorGrantStatus.ACTIVE,
                    revoked_at__isnull=True,
                )
                .first()
            )
            if grant is None:
                continue
            conversation = (
                SlackDmMirrorConversation.objects.select_for_update()
                .filter(
                    pk=candidate.pk,
                    status=SlackDmMirrorConversationStatus.LIVE,
                )
                .first()
            )
            if conversation is None:
                continue
            result, delivery_id = _enqueue_normalized_slack_event_locked(
                conversation,
                normalized,
            )
            if result == "enqueued":
                enqueued.append(delivery_id)
            elif result == "echo":
                echoes += 1
            elif result == "duplicate":
                duplicates += 1
    if enqueued:
        return {"status": "enqueued", "delivery_ids": enqueued}
    if echoes:
        return {"status": "echo_ignored", "count": echoes}
    return {"status": "duplicate" if duplicates else "ignored", "count": duplicates}


def ingest_mlai_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Queue one owner-authored MLAI private operation for Slack."""

    channel_id = str(payload.get("source_channel_id") or "").strip()
    conversation = SlackDmMirrorConversation.objects.select_related("grant").filter(
        mlai_channel_id=channel_id,
    ).first()
    if conversation is None:
        if SlackDmMirrorDelivery.objects.filter(
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id__startswith=REGISTRATION_STATE_PREFIX,
            metadata__channel_id=channel_id,
        ).exists():
            return {"status": "ignored"}
        return None
    if (
        conversation.status != SlackDmMirrorConversationStatus.LIVE
        or conversation.grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or conversation.grant.revoked_at is not None
    ):
        # The channel remains classified as private after retirement. Returning
        # a handled result prevents the API view from routing a late callback
        # into the generic/public bridge tables.
        return {"status": "ignored"}
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
    if not message_id or len(message_id) > 100 or len(parent_message_id) > 100:
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
    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(
                pk=conversation.grant_id,
                status=SlackDmMirrorGrantStatus.ACTIVE,
                revoked_at__isnull=True,
            )
            .first()
        )
        if grant is None:
            return {"status": "ignored"}
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(
                pk=conversation.pk,
                mlai_channel_id=channel_id,
                status=SlackDmMirrorConversationStatus.LIVE,
            )
            .first()
        )
        if conversation is None:
            return {"status": "ignored"}
        owner_device_pubkeys = _conversation_owner_device_pubkeys(conversation)
        if author_pubkey not in owner_device_pubkeys:
            return {"status": "ignored"}
        # Device deletion takes the same row lock before it can return. Holding
        # it through the encrypted-body insert makes local device revocation a
        # linear privacy boundary just like grant revocation.
        if _locked_active_verified_device(grant.user_id, author_pubkey) is None:
            return {"status": "ignored"}
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


def _backfill_delivery_is_outside_history_window(
    delivery: SlackDmMirrorDelivery,
    *,
    now=None,
    history_days: Any = None,
) -> bool:
    if (
        delivery.source_platform != CommunityBridgePlatform.SLACK
        or not bool((delivery.metadata or {}).get("backfill"))
    ):
        return False
    now = now or timezone.now()
    if history_days is None:
        history_days = delivery.conversation.grant.history_days
    cutoff = int(now.timestamp()) - _bounded_history_days(history_days) * 86_400
    return _delivery_created_at(delivery) < cutoff


def _complete_outside_history_window_delivery_locked(
    delivery: SlackDmMirrorDelivery,
    *,
    now=None,
) -> None:
    now = now or timezone.now()
    metadata = dict(delivery.metadata or {})
    metadata["history_outside_window"] = True
    metadata.pop("history_recovery_scheduled", None)
    metadata.pop("history_recovery_superseded", None)
    delivery.metadata = metadata
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.completed_at = now
    delivery.available_at = now
    delivery.last_error = ""
    delivery.updated_at = now
    delivery.save(
        update_fields=(
            "metadata",
            "status",
            "encrypted_text",
            "completed_at",
            "available_at",
            "last_error",
            "updated_at",
        )
    )


def _expire_outside_history_window_deliveries(*, limit: int = 500) -> int:
    """Incrementally erase queued history after it leaves the consent window."""

    global _history_expiration_cursor, _history_expiration_scan_available_at
    monotonic_now = time.monotonic()
    if monotonic_now < _history_expiration_scan_available_at:
        return 0
    _history_expiration_scan_available_at = (
        monotonic_now + HISTORY_EXPIRATION_SCAN_INTERVAL_SECONDS
    )
    with transaction.atomic():
        rows = list(
            SlackDmMirrorDelivery.objects.select_for_update(
                skip_locked=True,
                of=("self",),
            )
            .select_related("conversation__grant")
            .filter(
                id__gt=_history_expiration_cursor,
                source_platform=CommunityBridgePlatform.SLACK,
                metadata__backfill=True,
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.FAILED,
                    CommunityBridgeDeliveryStatus.DEAD,
                ),
            )
            .order_by("id")[: max(1, min(int(limit), 1000))]
        )
        if not rows:
            _history_expiration_cursor = 0
            return 0
        next_cursor = rows[-1].pk
        now = timezone.now()
        expired = 0
        for delivery in rows:
            if _backfill_delivery_is_outside_history_window(delivery, now=now):
                _complete_outside_history_window_delivery_locked(delivery, now=now)
                expired += 1
        _history_expiration_cursor = next_cursor
        return expired


def process_ready_deliveries(limit: int = 20, *, batch_size: int = 1) -> int:
    now = timezone.now()
    SlackDmMirrorDelivery.objects.filter(
        status=CommunityBridgeDeliveryStatus.PROCESSING,
        updated_at__lt=now - timedelta(minutes=5),
    ).exclude(
        source_message_id__startswith=REGISTRATION_STATE_PREFIX,
    ).update(
        status=CommunityBridgeDeliveryStatus.PENDING,
        available_at=now,
        last_error="Recovered an interrupted private delivery",
        updated_at=now,
    )
    _expire_outside_history_window_deliveries()
    processed = 0
    attempted = 0
    delivery_limit = max(1, min(int(limit), 100))
    normalized_batch_size = max(
        1,
        min(int(batch_size), MAX_PRIVATE_DELIVERY_BATCH),
    )
    while attempted < delivery_limit:
        claimed = _claim_ready_private_delivery_batch(
            limit=min(normalized_batch_size, delivery_limit - attempted),
        )
        if not claimed:
            break
        attempted += len(claimed)
        try:
            if len(claimed) > 1:
                _deliver_private_batch(claimed)
            else:
                _deliver_private(claimed[0])
        except Exception as exc:  # worker boundary; retry with bounded backoff
            for delivery in claimed:
                _record_private_delivery_failure(delivery, exc)
            logger.exception(
                "slack_dm_mirror_delivery_failed delivery_ids=%s",
                ",".join(str(delivery.pk) for delivery in claimed),
            )
            continue
        processed += len(claimed)
    return processed


def _claim_ready_private_delivery_batch(*, limit: int) -> list[SlackDmMirrorDelivery]:
    """Claim one conversation's next ordered work, batching simple creates."""

    with transaction.atomic():
        claim_now = timezone.now()
        candidate_conversation_id = (
            SlackDmMirrorDelivery.objects
            .filter(
                status=CommunityBridgeDeliveryStatus.PENDING,
                available_at__lte=claim_now,
                conversation__status=SlackDmMirrorConversationStatus.LIVE,
                conversation__grant__status=SlackDmMirrorGrantStatus.ACTIVE,
                conversation__grant__revoked_at__isnull=True,
            )
            .exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
            .order_by("available_at", "id")
            .values_list("conversation_id", flat=True)
            .first()
        )
        if candidate_conversation_id is None:
            return []
        # Conversation-first locking prevents two worker processes from
        # claiming different rows in the same DM and reversing their order.
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update(skip_locked=True)
            .select_related("grant__connection")
            .filter(
                pk=candidate_conversation_id,
                status=SlackDmMirrorConversationStatus.LIVE,
                grant__status=SlackDmMirrorGrantStatus.ACTIVE,
                grant__revoked_at__isnull=True,
            )
            .first()
        )
        if conversation is None or SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            status=CommunityBridgeDeliveryStatus.PROCESSING,
        ).exists():
            return []
        seed = None
        for _ in range(100):
            seed = (
                SlackDmMirrorDelivery.objects.select_for_update(
                    skip_locked=True,
                    of=("self",),
                )
                .filter(
                    conversation=conversation,
                    status=CommunityBridgeDeliveryStatus.PENDING,
                    available_at__lte=claim_now,
                )
                .exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
                .order_by("available_at", "id")
                .first()
            )
            if seed is None:
                return []
            seed.conversation = conversation
            if not _backfill_delivery_is_outside_history_window(
                seed,
                now=claim_now,
            ):
                break
            _complete_outside_history_window_delivery_locked(
                seed,
                now=claim_now,
            )
        else:
            return []
        seed.conversation = conversation
        candidates = [seed]
        if limit > 1 and _private_delivery_batch_eligible(seed):
            candidates = list(
                SlackDmMirrorDelivery.objects.select_for_update(
                    skip_locked=True,
                    of=("self",),
                )
                .filter(
                    conversation=conversation,
                    status=CommunityBridgeDeliveryStatus.PENDING,
                    available_at__lte=claim_now,
                )
                .exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
                .order_by("available_at", "id")[:limit]
            )
            bounded_candidates = []
            text_bytes = 0
            for candidate in candidates:
                candidate.conversation = conversation
                if _backfill_delivery_is_outside_history_window(
                    candidate,
                    now=claim_now,
                ):
                    _complete_outside_history_window_delivery_locked(
                        candidate,
                        now=claim_now,
                    )
                    continue
                if not _private_delivery_batch_eligible(candidate):
                    break
                next_text_bytes = text_bytes + len(
                    str(candidate.encrypted_text or "").encode("utf-8")
                )
                if (
                    bounded_candidates
                    and next_text_bytes > MAX_PRIVATE_DELIVERY_BATCH_TEXT_BYTES
                ):
                    break
                bounded_candidates.append(candidate)
                text_bytes = next_text_bytes
            candidates = bounded_candidates or [seed]
        for delivery in candidates:
            _prepare_outbound_echo_metadata(delivery)
            delivery.status = CommunityBridgeDeliveryStatus.PROCESSING
            delivery.save(update_fields=("metadata", "status", "updated_at"))
        return candidates


def _private_delivery_batch_eligible(delivery: SlackDmMirrorDelivery) -> bool:
    metadata = delivery.metadata or {}
    thread_ts = str(metadata.get("thread_ts") or "").strip()
    return bool(
        delivery.source_platform == CommunityBridgePlatform.SLACK
        and delivery.operation == CommunityBridgeDeliveryType.CREATE
        and (not thread_ts or thread_ts == delivery.source_message_id)
    )


def _prepare_outbound_echo_metadata(delivery: SlackDmMirrorDelivery) -> None:
    """Persist deterministic echo identity before external Slack I/O begins."""

    if delivery.source_platform != CommunityBridgePlatform.BUZZ:
        return
    metadata = dict(delivery.metadata or {})
    if delivery.operation == CommunityBridgeDeliveryType.CREATE:
        metadata["client_msg_id"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"mlai-chat-slack-dm:{delivery.pk}")
        )
        delivery.metadata = metadata
        return
    target_source_id = str(
        metadata.get("target_source_message_id")
        or metadata.get("source_event_id")
        or delivery.source_message_id
    ).strip()
    slack_ts = _slack_destination_message_id(
        delivery.conversation,
        target_source_id,
    )
    if not slack_ts:
        delivery.metadata = metadata
        return
    reaction = ""
    if delivery.operation in {
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    }:
        reaction = emoji_to_slack_reaction(delivery.encrypted_text)
    metadata.update(
        {
            "slack_ts": slack_ts,
            "slack_echo_key": _slack_echo_key(
                operation=delivery.operation,
                target_message_id=slack_ts,
                author_id=delivery.conversation.grant.slack_user_id,
                reaction=reaction,
                text=(
                    delivery.encrypted_text
                    if delivery.operation == CommunityBridgeDeliveryType.EDIT
                    else ""
                ),
            ),
            "slack_reaction": reaction,
            "slack_reaction_object_id": (
                reaction_object_id(
                    message_id=slack_ts,
                    reaction=reaction,
                    author_id=delivery.conversation.grant.slack_user_id,
                )
                if reaction
                else ""
            ),
        }
    )
    delivery.metadata = metadata


def _record_private_delivery_failure(
    claimed_delivery: SlackDmMirrorDelivery,
    exc: Exception,
) -> None:
    """Retry a failed body only while its consent boundary remains current."""

    if _is_slack_auth_error(exc):
        user_id = (
            SlackDmMirrorGrant.objects.filter(
                pk=claimed_delivery.conversation.grant_id
            )
            .values_list("user_id", flat=True)
            .first()
        )
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is not None:
            # The credential can no longer authorize any Slack I/O. Reuse the
            # full local disconnect privacy boundary so queued bodies,
            # registrations, derived lineage, and every sibling grant are
            # fenced before another worker tick.
            revoke_user_grant(user)
        return

    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(pk=claimed_delivery.conversation.grant_id)
            .first()
        )
        if grant is None:
            return
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(pk=claimed_delivery.conversation_id, grant=grant)
            .first()
        )
        if conversation is None:
            return
        delivery = (
            SlackDmMirrorDelivery.objects.select_for_update()
            .filter(pk=claimed_delivery.pk, conversation=conversation)
            .first()
        )
        if (
            delivery is None
            or delivery.status != CommunityBridgeDeliveryStatus.PROCESSING
        ):
            # Revoke/device replacement may already have cleared this row.  Do
            # not save the stale in-memory claim and resurrect its body.
            return
        dependency_pending = isinstance(exc, SlackDmMirrorDependencyPending)
        if not dependency_pending:
            delivery.attempts = min(delivery.attempts + 1, 32_767)
        dependency_metadata_changed = False
        if dependency_pending and delivery.source_platform == CommunityBridgePlatform.SLACK:
            dependency_metadata = dict(delivery.metadata or {})
            dependency_reconciliation_eligible = not any(
                bool(dependency_metadata.get(key))
                for key in (
                    "backfill",
                    "thread_parent_outside_history_window",
                    "dependency_outside_history",
                    "dependency_reconciliation_complete",
                )
            )
            if dependency_reconciliation_eligible and not bool(
                dependency_metadata.get("dependency_reconciliation_pending")
            ):
                # `_deliver_private` deliberately performs network I/O inside
                # an atomic grant boundary. Persist the follow-up only here,
                # after that transaction rolled back the dependency exception.
                dependency_metadata["dependency_reconciliation_pending"] = True
                delivery.metadata = dependency_metadata
                dependency_metadata_changed = True
                _mark_conversation_history_due(
                    conversation,
                    reason="A live Slack dependency arrived out of order",
                    reset_deliveries=False,
                    reconcile_current_state=True,
                )
        permanent = bool(getattr(exc, "permanent", False))
        authorized = bool(
            grant.status == SlackDmMirrorGrantStatus.ACTIVE
            and grant.revoked_at is None
            and conversation.status == SlackDmMirrorConversationStatus.LIVE
            and str((delivery.metadata or {}).get("participant_hash") or "")
            in {"", str(conversation.participant_hash or "")}
        )
        delivery.status = CommunityBridgeDeliveryStatus.PENDING
        if not authorized or permanent:
            delivery.status = CommunityBridgeDeliveryStatus.DEAD
        retry_after = _slack_retry_after_seconds(exc)
        delivery.available_at = timezone.now() + timedelta(
            seconds=max(
                retry_after,
                15 if dependency_pending else min(900, 2 ** min(delivery.attempts, 10)),
            )
        )
        delivery.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
        update_fields = [
            "attempts",
            "status",
            "available_at",
            "last_error",
            "updated_at",
        ]
        if delivery.status == CommunityBridgeDeliveryStatus.DEAD:
            delivery.encrypted_text = ""
            metadata = dict(delivery.metadata or {})
            if permanent:
                # Automatic source recovery must not hammer a permanently
                # rejected adapter operation forever. Explicit backfill or a
                # new consent generation clears this fence.
                metadata["permanent_failure"] = True
                metadata["history_recovery_scheduled"] = True
                delivery.metadata = metadata
            update_fields.append("encrypted_text")
            if permanent:
                update_fields.append("metadata")
        elif dependency_metadata_changed:
            update_fields.append("metadata")
        delivery.save(update_fields=tuple(update_fields))


def discover_grants_if_due() -> None:
    """Periodically discover new IM channels without blocking Slack webhooks."""

    global _last_grant_discovery_scan
    now_monotonic = time.monotonic()
    if now_monotonic - _last_grant_discovery_scan < 5:
        return
    _last_grant_discovery_scan = now_monotonic
    now = timezone.now()
    stale_processing_cutoff = now - timedelta(
        seconds=REGISTRATION_CLEANUP_LEASE_SECONDS
    )
    cleanup_grant_ids = list(
        SlackDmMirrorDelivery.objects.filter(
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id__startswith=REGISTRATION_STATE_PREFIX,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        .filter(
            Q(
                status=CommunityBridgeDeliveryStatus.PENDING,
                available_at__lte=now,
            )
            | Q(
                status=CommunityBridgeDeliveryStatus.PROCESSING,
                updated_at__lt=stale_processing_cutoff,
            )
        )
        .values("conversation__grant_id")
        .annotate(next_cleanup_at=Min("available_at"))
        .order_by("next_cleanup_at", "conversation__grant_id")
        .values_list("conversation__grant_id", flat=True)[:10]
    )
    for cleanup_grant_id in cleanup_grant_ids:
        try:
            _reconcile_registration_cleanup(
                cleanup_grant_id,
                raise_on_pending=False,
            )
        except Exception as exc:
            logger.warning(
                "slack_dm_mirror_registration_cleanup_failed " "grant_id=%s error=%s",
                cleanup_grant_id,
                exc.__class__.__name__,
            )
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
            if _is_slack_auth_error(exc):
                user = get_user_model().objects.filter(pk=grant.user_id).first()
                if user is not None:
                    revoke_user_grant(user)
                logger.warning(
                    "slack_dm_mirror_discovery_credential_revoked grant_id=%s",
                    grant.pk,
                )
                continue
            grant.refresh_from_db(fields=("status", "revoked_at", "last_error"))
            if (
                grant.status == SlackDmMirrorGrantStatus.ACTIVE
                and grant.revoked_at is None
                and grant.last_error != PRIVATE_REGISTRATION_REVOCATION_PENDING
            ):
                grant.last_discovery_at = timezone.now()
                grant.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
                grant.save(
                    update_fields=("last_discovery_at", "last_error", "updated_at")
                )
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

    _reconcile_registration_cleanup(
        conversation.grant_id,
        raise_on_pending=False,
    )
    with transaction.atomic():
        SlackDmMirrorGrant.objects.select_for_update().get(pk=conversation.grant_id)
        if _registration_cleanup_pending_locked(
            conversation.grant_id,
            conversation_id=conversation.pk,
        ):
            raise SlackDmMirrorError(
                "Previous private registration cleanup is still pending."
            )
    provision_request: dict[str, Any] | None = None
    deferred_error: Exception | None = None
    with transaction.atomic():
        # Establish the local provisioning intent while serialized with
        # revoke_grant, then release the lock before calling the adapter.
        grant = SlackDmMirrorGrant.objects.select_for_update().get(
            pk=conversation.grant_id
        )
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=conversation.pk
        )
        conversation.grant = grant
        provision_request, deferred_error = _prepare_owner_conversation_locked(
            conversation,
            force_backfill=force_backfill,
            reset_history=reset_history,
            required_owner_public_key=required_owner_public_key,
        )
    if deferred_error is not None:
        raise deferred_error
    if provision_request is None:
        return

    provision_error: Exception | None = None
    authority_error: Exception | None = None
    current_intent = False
    with transaction.atomic():
        # A durable attempt exists before the adapter call, but device revoke
        # must also be unable to return while that POST can still create a
        # registration containing the revoked key. Take the same
        # grant->conversation->device authority locks as DeviceView and keep
        # them through the bounded adapter call and exact-attempt finalization.
        grant = SlackDmMirrorGrant.objects.select_for_update().get(
            pk=conversation.grant_id
        )
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=conversation.pk
        )
        callback_author_pubkeys = sorted(
            {
                str(value or "").strip().lower()
                for value in provision_request["callback_author_pubkeys"]
                if str(value or "").strip()
            }
        )
        locked_devices = list(
            CommunityChatDevice.objects.select_for_update()
            .filter(
                user_id=grant.user_id,
                public_key__in=callback_author_pubkeys,
                status=DeviceBindingStatus.VERIFIED,
                revoked_at__isnull=True,
            )
            .order_by("public_key")
        )
        attempt = SlackDmMirrorDelivery.objects.select_for_update().get(
            pk=provision_request["attempt_id"],
            conversation=conversation,
        )
        authority_current = bool(
            grant.status == SlackDmMirrorGrantStatus.ACTIVE
            and grant.revoked_at is None
            and conversation.status == SlackDmMirrorConversationStatus.PROVISIONING
            and _registration_state(attempt) == REGISTRATION_STATE_PROVISIONING
            and _registration_generation(attempt) == _grant_consent_generation(grant)
            and _registration_participant_hash(attempt) == conversation.participant_hash
            and _registration_slack_participant_ids(attempt)
            == sorted(conversation.participant_slack_ids or [])
            and [device.public_key.lower() for device in locked_devices]
            == callback_author_pubkeys
        )
        if not authority_current:
            # The transaction that changed consent, participants, device
            # authority, or the authoritative attempt is responsible for
            # fencing all registrations it superseded. A late phase-two caller
            # may retire only its own still-live attempt: touching the whole
            # conversation here could cancel a newer ACTIVE registration, and
            # reopening an attempt already marked CLEANED could resurrect work
            # that another finalizer has safely reconciled.
            if _registration_state(attempt) in {
                REGISTRATION_STATE_PROVISIONING,
                REGISTRATION_STATE_AMBIGUOUS,
            }:
                _mark_registration_cleanup_pending_locked(
                    attempt,
                    reason="Private registration authority changed before adapter POST",
                    available_at=timezone.now(),
                )
                _update_registration_cleanup_summary_locked(grant)
            authority_error = SlackDmMirrorAuthorizationError(
                "Slack DM mirroring authority changed before provisioning."
            )
        else:
            try:
                provisioned = BuzzBridgeClient.provision_private_conversation(
                    provision_request["participant_pubkeys"],
                    callback_author_pubkeys=callback_author_pubkeys,
                    conversation_name=provision_request["conversation_name"],
                )
            except Exception as exc:
                _record_ambiguous_registration_attempt(
                    provision_request["attempt_id"],
                    exc,
                )
                provision_error = exc
            else:
                current_intent = _finalize_registration_attempt(
                    provision_request["attempt_id"],
                    channel_id=str(provisioned["channel_id"]),
                )
    if authority_error is not None:
        raise authority_error
    if provision_error is not None:
        raise provision_error
    _reconcile_registration_cleanup(
        conversation.grant_id,
        raise_on_pending=False,
    )
    if not current_intent:
        raise SlackDmMirrorAuthorizationError(
            "Slack DM mirroring consent or participants changed during provisioning."
        )
    with transaction.atomic():
        SlackDmMirrorGrant.objects.select_for_update().get(pk=conversation.grant_id)
        if _registration_cleanup_pending_locked(
            conversation.grant_id,
            conversation_id=conversation.pk,
        ):
            raise SlackDmMirrorError(
                "Previous private registration cleanup is still pending."
            )


def _prepare_owner_conversation_locked(
    conversation: SlackDmMirrorConversation,
    *,
    force_backfill: bool,
    reset_history: bool,
    required_owner_public_key: str | None,
) -> tuple[dict[str, Any] | None, Exception | None]:
    """Prepare a provision request while grant and conversation rows are locked."""

    participant_ids = sorted(set(conversation.participant_slack_ids or []))
    grant = conversation.grant
    if grant.status != SlackDmMirrorGrantStatus.ACTIVE or grant.revoked_at is not None:
        conversation.status = SlackDmMirrorConversationStatus.PAUSED
        conversation.last_error = "Slack DM mirroring is no longer active"
        conversation.save(update_fields=("status", "last_error", "updated_at"))
        return (
            None,
            SlackDmMirrorAuthorizationError(
                "Slack DM mirroring consent is no longer active."
            ),
        )
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
        return None, None
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
    _ensure_current_registration_row_locked(conversation, grant)
    reactivating_existing_mirror = bool(
        conversation.status == SlackDmMirrorConversationStatus.PAUSED
        and conversation.mlai_channel_id
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
            reconcile_current_state=True,
        )
    elif reactivating_existing_mirror:
        _mark_conversation_history_due(
            conversation,
            reason="Slack DM mirroring resumed",
            reset_deliveries=False,
            reconcile_current_state=True,
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
        return None, None
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
    conversation_name = _conversation_name(conversation)
    attempt = _create_registration_row_locked(
        conversation,
        grant=grant,
        state=REGISTRATION_STATE_PROVISIONING,
        participant_pubkeys=pubkeys,
        callback_author_pubkeys=owner_device_pubkeys,
        participant_hash=participant_hash,
        conversation_name_value=conversation_name,
        provision_attempt=True,
    )
    return (
        {
            "attempt_id": attempt.pk,
            "participant_pubkeys": pubkeys,
            "callback_author_pubkeys": owner_device_pubkeys,
            "conversation_name": conversation_name,
        },
        None,
    )


def _mark_conversation_history_due(
    conversation: SlackDmMirrorConversation,
    *,
    reason: str,
    reset_deliveries: bool,
    reconcile_current_state: bool = False,
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
    if reconcile_current_state:
        _mark_history_reconciliation_candidates_locked(conversation)
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


def _mark_history_reconciliation_candidates_locked(
    conversation: SlackDmMirrorConversation,
) -> None:
    """Mark current Slack state whose later absence implies delete/remove."""

    reconcile_epoch = uuid.uuid4().hex
    reconcile_oldest = ""
    if conversation.grant.history_days:
        reconcile_oldest = str(
            max(0, int(time.time()) - int(conversation.grant.history_days) * 86_400)
        )
    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update().filter(
            conversation=conversation,
            source_platform__in=(
                CommunityBridgePlatform.SLACK,
                CommunityBridgePlatform.BUZZ,
            ),
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
    )
    changed: list[SlackDmMirrorDelivery] = []
    for row in rows:
        original_metadata = dict(row.metadata or {})
        metadata = dict(original_metadata)
        _clear_history_reconciliation_metadata(metadata)
        if row.source_message_id.startswith(
            (HISTORY_STATE_PREFIX, REGISTRATION_STATE_PREFIX)
        ):
            if metadata != original_metadata:
                row.metadata = metadata
                changed.append(row)
            continue
        raw_timestamp = (
            row.source_message_id
            if row.source_platform == CommunityBridgePlatform.SLACK
            else str((row.metadata or {}).get("slack_ts") or "")
        )
        try:
            seconds, _ = _slack_ts_sort_key(raw_timestamp)
        except SlackDmMirrorError:
            if metadata != original_metadata:
                row.metadata = metadata
                changed.append(row)
            continue
        if reconcile_oldest and seconds < int(reconcile_oldest):
            if metadata != original_metadata:
                row.metadata = metadata
                changed.append(row)
            continue
        metadata[HISTORY_RECONCILE_CANDIDATE_KEY] = True
        metadata[HISTORY_RECONCILE_EPOCH_KEY] = reconcile_epoch
        metadata[HISTORY_RECONCILE_OLDEST_KEY] = reconcile_oldest
        row.metadata = metadata
        changed.append(row)
    if changed:
        SlackDmMirrorDelivery.objects.bulk_update(changed, ("metadata", "updated_at"))


def _clear_history_reconciliation_metadata(metadata: dict[str, Any]) -> None:
    metadata.pop(HISTORY_RECONCILE_CANDIDATE_KEY, None)
    metadata.pop(HISTORY_RECONCILE_EPOCH_KEY, None)
    metadata.pop(HISTORY_RECONCILE_OLDEST_KEY, None)


def _history_reconciliation_boundary_locked(
    conversation: SlackDmMirrorConversation,
) -> tuple[str, str]:
    boundaries = {
        (
            str((metadata or {}).get(HISTORY_RECONCILE_EPOCH_KEY) or ""),
            str((metadata or {}).get(HISTORY_RECONCILE_OLDEST_KEY) or ""),
        )
        for metadata in SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__history_reconcile_candidate=True,
        )
        .values_list("metadata", flat=True)
    }
    boundaries.discard(("", ""))
    if len(boundaries) > 1:
        raise SlackDmMirrorError(
            "Slack history reconciliation has conflicting scan boundaries."
        )
    return next(iter(boundaries), ("", ""))


def _clear_history_scan_states(conversation_ids: list[int]) -> None:
    if not conversation_ids:
        return
    SlackDmMirrorDelivery.objects.filter(
        conversation_id__in=conversation_ids,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id__startswith=HISTORY_STATE_PREFIX,
    ).delete()


def _restart_incomplete_history_scans_locked(
    conversations: list[SlackDmMirrorConversation],
) -> None:
    """Restart partial scans after an exact same-identity consent renewal."""

    conversation_ids_with_permanent_rows = set(
        SlackDmMirrorDelivery.objects.filter(
            conversation__in=conversations,
            source_platform=CommunityBridgePlatform.SLACK,
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            ),
            metadata__permanent_failure=True,
        ).values_list("conversation_id", flat=True)
    )
    incomplete = [
        conversation
        for conversation in conversations
        if conversation.history_backfilled_at is None
        or conversation.pk in conversation_ids_with_permanent_rows
    ]
    conversation_ids = [conversation.pk for conversation in incomplete]
    _clear_history_scan_states(conversation_ids)
    _clear_permanent_recovery_fences_locked(conversation_ids)
    now = timezone.now()
    for conversation in incomplete:
        conversation.oldest_synced_ts = ""
        conversation.latest_synced_ts = ""
        conversation.last_error = ""
        conversation.updated_at = now
    if incomplete:
        SlackDmMirrorConversation.objects.bulk_update(
            incomplete,
            ("oldest_synced_ts", "latest_synced_ts", "last_error", "updated_at"),
        )


def _clear_permanent_recovery_fences_locked(conversation_ids: list[int]) -> None:
    if not conversation_ids:
        return
    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update().filter(
            conversation_id__in=conversation_ids,
            source_platform=CommunityBridgePlatform.SLACK,
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            ),
        )
    )
    changed: list[SlackDmMirrorDelivery] = []
    for row in rows:
        metadata = dict(row.metadata or {})
        removed = metadata.pop("permanent_failure", None)
        scheduled = metadata.pop("history_recovery_scheduled", None)
        if removed is None and scheduled is None:
            continue
        row.metadata = metadata
        changed.append(row)
    if changed:
        SlackDmMirrorDelivery.objects.bulk_update(changed, ("metadata", "updated_at"))


def _mark_backfill_rows_for_recovery_locked(
    conversation_ids: list[int],
    *,
    now=None,
) -> int:
    """Mark terminal backfill rows for an idempotent source re-scan.

    DEAD bodies have already been erased, so they must never be changed back
    to PENDING directly. The next Slack scan repopulates the encrypted body and
    resets attempts through `_upsert_history_delivery`; rows Slack no longer
    returns are retained as content-free superseded tombstones.
    """

    if not conversation_ids:
        return 0
    now = now or timezone.now()
    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation_id__in=conversation_ids,
            source_platform=CommunityBridgePlatform.SLACK,
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            ),
        )
        .filter(
            Q(metadata__history_recovery_scheduled__isnull=True)
            | Q(metadata__history_recovery_scheduled=False)
        )
        .order_by("id")
    )
    for row in rows:
        metadata = dict(row.metadata or {})
        metadata["history_recovery_scheduled"] = True
        metadata.pop("history_recovery_superseded", None)
        row.metadata = metadata
        row.available_at = now
        row.updated_at = now
    if rows:
        SlackDmMirrorDelivery.objects.bulk_update(
            rows,
            ("metadata", "available_at", "updated_at"),
        )
    return len(rows)


def recover_dead_backfill_deliveries(limit: int = 10) -> int:
    """Schedule source re-scans for terminal backfill rows with erased bodies."""

    candidate_ids = list(
        SlackDmMirrorDelivery.objects.filter(
            source_platform=CommunityBridgePlatform.SLACK,
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            ),
            available_at__lte=timezone.now(),
            conversation__status=SlackDmMirrorConversationStatus.LIVE,
            conversation__grant__status=SlackDmMirrorGrantStatus.ACTIVE,
            conversation__grant__revoked_at__isnull=True,
        )
        .filter(
            Q(metadata__history_recovery_scheduled__isnull=True)
            | Q(metadata__history_recovery_scheduled=False)
        )
        .filter(
            Q(metadata__history_recovery_superseded__isnull=True)
            | Q(metadata__history_recovery_superseded=False)
        )
        .order_by("conversation_id")
        .values_list("conversation_id", flat=True)
        .distinct()[: max(1, min(int(limit), 100))]
    )
    scheduled = 0
    for conversation_id in candidate_ids:
        with transaction.atomic():
            conversation_ref = (
                SlackDmMirrorConversation.objects.filter(pk=conversation_id)
                .values("grant_id")
                .first()
            )
            if conversation_ref is None:
                continue
            grant = (
                SlackDmMirrorGrant.objects.select_for_update()
                .filter(
                    pk=conversation_ref["grant_id"],
                    status=SlackDmMirrorGrantStatus.ACTIVE,
                    revoked_at__isnull=True,
                )
                .first()
            )
            if grant is None:
                continue
            conversation = (
                SlackDmMirrorConversation.objects.select_for_update()
                .filter(
                    pk=conversation_id,
                    grant=grant,
                    status=SlackDmMirrorConversationStatus.LIVE,
                )
                .first()
            )
            if conversation is None:
                continue
            marked = _mark_backfill_rows_for_recovery_locked([conversation.pk])
            if not marked:
                continue
            _clear_history_scan_states([conversation.pk])
            conversation.history_backfilled_at = None
            conversation.oldest_synced_ts = ""
            conversation.latest_synced_ts = ""
            conversation.last_error = ""
            conversation.save(
                update_fields=(
                    "history_backfilled_at",
                    "oldest_synced_ts",
                    "latest_synced_ts",
                    "last_error",
                    "updated_at",
                )
            )
            scheduled += marked
    return scheduled


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
    recover_dead_backfill_deliveries(limit=scan_limit)
    for _ in range(scan_limit):
        now = timezone.now()
        with transaction.atomic():
            candidate = (
                SlackDmMirrorConversation.objects.filter(
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
                .values("id", "grant_id")
                .first()
            )
            if candidate is None:
                break
            grant = (
                SlackDmMirrorGrant.objects.select_for_update(
                    skip_locked=True,
                    of=("self",),
                )
                .select_related("connection")
                .filter(
                    pk=candidate["grant_id"],
                    status=SlackDmMirrorGrantStatus.ACTIVE,
                    revoked_at__isnull=True,
                )
                .first()
            )
            if grant is None:
                break
            conversation = (
                SlackDmMirrorConversation.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=candidate["id"],
                    grant=grant,
                    status=SlackDmMirrorConversationStatus.LIVE,
                )
                .first()
            )
            if conversation is None:
                break
            conversation.grant = grant
            conversation.last_error = f"history_scan_processing:{now.isoformat()}"
            conversation.save(update_fields=("last_error", "updated_at"))
        try:
            _enqueue_history_page(conversation, conversation.grant)
        except Exception as exc:
            if _is_slack_auth_error(exc):
                user = get_user_model().objects.filter(
                    pk=conversation.grant.user_id
                ).first()
                if user is not None:
                    revoke_user_grant(user)
                logger.warning(
                    "slack_dm_mirror_history_credential_revoked grant_id=%s",
                    conversation.grant_id,
                )
                continue
            _apply_slack_retry_after(exc)
            with transaction.atomic():
                locked_grant = (
                    SlackDmMirrorGrant.objects.select_for_update()
                    .filter(
                        pk=conversation.grant_id,
                        status=SlackDmMirrorGrantStatus.ACTIVE,
                        revoked_at__isnull=True,
                    )
                    .first()
                )
                if locked_grant is not None:
                    locked_conversation = (
                        SlackDmMirrorConversation.objects.select_for_update()
                        .filter(
                            pk=conversation.pk,
                            grant=locked_grant,
                            status=SlackDmMirrorConversationStatus.LIVE,
                        )
                        .first()
                    )
                    if locked_conversation is not None:
                        locked_conversation.last_error = (
                            f"{exc.__class__.__name__}: {exc}"[:2000]
                        )
                        locked_conversation.save(
                            update_fields=("last_error", "updated_at")
                        )
            logger.warning(
                "slack_dm_mirror_history_scan_failed conversation_id=%s error=%s",
                conversation.pk,
                exc,
            )
            continue
        processed += 1
    return processed


def _history_required_scopes(slack_conversation_id: str) -> set[str]:
    if str(slack_conversation_id or "").startswith("G"):
        return DIRECT_DM_SCOPES | GROUP_DM_SCOPES
    return set(DIRECT_DM_SCOPES)


def _history_scan_authority_from_state(
    state: SlackDmMirrorDelivery,
) -> _SlackHistoryScanAuthority:
    metadata = dict(state.metadata or {})
    try:
        history_days = int(metadata.get("history_days"))
    except (TypeError, ValueError) as exc:
        raise SlackDmMirrorAuthorizationError(
            "Slack history scan state is invalid."
        ) from exc
    authority = _SlackHistoryScanAuthority(
        epoch=str(metadata.get("scan_epoch") or "").strip(),
        participant_hash=str(metadata.get("participant_hash") or "").strip(),
        mlai_channel_id=str(metadata.get("mlai_channel_id") or "").strip(),
        registration_id=str(metadata.get("registration_id") or "").strip(),
        registration_generation=str(
            metadata.get("registration_generation") or ""
        ).strip(),
        history_days=history_days,
        oldest=str(metadata.get("oldest") or "").strip(),
    )
    if (
        not authority.epoch
        or not authority.participant_hash
        or not authority.mlai_channel_id
        or not authority.registration_id
        or not authority.registration_generation
        or authority.history_days < 0
        or (authority.history_days > 0 and not authority.oldest.isdigit())
        or (authority.history_days == 0 and authority.oldest)
    ):
        raise SlackDmMirrorAuthorizationError("Slack history scan state is invalid.")
    return authority


def _history_participant_boundary(
    conversation: SlackDmMirrorConversation,
) -> str:
    participant_hash = str(conversation.participant_hash or "").strip()
    if participant_hash:
        return participant_hash
    # Legacy LIVE rows can predate participant_hash. Keep their scan fenced by
    # an exact, content-free digest until discovery persists the canonical one.
    encoded = json.dumps(
        {
            "slack_ids": sorted(conversation.participant_slack_ids or []),
            "pubkeys": sorted(conversation.participant_buzz_pubkeys or []),
            "identity_map": sorted(
                (str(key), str(value))
                for key, value in (conversation.participant_identity_map or {}).items()
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_history_scan_authority_locked(
    conversation: SlackDmMirrorConversation,
    grant: SlackDmMirrorGrant,
    state: SlackDmMirrorDelivery,
    expected: _SlackHistoryScanAuthority,
) -> None:
    current = _history_scan_authority_from_state(state)
    registration = (
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=expected.registration_id,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        .first()
    )
    current_registration = bool(
        registration is not None
        and _registration_state(registration) == REGISTRATION_STATE_ACTIVE
        and _registration_generation(registration)
        == expected.registration_generation
        and _registration_channel_id(registration) == expected.mlai_channel_id
        and _registration_participant_hash(registration)
        == str(conversation.participant_hash or "")
        and _registration_slack_participant_ids(registration)
        == sorted(conversation.participant_slack_ids or [])
    )
    if (
        current != expected
        or _history_participant_boundary(conversation) != expected.participant_hash
        or str(conversation.mlai_channel_id or "") != expected.mlai_channel_id
        or grant.history_days != expected.history_days
        or not current_registration
    ):
        raise SlackDmMirrorAuthorizationError(
            "Slack history scan authority changed before the response was stored."
        )


def _prepare_history_scan_page(
    conversation_id: int,
    grant_id: int,
    authority: _SlackGrantApiAuthority,
    required_scopes: set[str] | frozenset[str],
) -> tuple[
    _SlackHistoryScanAuthority,
    str,
    str,
    SlackDmMirrorDelivery | None,
    bool,
]:
    """Create or resume one durable scan epoch before issuing Slack I/O."""

    with transaction.atomic():
        conversation, grant = _locked_history_write_context(
            conversation_id,
            grant_id,
            authority,
            required_scopes,
        )
        if _normalize_grant_history_window_locked(grant):
            # The queryset update in the normalizer deliberately resets every
            # conversation owned by this legacy grant. Keep this locked model
            # instance in sync before constructing the new bounded scan.
            conversation.history_backfilled_at = None
            conversation.oldest_synced_ts = ""
            conversation.latest_synced_ts = ""
            conversation.last_error = ""
        state = (
            SlackDmMirrorDelivery.objects.select_for_update()
            .filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=HISTORY_MAIN_STATE_ID,
                operation=CommunityBridgeDeliveryType.CREATE,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
            )
            .first()
        )
        if state is None:
            registration = _ensure_current_registration_row_locked(
                conversation,
                grant,
            )
            if (
                registration is None
                or _registration_state(registration) != REGISTRATION_STATE_ACTIVE
            ):
                raise SlackDmMirrorAuthorizationError(
                    "The private conversation registration is not current."
                )
            (
                history_reconcile_epoch,
                history_reconcile_oldest,
            ) = _history_reconciliation_boundary_locked(conversation)
            oldest = history_reconcile_oldest
            if not history_reconcile_epoch and grant.history_days > 0:
                oldest = str(
                    max(0, int(time.time()) - int(grant.history_days) * 86_400)
                )
            state = _ensure_history_state(
                conversation,
                source_message_id=HISTORY_MAIN_STATE_ID,
                metadata={
                    "history_scan_state": "main",
                    "complete": False,
                    "scan_epoch": uuid.uuid4().hex,
                    "participant_hash": _history_participant_boundary(conversation),
                    "mlai_channel_id": str(conversation.mlai_channel_id or ""),
                    "registration_id": registration.source_message_id,
                    "registration_generation": _registration_generation(registration),
                    "history_days": int(grant.history_days),
                    "oldest": oldest,
                    HISTORY_RECONCILE_EPOCH_KEY: history_reconcile_epoch,
                },
            )
        scan_authority = _history_scan_authority_from_state(state)
        _assert_history_scan_authority_locked(
            conversation,
            grant,
            state,
            scan_authority,
        )
        thread_state = _next_incomplete_thread_state(
            conversation,
            scan_epoch=scan_authority.epoch,
        )
        return (
            scan_authority,
            conversation.slack_conversation_id,
            conversation.oldest_synced_ts,
            thread_state,
            bool((state.metadata or {}).get("complete")),
        )


def _enqueue_history_page(
    conversation: SlackDmMirrorConversation,
    grant: SlackDmMirrorGrant,
) -> int:
    authority = _capture_slack_grant_api_authority(grant)
    required_scopes = _history_required_scopes(conversation.slack_conversation_id)
    (
        scan_authority,
        slack_conversation_id,
        oldest_synced_ts,
        thread_state,
        main_complete,
    ) = _prepare_history_scan_page(
        conversation.pk,
        grant.pk,
        authority,
        required_scopes,
    )
    if thread_state is not None:
        return _enqueue_reply_page(
            conversation.pk,
            grant.pk,
            slack_conversation_id,
            authority,
            scan_authority,
            thread_state,
        )
    if main_complete:
        with transaction.atomic():
            locked_conversation, _ = _locked_history_write_context(
                conversation.pk,
                grant.pk,
                authority,
                required_scopes,
                scan_authority=scan_authority,
            )
            _finish_history_scan(locked_conversation)
        return 0

    request_kwargs: dict[str, Any] = {
        "channel": slack_conversation_id,
        "limit": HISTORY_PAGE_LIMIT,
    }
    if scan_authority.history_days > 0:
        request_kwargs["oldest"] = scan_authority.oldest
        request_kwargs["inclusive"] = True
    if oldest_synced_ts:
        request_kwargs["latest"] = oldest_synced_ts
        request_kwargs["inclusive"] = False
    response = _call_slack_with_grant_authority(
        authority,
        "conversations_history",
        required_scopes=required_scopes,
        **request_kwargs,
    )
    return _persist_history_page(
        conversation.pk,
        grant.pk,
        authority,
        scan_authority,
        required_scopes,
        response,
    )


def _locked_history_write_context(
    conversation_id: int,
    grant_id: int,
    authority: _SlackGrantApiAuthority,
    required_scopes: set[str] | frozenset[str],
    *,
    scan_authority: _SlackHistoryScanAuthority | None = None,
) -> tuple[SlackDmMirrorConversation, SlackDmMirrorGrant]:
    grant, _ = _lock_slack_grant_api_authority(
        authority,
        required_scopes=required_scopes,
    )
    if grant.pk != grant_id:
        raise SlackDmMirrorAuthorizationError(
            "Slack DM mirroring was revoked during history ingestion."
        )
    conversation = (
        SlackDmMirrorConversation.objects.select_for_update()
        .filter(
            pk=conversation_id,
            grant=grant,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        .first()
    )
    if conversation is None:
        raise SlackDmMirrorAuthorizationError(
            "Slack DM mirroring was paused during history ingestion."
        )
    if scan_authority is not None:
        state = (
            SlackDmMirrorDelivery.objects.select_for_update()
            .filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=HISTORY_MAIN_STATE_ID,
                operation=CommunityBridgeDeliveryType.CREATE,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
            )
            .first()
        )
        if state is None:
            raise SlackDmMirrorAuthorizationError(
                "Slack history scan was replaced before the response was stored."
            )
        _assert_history_scan_authority_locked(
            conversation,
            grant,
            state,
            scan_authority,
        )
    return conversation, grant


def _persist_history_page(
    conversation_id: int,
    grant_id: int,
    authority: _SlackGrantApiAuthority,
    scan_authority: _SlackHistoryScanAuthority,
    required_scopes: set[str] | frozenset[str],
    response: Any,
) -> int:
    with transaction.atomic():
        conversation, _ = _locked_history_write_context(
            conversation_id,
            grant_id,
            authority,
            required_scopes,
            scan_authority=scan_authority,
        )
        state = SlackDmMirrorDelivery.objects.select_for_update().get(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=HISTORY_MAIN_STATE_ID,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        return _persist_history_page_locked(
            conversation,
            state,
            scan_authority,
            response,
        )


def _persist_history_page_locked(
    conversation: SlackDmMirrorConversation,
    state: SlackDmMirrorDelivery,
    scan_authority: _SlackHistoryScanAuthority,
    response: Any,
) -> int:
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
    # Release each fetched page immediately. Idempotent source keys and the
    # per-conversation queue preserve mutation dependencies while the UI can
    # begin receiving messages before a large scan has reached its last page.
    held_until = timezone.now()
    for message in history:
        message = dict(message)
        parent_message_id = str(message.get("thread_ts") or "").strip()
        parent_outside_window = False
        if (
            parent_message_id
            and parent_message_id != str(message.get("ts") or "")
            and scan_authority.oldest
        ):
            parent_seconds, _ = _slack_ts_sort_key(parent_message_id)
            parent_outside_window = parent_seconds < int(scan_authority.oldest)
        if parent_outside_window:
            # Do not import an old private thread merely because it has a recent
            # reply. Preserve the recent message as a top-level item and retain
            # only a content-free audit marker for the detached relationship.
            message["_mlai_original_thread_ts"] = parent_message_id
            message["thread_ts"] = ""
        _enqueue_history_message(
            conversation,
            message,
            scan_authority=scan_authority,
            held_until=held_until,
        )
        parent_message_id = str(message.get("thread_ts") or "").strip()
        if parent_message_id and parent_message_id != str(message.get("ts") or ""):
            _ensure_thread_state(
                conversation,
                parent_message_id,
                scan_epoch=scan_authority.epoch,
            )
        if (
            int(message.get("reply_count") or 0) > 0
            or bool(message.get("latest_reply"))
            or int(message.get("reply_users_count") or 0) > 0
        ):
            _ensure_thread_state(
                conversation,
                str(message.get("ts") or "").strip(),
                scan_epoch=scan_authority.epoch,
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
        state_metadata = dict(state.metadata or {})
        state_metadata["complete"] = True
        state.metadata = state_metadata
        state.save(update_fields=("metadata", "updated_at"))
    conversation.last_error = ""
    conversation.save(update_fields=tuple(update_fields))
    if (
        not has_more
        and _next_incomplete_thread_state(
            conversation,
            scan_epoch=scan_authority.epoch,
        )
        is None
    ):
        _finish_history_scan(conversation)
    return len(history)


def _enqueue_reply_page(
    conversation_id: int,
    grant_id: int,
    slack_conversation_id: str,
    authority: _SlackGrantApiAuthority,
    scan_authority: _SlackHistoryScanAuthority,
    state: SlackDmMirrorDelivery,
) -> int:
    metadata = dict(state.metadata or {})
    parent_message_id = str(metadata.get("parent_ts") or "").strip()
    if not parent_message_id:
        raise SlackDmMirrorError("Slack thread scan state is invalid.")
    request_kwargs: dict[str, Any] = {
        "channel": slack_conversation_id,
        "ts": parent_message_id,
        "limit": HISTORY_PAGE_LIMIT,
    }
    cursor = str(metadata.get("cursor") or "").strip()
    if cursor:
        request_kwargs["cursor"] = cursor
    if scan_authority.oldest:
        request_kwargs["oldest"] = scan_authority.oldest
        request_kwargs["inclusive"] = True
    required_scopes = _history_required_scopes(slack_conversation_id)
    response = _call_slack_with_grant_authority(
        authority,
        "conversations_replies",
        required_scopes=required_scopes,
        **request_kwargs,
    )
    return _persist_reply_page(
        conversation_id,
        grant_id,
        state.pk,
        parent_message_id,
        authority,
        scan_authority,
        required_scopes,
        response,
    )


def _persist_reply_page(
    conversation_id: int,
    grant_id: int,
    state_id: int,
    parent_message_id: str,
    authority: _SlackGrantApiAuthority,
    scan_authority: _SlackHistoryScanAuthority,
    required_scopes: set[str] | frozenset[str],
    response: Any,
) -> int:
    with transaction.atomic():
        conversation, _ = _locked_history_write_context(
            conversation_id,
            grant_id,
            authority,
            required_scopes,
            scan_authority=scan_authority,
        )
        state = SlackDmMirrorDelivery.objects.select_for_update().get(
            pk=state_id,
            conversation=conversation,
        )
        if str((state.metadata or {}).get("scan_epoch") or "") != scan_authority.epoch:
            raise SlackDmMirrorAuthorizationError(
                "Slack thread scan was replaced before the response was stored."
            )
        return _persist_reply_page_locked(
            conversation,
            state,
            parent_message_id,
            scan_authority,
            response,
        )


def _persist_reply_page_locked(
    conversation: SlackDmMirrorConversation,
    state: SlackDmMirrorDelivery,
    parent_message_id: str,
    scan_authority: _SlackHistoryScanAuthority,
    response: Any,
) -> int:
    metadata = dict(state.metadata or {})
    participant_ids = set(conversation.participant_slack_ids or [])
    messages = []
    for message in response.get("messages") or []:
        if not isinstance(message, dict) or message.get("bot_id"):
            continue
        message_id = str(message.get("ts") or "").strip()
        author_id = str(message.get("user") or "").strip()
        if not message_id or author_id not in participant_ids:
            continue
        _slack_ts_sort_key(message_id)
        if (
            message_id != parent_message_id
            and scan_authority.oldest
            and _slack_ts_sort_key(message_id)[0] < int(scan_authority.oldest)
        ):
            continue
        messages.append(message)
    messages.sort(key=lambda message: _slack_ts_sort_key(str(message.get("ts") or "")))
    held_until = timezone.now()
    for message in messages:
        message = dict(message)
        message["thread_ts"] = str(message.get("thread_ts") or parent_message_id)
        _enqueue_history_message(
            conversation,
            message,
            scan_authority=scan_authority,
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
    main_state = (
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform__in=(
                CommunityBridgePlatform.SLACK,
                CommunityBridgePlatform.BUZZ,
            ),
            source_message_id=HISTORY_MAIN_STATE_ID,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
        .first()
    )
    main_complete = bool(
        main_state is not None
        and str((main_state.metadata or {}).get("scan_epoch") or "")
        == scan_authority.epoch
        and (main_state.metadata or {}).get("complete")
    )
    if (
        not has_more
        and main_complete
        and _next_incomplete_thread_state(
            conversation,
            scan_epoch=scan_authority.epoch,
        )
        is None
    ):
        _finish_history_scan(conversation)
    return len(messages)


def _enqueue_history_message(
    conversation: SlackDmMirrorConversation,
    message: dict[str, Any],
    *,
    scan_authority: _SlackHistoryScanAuthority,
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
        "history_scan_epoch": scan_authority.epoch,
    }
    original_thread_ts = str(message.get("_mlai_original_thread_ts") or "").strip()
    if original_thread_ts:
        metadata.update(
            {
                "original_thread_ts": original_thread_ts,
                "thread_parent_outside_history_window": True,
            }
        )
    client_message_id = str(message.get("client_msg_id") or "").strip()
    outbound_create_query = SlackDmMirrorDelivery.objects.select_for_update().filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        operation=CommunityBridgeDeliveryType.CREATE,
    )
    outbound_create = None
    if client_message_id:
        outbound_create = (
            outbound_create_query.filter(metadata__client_msg_id=client_message_id)
            .order_by("-id")
            .first()
        )
    if outbound_create is None:
        outbound_create = (
            outbound_create_query.filter(
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                metadata__slack_ts=message_id,
            )
            .order_by("-id")
            .first()
        )
    if outbound_create is None:
        _upsert_history_delivery(
            conversation,
            source_message_id=message_id,
            author_id=author_id,
            operation=CommunityBridgeDeliveryType.CREATE,
            text=text,
            metadata=metadata,
            held_until=held_until,
        )
    elif outbound_create.status in {
        CommunityBridgeDeliveryStatus.PENDING,
        CommunityBridgeDeliveryStatus.PROCESSING,
        CommunityBridgeDeliveryStatus.FAILED,
    }:
        _complete_outbound_history_echo_locked(
            outbound_create,
            slack_ts=message_id,
        )
    elif bool((outbound_create.metadata or {}).get("history_reconcile_candidate")):
        outbound_metadata = dict(outbound_create.metadata or {})
        _clear_history_reconciliation_metadata(outbound_metadata)
        outbound_create.metadata = outbound_metadata
        outbound_create.save(update_fields=("metadata", "updated_at"))
    edited = message.get("edited") if isinstance(message.get("edited"), dict) else {}
    edited_timestamp = str(edited.get("ts") or "").strip()
    if edited_timestamp:
        edit_echo_key = _slack_echo_key(
            operation=CommunityBridgeDeliveryType.EDIT,
            target_message_id=message_id,
            author_id=author_id,
            text=text,
        )
        outbound_edit = _recent_or_ambiguous_outbound_mutation(
            conversation,
            operation=CommunityBridgeDeliveryType.EDIT,
            metadata_filter={"metadata__slack_echo_key": edit_echo_key},
        )
        if outbound_edit is not None:
            _complete_outbound_history_echo_locked(
                outbound_edit,
                slack_ts=message_id,
            )
        else:
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
            outbound_reaction = _recent_or_ambiguous_outbound_mutation(
                conversation,
                operation=CommunityBridgeDeliveryType.REACTION_ADD,
                metadata_filter={
                    "metadata__slack_reaction_object_id": semantic_id,
                },
            )
            if outbound_reaction is not None:
                # The exact MLAI-origin reaction already exists in Slack. Do
                # not mirror it back as a second private reaction event.
                _complete_outbound_history_echo_locked(
                    outbound_reaction,
                    slack_ts=message_id,
                )
                continue
            _upsert_history_delivery(
                conversation,
                source_message_id=semantic_id,
                author_id=reaction_author_id,
                operation=CommunityBridgeDeliveryType.REACTION_ADD,
                text=emoji,
                metadata={
                    "backfill": True,
                    "history_scan_epoch": scan_authority.epoch,
                    "event_ts": message_id,
                    "target_source_message_id": message_id,
                    "reaction_object_id": semantic_id,
                    "slack_reaction": slack_reaction,
                    "participant_hash": conversation.participant_hash,
                },
                held_until=held_until,
            )


def _recent_or_ambiguous_outbound_mutation(
    conversation: SlackDmMirrorConversation,
    *,
    operation: str,
    metadata_filter: dict[str, Any],
) -> SlackDmMirrorDelivery | None:
    return (
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            operation=operation,
        )
        .filter(
            Q(
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.PROCESSING,
                    CommunityBridgeDeliveryStatus.FAILED,
                )
            )
            | Q(
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                updated_at__gte=timezone.now()
                - timedelta(seconds=SLACK_ECHO_WINDOW_SECONDS),
            )
        )
        .filter(**metadata_filter)
        .order_by("-id")
        .first()
    )


def _complete_outbound_history_echo_locked(
    delivery: SlackDmMirrorDelivery,
    *,
    slack_ts: str,
) -> None:
    if delivery.status == CommunityBridgeDeliveryStatus.COMPLETED:
        return
    metadata = dict(delivery.metadata or {})
    existing_slack_ts = str(metadata.get("slack_ts") or "").strip()
    if existing_slack_ts and existing_slack_ts != slack_ts:
        raise SlackDmMirrorError(
            "Slack history returned a conflicting outbound message identity."
        )
    metadata["slack_ts"] = slack_ts
    _clear_history_reconciliation_metadata(metadata)
    now = timezone.now()
    delivery.metadata = metadata
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.completed_at = now
    delivery.last_error = ""
    delivery.save(
        update_fields=(
            "metadata",
            "status",
            "encrypted_text",
            "completed_at",
            "last_error",
            "updated_at",
        )
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
    if bool((delivery.metadata or {}).get("permanent_failure")):
        # Automatic hourly scans may observe the same source message forever.
        # Only explicit backfill or renewed consent clears this durable fence.
        return delivery
    if delivery.status in (
        CommunityBridgeDeliveryStatus.FAILED,
        CommunityBridgeDeliveryStatus.DEAD,
    ) or (
        delivery.status == CommunityBridgeDeliveryStatus.COMPLETED
        and bool((delivery.metadata or {}).get("history_outside_window"))
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
        delivery.source_author_id = author_id
        delivery.encrypted_text = text
        delivery.metadata = metadata
        delivery.save(
            update_fields=(
                "source_author_id",
                "encrypted_text",
                "metadata",
                "updated_at",
            )
        )
    elif (
        delivery.status == CommunityBridgeDeliveryStatus.COMPLETED
        and bool((delivery.metadata or {}).get("history_reconcile_candidate"))
    ):
        reconciled_metadata = {**(delivery.metadata or {}), **metadata}
        _clear_history_reconciliation_metadata(reconciled_metadata)
        delivery.metadata = reconciled_metadata
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
    *,
    scan_epoch: str,
) -> SlackDmMirrorDelivery:
    return _ensure_history_state(
        conversation,
        source_message_id=f"{HISTORY_STATE_PREFIX}thread:{parent_message_id}",
        metadata={
            "history_scan_state": "thread",
            "scan_epoch": scan_epoch,
            "parent_ts": parent_message_id,
            "cursor": "",
            "complete": False,
        },
    )


def _next_incomplete_thread_state(
    conversation: SlackDmMirrorConversation,
    *,
    scan_epoch: str | None = None,
) -> SlackDmMirrorDelivery | None:
    states = SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id__startswith=f"{HISTORY_STATE_PREFIX}thread:",
        operation=CommunityBridgeDeliveryType.CREATE,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
    ).order_by("id")
    return next(
        (
            state
            for state in states
            if (
                scan_epoch is None
                or str((state.metadata or {}).get("scan_epoch") or "")
                == scan_epoch
            )
            and not bool((state.metadata or {}).get("complete"))
        ),
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
    _reconcile_absent_slack_state_locked(conversation)
    _complete_dependency_reconciliation_locked(conversation)
    _release_history_deliveries(conversation)
    _supersede_unrecovered_backfill_rows_locked(conversation)
    _clear_history_scan_states([conversation.pk])


def _complete_dependency_reconciliation_locked(
    conversation: SlackDmMirrorConversation,
) -> None:
    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update().filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            status__in=(
                CommunityBridgeDeliveryStatus.PENDING,
                CommunityBridgeDeliveryStatus.PROCESSING,
                CommunityBridgeDeliveryStatus.FAILED,
            ),
            metadata__dependency_reconciliation_pending=True,
        )
    )
    now = timezone.now()
    for row in rows:
        metadata = dict(row.metadata or {})
        metadata.pop("dependency_reconciliation_pending", None)
        metadata["dependency_reconciliation_complete"] = True
        row.metadata = metadata
        row.available_at = now
        row.updated_at = now
    if rows:
        SlackDmMirrorDelivery.objects.bulk_update(
            rows,
            ("metadata", "available_at", "updated_at"),
        )


def _reconcile_absent_slack_state_locked(
    conversation: SlackDmMirrorConversation,
) -> None:
    """Turn messages absent from a complete scan into exact delete deltas.

    Slack history reaction actor lists may be partial, so reaction removals are
    reconciled only from authoritative live Events API callbacks.
    """

    state = (
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=HISTORY_MAIN_STATE_ID,
            operation=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        )
        .first()
    )
    state_metadata = dict(state.metadata or {}) if state is not None else {}
    state_reconcile_epoch = str(
        state_metadata.get(HISTORY_RECONCILE_EPOCH_KEY) or ""
    )
    state_reconcile_oldest = str(state_metadata.get("oldest") or "")
    candidates = list(
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform__in=(
                CommunityBridgePlatform.SLACK,
                CommunityBridgePlatform.BUZZ,
            ),
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__history_reconcile_candidate=True,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        .order_by("id")
    )
    now = timezone.now()
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        candidate_reconcile_epoch = str(
            metadata.get(HISTORY_RECONCILE_EPOCH_KEY) or ""
        )
        candidate_reconcile_oldest = str(
            metadata.get(HISTORY_RECONCILE_OLDEST_KEY) or ""
        )
        exact_scan_boundary = bool(
            state_reconcile_epoch
            and candidate_reconcile_epoch == state_reconcile_epoch
            and candidate_reconcile_oldest == state_reconcile_oldest
        )
        _clear_history_reconciliation_metadata(metadata)
        candidate.metadata = metadata
        candidate.save(update_fields=("metadata", "updated_at"))
        if not exact_scan_boundary:
            # Legacy or superseded candidates did not share this scan's fixed
            # cutoff. Clear them without inferring a destructive deletion.
            continue
        semantic_target = (
            candidate.source_message_id
            if candidate.source_platform == CommunityBridgePlatform.SLACK
            else str(metadata.get("slack_ts") or "").strip()
        )
        outbound_delete_exists = bool(
            semantic_target
            and SlackDmMirrorDelivery.objects.filter(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.BUZZ,
                operation=CommunityBridgeDeliveryType.DELETE,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                metadata__slack_ts=semantic_target,
            ).exists()
        )
        if outbound_delete_exists:
            continue
        if not semantic_target or SlackDmMirrorDelivery.objects.filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            operation=CommunityBridgeDeliveryType.DELETE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            metadata__target_source_message_id=semantic_target,
        ).exists():
            continue
        operation = CommunityBridgeDeliveryType.DELETE
        removal_metadata = {
            "backfill": True,
            "history_reconciliation": True,
            "event_ts": f"{int(now.timestamp())}.000000",
            "target_source_message_id": semantic_target,
            "participant_hash": conversation.participant_hash,
        }
        source_id = "reconcile:" + hashlib.sha256(
            f"{operation}\0{semantic_target}".encode("utf-8")
        ).hexdigest()
        SlackDmMirrorDelivery.objects.get_or_create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=source_id,
            operation=operation,
            defaults={
                "source_author_id": (
                    candidate.source_author_id
                    if candidate.source_platform == CommunityBridgePlatform.SLACK
                    else conversation.grant.slack_user_id
                ),
                "encrypted_text": "",
                "metadata": removal_metadata,
                "available_at": now,
            },
        )


def _supersede_unrecovered_backfill_rows_locked(
    conversation: SlackDmMirrorConversation,
) -> None:
    """Retain idempotency tombstones for rows absent from a recovery scan."""

    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            metadata__history_recovery_scheduled=True,
            status__in=(
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.DEAD,
            ),
        )
        .exclude(metadata__permanent_failure=True)
        .order_by("id")
    )
    now = timezone.now()
    for row in rows:
        metadata = dict(row.metadata or {})
        metadata.pop("history_recovery_scheduled", None)
        metadata["history_recovery_superseded"] = True
        row.metadata = metadata
        row.encrypted_text = ""
        row.last_error = "Superseded by completed Slack history reconciliation"
        row.updated_at = now
    if rows:
        SlackDmMirrorDelivery.objects.bulk_update(
            rows,
            ("metadata", "encrypted_text", "last_error", "updated_at"),
        )


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
    # A claimed body must linearize on the same grant lock as revoke. Holding
    # that boundary through the private network call means revoke either wins
    # before the body is read or waits and erases it immediately afterwards;
    # no post-I/O stale save can resurrect revoked content.
    conversation_id = delivery.conversation_id
    grant_id = delivery.conversation.grant_id
    grant_snapshot = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(pk=grant_id)
        .first()
    )
    if grant_snapshot is None:
        raise SlackDmMirrorAuthorizationError(
            "The private delivery is no longer authorized."
        )
    _refresh_slack_grant_token_if_due(grant_snapshot)
    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update(of=("self",))
            .select_related("connection")
            .filter(pk=grant_id)
            .first()
        )
        if grant is None:
            raise SlackDmMirrorAuthorizationError(
                "The private delivery is no longer authorized."
            )
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(pk=conversation_id, grant=grant)
            .first()
        )
        if conversation is None:
            raise SlackDmMirrorAuthorizationError(
                "The private delivery is no longer authorized."
            )
        locked_device = None
        claimed_source_platform = delivery.source_platform
        claimed_source_author_id = str(delivery.source_author_id or "").strip().lower()
        if claimed_source_platform == CommunityBridgePlatform.BUZZ:
            if claimed_source_author_id not in _conversation_owner_device_pubkeys(
                conversation
            ):
                raise SlackDmMirrorAuthorizationError(
                    "The originating MLAI Chat device is no longer authorized."
                )
            locked_device = _locked_active_verified_device(
                grant.user_id,
                claimed_source_author_id,
            )
            if locked_device is None:
                raise SlackDmMirrorAuthorizationError(
                    "The originating MLAI Chat device is no longer authorized."
                )
        delivery = (
            SlackDmMirrorDelivery.objects.select_for_update()
            .filter(pk=delivery.pk, conversation=conversation)
            .first()
        )
        if delivery is None:
            raise SlackDmMirrorAuthorizationError(
                "The private delivery is no longer authorized."
            )
        if (
            delivery.source_platform != claimed_source_platform
            or str(delivery.source_author_id or "").strip().lower()
            != claimed_source_author_id
        ):
            raise SlackDmMirrorAuthorizationError(
                "The private delivery authority changed while it was claimed."
            )
        conversation.grant = grant
        delivery.conversation = conversation
        _assert_private_delivery_authorized_locked(
            delivery,
            locked_device=locked_device,
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


def _deliver_private_batch(claimed: list[SlackDmMirrorDelivery]) -> None:
    """Deliver ordered top-level Slack creates under one revocation fence."""

    if not claimed or any(
        delivery.conversation_id != claimed[0].conversation_id
        or not _private_delivery_batch_eligible(delivery)
        for delivery in claimed
    ):
        raise SlackDmMirrorError("Private delivery batch is invalid.")
    conversation_id = claimed[0].conversation_id
    grant_id = claimed[0].conversation.grant_id
    grant_snapshot = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(pk=grant_id)
        .first()
    )
    if grant_snapshot is None:
        raise SlackDmMirrorAuthorizationError(
            "The private delivery batch is no longer authorized."
        )
    _refresh_slack_grant_token_if_due(grant_snapshot)
    claimed_ids = [delivery.pk for delivery in claimed]
    with transaction.atomic():
        grant = (
            SlackDmMirrorGrant.objects.select_for_update(of=("self",))
            .select_related("connection")
            .filter(pk=grant_id)
            .first()
        )
        if grant is None:
            raise SlackDmMirrorAuthorizationError(
                "The private delivery batch is no longer authorized."
            )
        conversation = (
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(pk=conversation_id, grant=grant)
            .first()
        )
        if conversation is None:
            raise SlackDmMirrorAuthorizationError(
                "The private delivery batch is no longer authorized."
            )
        conversation.grant = grant
        deliveries_by_id = {
            delivery.pk: delivery
            for delivery in SlackDmMirrorDelivery.objects.select_for_update().filter(
                pk__in=claimed_ids,
                conversation=conversation,
            )
        }
        deliveries = [deliveries_by_id.get(delivery_id) for delivery_id in claimed_ids]
        if any(delivery is None for delivery in deliveries):
            raise SlackDmMirrorAuthorizationError(
                "The private delivery batch is no longer authorized."
            )

        payloads = []
        source_metadata_by_id: dict[int, dict[str, Any]] = {}
        for delivery in deliveries:
            if delivery is None:
                continue
            delivery.conversation = conversation
            _assert_private_delivery_authorized_locked(delivery)
            if not _private_delivery_batch_eligible(delivery):
                raise SlackDmMirrorError("Private delivery batch changed while claimed.")
            linked_pubkey = str(
                (conversation.participant_identity_map or {}).get(
                    delivery.source_author_id
                )
                or ""
            ).lower()
            if not linked_pubkey:
                raise SlackDmMirrorError(
                    "Slack author is not part of this owner mirror."
                )
            profile = (conversation.participant_profiles or {}).get(
                delivery.source_author_id
            ) or {}
            source_metadata = dict(delivery.metadata or {})
            source_metadata_by_id[delivery.pk] = source_metadata
            payloads.append(
                {
                    "delivery_id": str(delivery.pk),
                    "created_at": _delivery_created_at(delivery),
                    "operation": delivery.operation,
                    "channel_id": str(conversation.mlai_channel_id),
                    "participant_pubkeys": sorted(
                        conversation.participant_buzz_pubkeys or []
                    ),
                    "text": delivery.encrypted_text,
                    "source_workspace_id": conversation.slack_workspace_id,
                    "source_channel_id": conversation.slack_conversation_id,
                    "source_message_id": delivery.source_message_id,
                    "source_author_id": delivery.source_author_id,
                    "source_author_display_name": str(
                        profile.get("display_name") or delivery.source_author_id
                    ),
                    "source_author_avatar_url": str(profile.get("avatar_url") or "")
                    or None,
                    "linked_pubkey": linked_pubkey,
                    "target_message_id": None,
                    "parent_message_id": None,
                }
            )
        results = BuzzBridgeClient.deliver_private_batch(payloads)
        if len(results) != len(deliveries):
            raise SlackDmMirrorError("Private delivery batch response is incomplete.")

        now = timezone.now()
        latest_timestamp = ""
        for delivery, result in zip(deliveries, results):
            if delivery is None:
                continue
            _assert_private_delivery_authorized_locked(delivery)
            source_metadata = source_metadata_by_id[delivery.pk]
            delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
            delivery.encrypted_text = ""
            delivery.metadata = {
                **source_metadata,
                "participant_hash": conversation.participant_hash,
                "destination_message_id": str(result.get("message_id") or ""),
                "destination_parent_message_id": str(
                    result.get("parent_message_id") or ""
                ),
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
            event_timestamp = str(
                source_metadata.get("event_ts") or delivery.source_message_id
            ).strip()
            try:
                _slack_ts_sort_key(event_timestamp)
            except SlackDmMirrorError:
                continue
            if not latest_timestamp or _slack_ts_sort_key(
                event_timestamp
            ) > _slack_ts_sort_key(latest_timestamp):
                latest_timestamp = event_timestamp
        conversation.last_synced_at = now
        if latest_timestamp:
            conversation.latest_synced_ts = latest_timestamp
        conversation.save(
            update_fields=("last_synced_at", "latest_synced_ts", "updated_at")
        )
        grant.last_synced_at = now
        grant.save(update_fields=("last_synced_at", "updated_at"))


def _assert_private_delivery_authorized_locked(
    delivery: SlackDmMirrorDelivery,
    *,
    locked_device: CommunityChatDevice | None = None,
) -> None:
    """Recheck a locked delivery immediately before body I/O or persistence."""

    conversation = delivery.conversation
    grant = conversation.grant
    _assert_grant_connection_authorized(grant)
    if (
        delivery.status != CommunityBridgeDeliveryStatus.PROCESSING
        or conversation.status != SlackDmMirrorConversationStatus.LIVE
        or grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
    ):
        raise SlackDmMirrorAuthorizationError(
            "The private delivery is no longer authorized."
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
    if delivery.source_platform == CommunityBridgePlatform.BUZZ:
        source_author_pubkey = str(delivery.source_author_id or "").strip().lower()
        if (
            source_author_pubkey not in _conversation_owner_device_pubkeys(conversation)
            or (
                locked_device is not None
                and (
                    locked_device.user_id != grant.user_id
                    or locked_device.public_key.lower() != source_author_pubkey
                    or locked_device.status != DeviceBindingStatus.VERIFIED
                    or locked_device.revoked_at is not None
                )
            )
            or (
                locked_device is None
                and _active_verified_device(grant.user_id, source_author_pubkey) is None
            )
        ):
            raise SlackDmMirrorAuthorizationError(
                "The originating MLAI Chat device is no longer authorized."
            )


def _dependency_rows_can_progress(rows) -> bool:
    for status, source_platform, metadata in rows.values_list(
        "status",
        "source_platform",
        "metadata",
    ):
        if status in {
            CommunityBridgeDeliveryStatus.PENDING,
            CommunityBridgeDeliveryStatus.PROCESSING,
            CommunityBridgeDeliveryStatus.FAILED,
        }:
            return True
        if (
            status == CommunityBridgeDeliveryStatus.DEAD
            and source_platform == CommunityBridgePlatform.SLACK
            and not bool((metadata or {}).get("history_recovery_superseded"))
        ):
            return True
    return False


def _dependency_arrival_grace_active(delivery: SlackDmMirrorDelivery) -> bool:
    """Keep a live operation intact until exact source reconciliation finishes."""

    metadata = dict(delivery.metadata or {})
    if (
        bool(metadata.get("backfill"))
        or bool(metadata.get("thread_parent_outside_history_window"))
        or bool(metadata.get("dependency_outside_history"))
    ):
        return False
    if delivery.source_platform == CommunityBridgePlatform.SLACK:
        return not bool(metadata.get("dependency_reconciliation_complete"))
    return delivery.created_at >= timezone.now() - timedelta(
        seconds=DEPENDENCY_ARRIVAL_GRACE_SECONDS
    )


def _slack_target_dependency_can_progress(
    conversation: SlackDmMirrorConversation,
    source_message_id: str,
) -> bool:
    target = str(source_message_id or "").strip().lower()
    rows = SlackDmMirrorDelivery.objects.filter(conversation=conversation).filter(
        Q(
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=target,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        | Q(
            source_platform=CommunityBridgePlatform.SLACK,
            operation=CommunityBridgeDeliveryType.CREATE,
            metadata__destination_message_id=target,
        )
    )
    return _dependency_rows_can_progress(rows)


def _mlai_target_dependency_can_progress(
    conversation: SlackDmMirrorConversation,
    source_message_id: str,
    *,
    operation: str = CommunityBridgeDeliveryType.CREATE,
) -> bool:
    target = str(source_message_id or "").strip()
    rows = SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        operation=operation,
    ).filter(
        Q(
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=target,
        )
        | Q(
            source_platform=CommunityBridgePlatform.SLACK,
            metadata__reaction_object_id=target,
        )
        | Q(
            source_platform=CommunityBridgePlatform.BUZZ,
            metadata__slack_ts=target,
        )
    )
    return _dependency_rows_can_progress(rows)


def _complete_superseded_dependency_locked(
    delivery: SlackDmMirrorDelivery,
    *,
    reason: str,
) -> None:
    """Content-free completion for a mutation whose target cannot arrive."""

    _assert_private_delivery_authorized_locked(delivery)
    metadata = dict(delivery.metadata or {})
    metadata.update(
        {
            "dependency_superseded": True,
            "dependency_superseded_reason": reason,
        }
    )
    now = timezone.now()
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.metadata = metadata
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


def _mutation_sequence_key(delivery: SlackDmMirrorDelivery) -> str:
    metadata = delivery.metadata or {}
    if delivery.operation in {
        CommunityBridgeDeliveryType.EDIT,
        CommunityBridgeDeliveryType.DELETE,
    }:
        return str(metadata.get("target_source_message_id") or "").strip()
    if delivery.operation in {
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    }:
        return str(metadata.get("reaction_object_id") or "").strip()
    return ""


def _event_sequence_value(delivery: SlackDmMirrorDelivery) -> tuple[float, int]:
    raw = str((delivery.metadata or {}).get("event_ts") or "").strip()
    try:
        return float(raw), delivery.pk
    except (TypeError, ValueError):
        return delivery.created_at.timestamp(), delivery.pk


def _supersede_stale_slack_mutation_locked(
    delivery: SlackDmMirrorDelivery,
) -> bool:
    """Prevent delayed older Slack mutations from overwriting newer state."""

    if delivery.source_platform != CommunityBridgePlatform.SLACK:
        return False
    sequence_key = _mutation_sequence_key(delivery)
    if not sequence_key:
        return False
    current_value = _event_sequence_value(delivery)
    peers = SlackDmMirrorDelivery.objects.filter(
        conversation=delivery.conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
        operation__in=(
            CommunityBridgeDeliveryType.EDIT,
            CommunityBridgeDeliveryType.DELETE,
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        ),
    ).exclude(pk=delivery.pk)
    for peer in peers:
        if _mutation_sequence_key(peer) != sequence_key:
            continue
        if _event_sequence_value(peer) > current_value:
            _complete_superseded_dependency_locked(
                delivery,
                reason="A newer Slack mutation was already mirrored.",
            )
            return True
    return False


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
    client = WebClient(
        token=grant.connection.access_token,
        timeout=_slack_sdk_timeout_seconds(),
    )
    client_message_id = ""
    slack_ts = ""
    reaction = ""
    if operation == CommunityBridgeDeliveryType.CREATE:
        client_message_id = str(source_metadata.get("client_msg_id") or "").strip()
        if not client_message_id:
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
                if _slack_target_dependency_can_progress(
                    conversation,
                    parent_source_id,
                ) or _dependency_arrival_grace_active(delivery):
                    raise SlackDmMirrorDependencyPending(
                        "The private thread parent has not reached Slack yet."
                    )
                source_metadata.update(
                    {
                        "original_source_parent_message_id": parent_source_id,
                        "source_parent_message_id": "",
                        "thread_parent_unavailable": True,
                    }
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
            if _slack_target_dependency_can_progress(
                conversation,
                target_source_id,
            ) or _dependency_arrival_grace_active(delivery):
                raise SlackDmMirrorDependencyPending(
                    "The original private message has not reached Slack yet."
                )
            _complete_superseded_dependency_locked(
                delivery,
                reason="The target MLAI Chat message is outside the mirrored history.",
            )
            return
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
    _assert_private_delivery_authorized_locked(delivery)
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
    if _supersede_stale_slack_mutation_locked(delivery):
        return
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
                if _mlai_target_dependency_can_progress(
                    conversation,
                    source_parent_id,
                ) or _dependency_arrival_grace_active(delivery):
                    raise SlackDmMirrorDependencyPending(
                        "The private thread parent has not reached MLAI Chat yet."
                    )
                source_metadata = dict(source_metadata)
                source_metadata.update(
                    {
                        "original_thread_ts": source_parent_id,
                        "thread_ts": "",
                        "thread_parent_unavailable": True,
                    }
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
            if _mlai_target_dependency_can_progress(
                conversation,
                reaction_object,
                operation=CommunityBridgeDeliveryType.REACTION_ADD,
            ) or _dependency_arrival_grace_active(delivery):
                raise SlackDmMirrorDependencyPending(
                    "The private reaction has not reached MLAI Chat yet."
                )
            _complete_superseded_dependency_locked(
                delivery,
                reason="The mirrored reaction target is unavailable.",
            )
            return
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
            if _mlai_target_dependency_can_progress(
                conversation,
                target_source_message_id,
            ) or _dependency_arrival_grace_active(delivery):
                raise SlackDmMirrorDependencyPending(
                    "The original private message has not reached MLAI Chat yet."
                )
            _complete_superseded_dependency_locked(
                delivery,
                reason="The target Slack message is outside the mirrored history.",
            )
            return
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
    _assert_private_delivery_authorized_locked(delivery)
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


def _assert_grant_connection_authorized(grant: SlackDmMirrorGrant) -> None:
    """Reject private I/O unless the credential still matches this grant."""

    connection = grant.connection
    try:
        identity = _connection_identity(connection)
    except SlackDmMirrorError as exc:
        raise SlackDmMirrorAuthorizationError(
            "The Slack credential no longer matches this private mirror."
        ) from exc
    if (
        connection.provider != ExternalServiceProvider.SLACK
        or connection.user_id != grant.user_id
        or connection.status
        not in (
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        )
        or not str(connection.access_token or "").strip()
        or not DIRECT_DM_SCOPES.issubset(set(connection.scopes or []))
        or identity != (grant.slack_workspace_id, grant.slack_user_id)
    ):
        raise SlackDmMirrorAuthorizationError(
            "The Slack credential no longer matches this private mirror."
        )


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
        get_user_model().objects.select_for_update().get(pk=grant.user_id)
        locked_grant = (
            SlackDmMirrorGrant.objects.select_for_update(of=("self",))
            .select_related("connection", "user")
            .get(pk=grant.pk)
        )
        if (
            locked_grant.status != SlackDmMirrorGrantStatus.ACTIVE
            or locked_grant.revoked_at is not None
        ):
            raise SlackDmMirrorAuthorizationError(
                "Slack DM mirroring is no longer active."
            )
        conversations = list(
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(grant=locked_grant)
            .order_by("id")
        )
        requested_device = None
        if requested_key:
            requested_device = _locked_active_verified_device(
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
            linked_device = _locked_active_verified_device(
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
            if target_device is not None:
                target_device = _locked_active_verified_device(
                    locked_grant.user_id,
                    target_device.public_key,
                )
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
        conversation_ids = [conversation.pk for conversation in conversations]
        for conversation in conversations:
            conversation.grant = locked_grant
            _prepare_conversation_registration_cleanup_locked(
                locked_grant,
                conversation,
                reason="Owner device identity changed",
            )
        _clear_history_scan_states(conversation_ids)
        for conversation in conversations:
            conversation.history_backfilled_at = None
            conversation.oldest_synced_ts = ""
            conversation.latest_synced_ts = ""
            conversation.mlai_channel_id = None
            conversation.last_error = ""
            if conversation.status != SlackDmMirrorConversationStatus.PAUSED:
                conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
            conversation.save(
                update_fields=(
                    "history_backfilled_at",
                    "oldest_synced_ts",
                    "latest_synced_ts",
                    "mlai_channel_id",
                    "status",
                    "last_error",
                    "updated_at",
                )
            )
        private_deliveries = SlackDmMirrorDelivery.objects.filter(
            conversation_id__in=conversation_ids,
        ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX)
        private_deliveries.update(encrypted_text="", updated_at=now)
        private_deliveries.filter(
            status__in=(
                CommunityBridgeDeliveryStatus.PENDING,
                CommunityBridgeDeliveryStatus.PROCESSING,
                CommunityBridgeDeliveryStatus.FAILED,
                CommunityBridgeDeliveryStatus.COMPLETED,
            )
        ).update(
            status=CommunityBridgeDeliveryStatus.DEAD,
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


def _locked_active_verified_device(
    user_id: int,
    public_key: str,
) -> CommunityChatDevice | None:
    """Return and lock an active device inside the caller's transaction."""

    return (
        CommunityChatDevice.objects.select_for_update()
        .filter(
            user_id=user_id,
            public_key=str(public_key or "").strip().lower(),
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        )
        .first()
    )


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
    """Return the configured import window, capped at thirty days.

    Zero previously meant unbounded retained Slack history. Keep the runtime
    fail-safe even if a deployment still has that legacy value configured.
    """

    try:
        configured = int(getattr(settings, "SLACK_DM_MIRROR_HISTORY_DAYS", 30))
    except (TypeError, ValueError):
        configured = MAX_HISTORY_DAYS
    return max(1, min(configured, MAX_HISTORY_DAYS))


def _bounded_history_days(value: Any) -> int:
    configured = _history_days()
    try:
        current = int(value)
    except (TypeError, ValueError):
        current = configured
    if current <= 0:
        return configured
    return min(current, configured, MAX_HISTORY_DAYS)


def _normalize_grant_history_window_locked(grant: SlackDmMirrorGrant) -> bool:
    """Fence legacy unbounded scans before any further Slack history I/O."""

    bounded_days = _bounded_history_days(grant.history_days)
    if grant.history_days == bounded_days:
        return False
    grant.history_days = bounded_days
    grant.save(update_fields=("history_days", "updated_at"))
    conversation_ids = list(
        SlackDmMirrorConversation.objects.select_for_update()
        .filter(
            grant=grant,
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        .values_list("id", flat=True)
    )
    _clear_history_scan_states(conversation_ids)
    _clear_permanent_recovery_fences_locked(conversation_ids)
    now = timezone.now()
    SlackDmMirrorConversation.objects.filter(pk__in=conversation_ids).update(
        history_backfilled_at=None,
        oldest_synced_ts="",
        latest_synced_ts="",
        last_error="",
        updated_at=now,
    )
    _mark_backfill_rows_for_recovery_locked(conversation_ids, now=now)
    return True


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
    authority: _SlackGrantApiAuthority,
    slack_user_id: str,
    cache: dict[str, dict[str, str]],
    *,
    required_scopes: set[str] | frozenset[str],
) -> dict[str, str]:
    cached = cache.get(slack_user_id)
    if cached is not None:
        return cached
    response = _call_slack_with_grant_authority(
        authority,
        "users_info",
        required_scopes=required_scopes,
        user=slack_user_id,
    )
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
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
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
    authority: _SlackGrantApiAuthority,
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

    # Slack explicitly documents the embedded MPIM member list as potentially
    # truncated. Always page the authoritative members endpoint; otherwise a
    # tenth participant or a removed owner can be missed and private content
    # can be provisioned with the wrong boundary.
    participant_ids: set[str] = set()
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        response = _call_slack_with_grant_authority(
            authority,
            "conversations_members",
            required_scopes=DIRECT_DM_SCOPES | GROUP_DM_SCOPES,
            channel=channel_id,
            limit=200,
            cursor=cursor,
        )
        participant_ids.update(
            str(value or "").strip()
            for value in response.get("members") or []
            if str(value or "").strip()
        )
        next_cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if next_cursor and next_cursor in seen_cursors:
            raise SlackDmMirrorError(
                "Slack member pagination made no progress."
            )
        if not next_cursor:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    if owner_slack_user_id not in participant_ids or not 2 <= len(participant_ids) <= 9:
        return []
    return sorted(participant_ids)
