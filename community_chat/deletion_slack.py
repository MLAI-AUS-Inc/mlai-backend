"""Remove an MLAI bot's reviewed Slack copy of a departing member's Chat post."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from integrations.models import CommunityBridgeMessageLink, CommunityBridgePlatform
from integrations.services.community_bridge.slack import SlackBridgeClient

from .models import AccountDeletionRequest, CommunityChatDevice


class SlackCopyErasureError(ValueError):
    """A reviewed link could not be matched or verified."""


@transaction.atomic
def erase_slack_copy(*, request_id, link_id, apply=False):
    """Act on an exact mapping within one confirmed deletion case."""
    user_id = AccountDeletionRequest.objects.filter(pk=request_id).values_list("user_id", flat=True).first()
    if user_id is None:
        raise SlackCopyErasureError("An open deletion case with an account is required.")
    get_user_model().objects.select_for_update().get(pk=user_id)
    request = AccountDeletionRequest.objects.select_for_update().get(pk=request_id)
    if request.user_id != user_id or request.status == AccountDeletionRequest.Status.COMPLETED:
        raise SlackCopyErasureError("An open deletion case with an account is required.")
    link = (
        CommunityBridgeMessageLink.objects.select_for_update(of=("self",))
        .select_related("channel")
        .filter(pk=link_id, source_platform=CommunityBridgePlatform.BUZZ,
                destination_platform=CommunityBridgePlatform.SLACK)
        .first()
    )
    workspace = getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
    if (link is None or not workspace or link.channel.slack_workspace_id != workspace
            or not link.destination_message_id or not link.source_message_id
            or link.destination_channel_id != link.channel.slack_channel_id
            or link.source_channel_id != link.channel.destination_channel_id):
        raise SlackCopyErasureError("The link is outside the configured Slack bridge scope.")
    if not CommunityChatDevice.objects.filter(
        user_id=user_id, public_key=link.source_author_id, verified_at__lte=link.created_at,
    ).exclude(revoked_at__lt=link.created_at).exists():
        raise SlackCopyErasureError("The link's signing key did not belong to this member when mapped.")
    if CommunityChatDevice.objects.filter(
        public_key=link.source_author_id, verified_at__lte=link.created_at,
    ).exclude(user_id=user_id).exclude(revoked_at__lt=link.created_at).exists():
        raise SlackCopyErasureError("The signing key's historical ownership is ambiguous.")
    if link.destination_deleted_at and not link.source_payload and not link.destination_payload:
        return "already_erased"
    if not apply:
        return "ready"
    if not SlackBridgeClient.is_configured():
        raise SlackCopyErasureError("The Slack bridge bot credential is unavailable.")
    client = SlackBridgeClient.get_client()
    identity = client.auth_test()
    if identity.get("team_id") != workspace:
        raise SlackCopyErasureError("The bot credential belongs to a different workspace.")
    try:
        response = client.chat_delete(channel=link.destination_channel_id, ts=link.destination_message_id)
    except SlackApiError as error:
        if error.response.get("error") != "message_not_found":
            raise SlackCopyErasureError("Slack could not verify deletion of this copy.") from error
    else:
        if response.get("ok") is not True:
            raise SlackCopyErasureError("Slack could not verify deletion of this copy.")
    link.destination_deleted_at = link.destination_deleted_at or timezone.now()
    link.source_payload = {}
    link.destination_payload = {}
    link.save(update_fields=("destination_deleted_at", "source_payload", "destination_payload", "updated_at"))
    return "erased"
