from dataclasses import dataclass
from uuid import UUID

from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeDeletionRequest,
    CommunityBridgeDeletionRequestStatus,
    CommunityBridgeIdentityLink,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.community_bridge.slack import SlackBridgeClient


class SlackDeletionError(Exception):
    """A stable client-facing failure from the Slack deletion broker."""

    def __init__(self, code: str, *, http_status: int, detail: str):
        self.code = code
        self.http_status = http_status
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class SlackDeletionResult:
    request_id: int
    status: str


def delete_slack_origin_message(
    *,
    user,
    device_public_key: str,
    buzz_event_id: str,
    idempotency_key: UUID,
) -> SlackDeletionResult:
    """Delete an owned Slack-origin message using its author's Slack token.

    The Slack Events callback remains the authoritative signal that removes the
    corresponding Buzz event. This command records intent and asks Slack to
    delete; it never forges a bridge-key deletion locally.
    """

    event_id = str(buzz_event_id or "").strip().lower()
    link = (
        CommunityBridgeMessageLink.objects.select_related("channel")
        .filter(
            source_platform=CommunityBridgePlatform.SLACK,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_message_id=event_id,
        )
        .first()
    )
    if link is None:
        raise SlackDeletionError(
            "message_not_found",
            http_status=404,
            detail="This mirrored Slack message could not be found.",
        )

    _require_message_owner(
        user=user,
        device_public_key=device_public_key,
        link=link,
    )

    request_row, created = CommunityBridgeDeletionRequest.objects.get_or_create(
        user=user,
        idempotency_key=idempotency_key,
        defaults={
            "message_link": link,
            "status": CommunityBridgeDeletionRequestStatus.PROCESSING,
            "slack_workspace_id": link.channel.slack_workspace_id,
            "slack_channel_id": link.source_channel_id,
            "slack_message_id": link.source_message_id,
            "buzz_event_id": event_id,
        },
    )
    if not created:
        if request_row.message_link_id != link.id:
            raise SlackDeletionError(
                "idempotency_conflict",
                http_status=409,
                detail="This deletion request key is already bound to another message.",
            )
        return SlackDeletionResult(request_row.id, request_row.status)

    if link.source_deleted_at is not None:
        _complete_request(
            request_row,
            status=CommunityBridgeDeletionRequestStatus.ALREADY_DELETED,
        )
        return SlackDeletionResult(
            request_row.id,
            CommunityBridgeDeletionRequestStatus.ALREADY_DELETED,
        )

    try:
        connection = _resolve_user_slack_connection(user=user, link=link)
        response = SlackBridgeClient.delete_message_as_user(
            access_token=connection.access_token,
            channel_id=link.source_channel_id,
            message_id=link.source_message_id,
        )
    except SlackDeletionError as exc:
        _fail_request(request_row, exc.code)
        raise
    except SlackApiError as exc:
        error_code = str(exc.response.get("error") or "slack_delete_failed")
        if error_code == "message_not_found":
            now = timezone.now()
            CommunityBridgeMessageLink.objects.filter(id=link.id).update(
                source_deleted_at=now,
                updated_at=now,
            )
            _complete_request(
                request_row,
                status=CommunityBridgeDeletionRequestStatus.ALREADY_DELETED,
                provider_response={"error": error_code},
            )
            return SlackDeletionResult(
                request_row.id,
                CommunityBridgeDeletionRequestStatus.ALREADY_DELETED,
            )
        client_error = _map_slack_error(error_code)
        _fail_request(request_row, client_error.code, {"error": error_code})
        raise client_error from exc

    _complete_request(
        request_row,
        status=CommunityBridgeDeletionRequestStatus.SUCCEEDED,
        provider_response=response,
    )
    return SlackDeletionResult(
        request_row.id,
        CommunityBridgeDeletionRequestStatus.SUCCEEDED,
    )


def _require_message_owner(*, user, device_public_key: str, link) -> None:
    public_key = str(device_public_key or "").strip().lower()
    owns_device = CommunityChatDevice.objects.filter(
        user=user,
        public_key=public_key,
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    ).exists()
    owns_slack_identity = CommunityBridgeIdentityLink.objects.filter(
        user=user,
        slack_workspace_id=link.channel.slack_workspace_id,
        slack_user_id=link.source_author_id,
        revoked_at__isnull=True,
    ).exists()
    if not owns_device or not owns_slack_identity:
        raise SlackDeletionError(
            "not_message_owner",
            http_status=403,
            detail="Only the message author can delete this Slack message.",
        )


def _resolve_user_slack_connection(*, user, link) -> ExternalServiceConnection:
    connection = (
        ExternalServiceConnection.objects.filter(
            user=user,
            provider=ExternalServiceProvider.SLACK,
            status=ExternalServiceConnectionStatus.CONNECTED,
            external_account_id=link.channel.slack_workspace_id,
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    if connection is None or not connection.access_token:
        raise _reauthorization_required()
    scopes = {str(scope).strip() for scope in (connection.scopes or [])}
    metadata = dict(connection.provider_metadata or {})
    authed_user = dict(metadata.get("authed_user") or {})
    if (
        metadata.get("token_source") != "authed_user"
        or str(authed_user.get("id") or "").strip() != link.source_author_id
        or "chat:write" not in scopes
        or (
            connection.token_expires_at is not None
            and connection.token_expires_at <= timezone.now()
        )
    ):
        raise _reauthorization_required()
    return connection


def _reauthorization_required() -> SlackDeletionError:
    return SlackDeletionError(
        "slack_reauthorization_required",
        http_status=409,
        detail="Reconnect Slack to grant permission to delete your own messages.",
    )


def _map_slack_error(error_code: str) -> SlackDeletionError:
    if error_code in {"cant_delete_message", "restricted_action"}:
        return SlackDeletionError(
            "slack_delete_not_allowed",
            http_status=403,
            detail="Slack does not allow this account to delete that message.",
        )
    if error_code in {
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
        "missing_scope",
    }:
        return _reauthorization_required()
    if error_code == "ratelimited":
        return SlackDeletionError(
            "slack_rate_limited",
            http_status=503,
            detail="Slack is temporarily rate limiting deletion requests. Try again shortly.",
        )
    return SlackDeletionError(
        "slack_delete_failed",
        http_status=502,
        detail="Slack could not delete this message.",
    )


def _complete_request(
    request_row: CommunityBridgeDeletionRequest,
    *,
    status: str,
    provider_response: dict | None = None,
) -> None:
    now = timezone.now()
    CommunityBridgeDeletionRequest.objects.filter(id=request_row.id).update(
        status=status,
        error_code="",
        provider_response=provider_response or {},
        completed_at=now,
        updated_at=now,
    )


def _fail_request(
    request_row: CommunityBridgeDeletionRequest,
    error_code: str,
    provider_response: dict | None = None,
) -> None:
    now = timezone.now()
    CommunityBridgeDeletionRequest.objects.filter(id=request_row.id).update(
        status=CommunityBridgeDeletionRequestStatus.FAILED,
        error_code=str(error_code or "")[:100],
        provider_response=provider_response or {},
        completed_at=now,
        updated_at=now,
    )
