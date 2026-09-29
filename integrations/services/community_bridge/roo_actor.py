"""Resolve Public Roo's actor from a completed, authenticated Chat delivery."""

import re

from django.conf import settings
from django.contrib.auth import get_user_model

from community_chat.privacy import has_ai_consent
from integrations.models import CommunityBridgeMessageLink, CommunityBridgePlatform
from integrations.services.slack_channel_mentions import render_slack_mentions
from integrations.services.slack_roo import public_roo_target
from .identity import verified_identity_for_buzz


class BridgeActorError(ValueError):
    """A missing or unauthorized bridge actor, with an HTTP retry contract."""

    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


def resolve_roo_actor(*, workspace_id, channel_id, message_id, thread_ts, bridge_user_id):
    """Resolve an exact Slack message; caller text and display names confer no authority.

    Only public bridge records are supported here. Private mirrors have their own
    owner/consent boundary and must not acquire public-channel authority.
    """
    fields = {
        "workspace_id": (workspace_id, r"T[A-Z0-9]+"),
        "channel_id": (channel_id, r"C[A-Z0-9]+"),
        "message_id": (message_id, r"[0-9]{10,}\.[0-9]+"),
        "thread_ts": (thread_ts, r"[0-9]{10,}\.[0-9]+"),
        "bridge_user_id": (bridge_user_id, r"[UW][A-Z0-9]+"),
    }
    if any(not isinstance(value, str) or not re.fullmatch(pattern, value)
           for value, pattern in fields.values()):
        raise BridgeActorError("invalid_bridge_context", 400)
    expected_bot = str(getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") or "")
    target = public_roo_target()
    if not expected_bot or bridge_user_id != expected_bot or not target or target[0] != workspace_id:
        raise BridgeActorError("unknown_bridge_sender", 404)

    link = CommunityBridgeMessageLink.objects.select_related("channel").filter(
        source_platform=CommunityBridgePlatform.BUZZ,
        destination_platform=CommunityBridgePlatform.SLACK,
        destination_channel_id=channel_id,
        destination_message_id=message_id,
        channel__slack_workspace_id=workspace_id,
        channel__slack_channel_id=channel_id,
        channel__destination_platform=CommunityBridgePlatform.BUZZ,
    ).first()
    # Slack may deliver app_mention before the posting worker commits its link.
    # Never acknowledge that race as a permanently handled mention.
    if link is None:
        raise BridgeActorError("bridge_delivery_pending", 409)
    if (not link.channel.enabled or link.source_deleted_at or link.destination_deleted_at
            or link.source_channel_id != link.channel.destination_channel_id
            or thread_ts != (link.destination_parent_message_id or link.destination_message_id)
            or not re.fullmatch(r"[0-9a-f]{64}", link.source_message_id)):
        raise BridgeActorError("bridge_context_revoked", 403)

    identity = verified_identity_for_buzz(
        slack_workspace_id=workspace_id, buzz_pubkey=link.source_author_id,
    )
    if (not identity or identity.get("identity_source") != "mlai_account"
            or not identity.get("user_profile_id")
            or identity.get("slack_workspace_id") != workspace_id
            or not re.fullmatch(r"[UW][A-Z0-9]+", identity.get("slack_user_id") or "")
            or identity["slack_user_id"] == expected_bot):
        raise BridgeActorError("bridge_actor_unverified", 403)
    if getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True):
        user = get_user_model().objects.filter(
            community_chat_profile_id=identity["user_profile_id"], is_active=True,
        ).first()
        if user is None or not has_ai_consent(user.pk):
            raise BridgeActorError("bridge_actor_consent_required", 403)

    payload = link.source_payload or {}
    try:
        text, mentions = render_slack_mentions(
            str(payload.get("text") or ""),
            (payload.get("metadata") or {}).get("slack_mention_tags") or [],
        )
    except ValueError as exc:
        raise BridgeActorError("roo_not_explicitly_mentioned", 403) from exc
    if target[1] not in mentions:
        raise BridgeActorError("roo_not_explicitly_mentioned", 403)
    return {
        **{key: value for key, (value, _) in fields.items()},
        "user_id": identity["slack_user_id"],
        "source_event_id": link.source_message_id,
        "text": text,
    }
