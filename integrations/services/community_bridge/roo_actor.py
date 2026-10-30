"""Resolve Public Roo's actor from a completed, authenticated Chat delivery."""

import re

from django.conf import settings
from django.contrib.auth import get_user_model

from community_chat.privacy import has_ai_consent
from integrations.models import (
    CommunityBridgeDelivery, CommunityBridgeIdentityLink, CommunityBridgeMessageLink,
    CommunityBridgePlatform, SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.slack_chat_catalog import public_channels_enabled
from integrations.services.slack_channel_mentions import render_slack_mentions
from integrations.services.slack_roo import public_roo_target
from .identity import community_account_for_key, verified_identity_for_buzz
from .native_mentions import native_mention_tags, native_recipient_targets


def _acknowledged_create(link, poster):
    """Require the exact immutable provider acknowledgement behind a new link."""
    from .member_attempts import ATTEMPT_KEY, command_hash, response_from_receipt
    delivery = CommunityBridgeDelivery.objects.filter(
        channel_id=link.channel_id, source_platform="buzz", target_platform="slack",
        source_channel_id=link.source_channel_id, source_message_id=link.source_message_id,
        target_channel_id=link.destination_channel_id, delivery_type="create",
    ).first()
    if delivery is None or delivery.status in {"pending", "processing", "waiting_parent"}:
        raise BridgeActorError("bridge_delivery_pending", 409)
    payload = delivery.payload if isinstance(delivery.payload, dict) else {}
    command, attempt = payload.get("_community_delivery_v1"), payload.get(ATTEMPT_KEY)
    wire = command.get("wire") if isinstance(command, dict) else None
    fact = attempt.get("receipt") if isinstance(attempt, dict) else None
    if (delivery.status != "completed" or not delivery.completed_at
            or payload.get("source_author_id") != link.source_author_id
            or not isinstance(command, dict) or command.get("actor") != poster
            or not isinstance(wire, dict) or not isinstance(wire.get("kwargs"), dict)
            or wire["kwargs"].get("channel") != link.destination_channel_id
            or wire["kwargs"].get("thread_ts", "") != link.destination_parent_message_id
            or wire.get("parent") != link.destination_parent_message_id
            or wire.get("text_hash") != (link.destination_payload or {}).get("text_hash")
            or not wire["kwargs"].get("client_msg_id")
            or not isinstance(attempt, dict) or attempt.get("status") != "acknowledged"
            or attempt.get("command_hash") != command_hash(command)
            or not isinstance(fact, dict) or fact.get("ts") != link.destination_message_id):
        raise BridgeActorError("bridge_actor_unverified", 403)
    if response_from_receipt(fact, poster, wire, "create") is None:
        raise BridgeActorError("bridge_actor_unverified", 403)


def _verified_member_poster(link, poster, account, bridge_user_id):
    """Revalidate the acknowledged original account/grant without provider I/O."""
    from .member_delivery import _binding, _poster_owner

    if (_poster_owner(poster) is None or poster.get("user_id") != account["user_id"]
            or poster.get("public_key") != link.source_author_id
            or poster.get("workspace_id") != link.channel.slack_workspace_id
            or poster.get("slack_user_id") != bridge_user_id):
        raise BridgeActorError("bridge_actor_unverified", 403)
    _acknowledged_create(link, poster)
    grant = SlackDmMirrorGrant.objects.select_related("user", "connection").filter(
        pk=poster["grant_id"], user_id=account["user_id"], connection_id=poster["connection_id"],
        slack_workspace_id=poster["workspace_id"], slack_user_id=bridge_user_id,
        status="active", revoked_at__isnull=True,
    ).first()
    try:
        authority = dm._capture_slack_grant_api_authority(grant, refresh_token=False) if grant else None
        valid = bool(grant and access.enabled()
                     and _binding(authority, link.source_author_id) == poster
                     and public_channels_enabled(grant, authority=authority)
                     and "chat:write" in authority.scopes
                     and access._membership_state(grant, access._membership(grant, link.destination_channel_id)) == "joined")
    except (AttributeError, TypeError, ValueError, dm.SlackDmMirrorError):
        valid = False
    if not valid:
        raise BridgeActorError("bridge_actor_unverified", 403)


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
    if not expected_bot or not target or target[0] != workspace_id:
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
        if bridge_user_id != expected_bot and not CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=workspace_id, slack_user_id=bridge_user_id,
            user__is_active=True, revoked_at__isnull=True,
        ).exists():
            raise BridgeActorError("unknown_bridge_sender", 404)
        raise BridgeActorError("bridge_delivery_pending", 409)
    if (not link.channel.enabled or link.channel.audience != "community"
            or link.channel.slack_visibility != "public" or link.channel.slack_archived
            or link.channel.slack_deleted or link.channel.hold_reason
            or link.channel.mirror_decision != "mirror"
            or link.source_deleted_at or link.destination_deleted_at
            or link.source_channel_id != link.channel.destination_channel_id
            or thread_ts != (link.destination_parent_message_id or link.destination_message_id)
            or not re.fullmatch(r"[0-9a-f]{64}", link.source_message_id)):
        raise BridgeActorError("bridge_context_revoked", 403)

    account = community_account_for_key(slack_workspace_id=workspace_id, buzz_pubkey=link.source_author_id)
    if account is None:
        raise BridgeActorError("bridge_actor_unverified", 403)
    destination = link.destination_payload if isinstance(link.destination_payload, dict) else {}
    poster = destination.get("poster")
    if isinstance(poster, dict) and poster.get("kind") == "member":
        _verified_member_poster(link, poster, account, bridge_user_id)
        posting_mode = "member"
    elif (bridge_user_id == expected_bot and (poster is None or poster == {"kind": "bot"}
            or isinstance(poster, dict) and poster.get("kind") == "bot"
            and poster.get("user_id") == account["user_id"]
            and poster.get("public_key") == link.source_author_id
            and poster.get("workspace_id") == workspace_id
            and poster.get("slack_user_id") == expected_bot)):
        if isinstance(poster, dict) and poster.get("user_id") is not None:
            _acknowledged_create(link, poster)
        posting_mode = "bot"
    else:
        raise BridgeActorError("unknown_bridge_sender", 404)
    identity = verified_identity_for_buzz(
        slack_workspace_id=workspace_id, buzz_pubkey=link.source_author_id,
    )
    if (not identity or identity.get("identity_source") != "mlai_account"
            or not identity.get("user_profile_id")
            or identity.get("user_profile_id") != account["user_profile_id"]
            or identity.get("slack_workspace_id") != workspace_id
            or not re.fullmatch(r"[UW][A-Z0-9]+", identity.get("slack_user_id") or "")
            or posting_mode == "member" and identity.get("slack_user_id") != bridge_user_id
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
    try:
        native_tags = native_mention_tags(
            str(payload.get("text") or ""),
            (payload.get("metadata") or {}).get("mlai_mention_tags") or [],
        )
        recipients = native_recipient_targets(
            workspace_id=workspace_id, text=str(payload.get("text") or ""),
            tags=native_tags,
            bindings=(payload.get("metadata") or {}).get("mlai_mention_bindings"),
        )
    except ValueError as exc:
        raise BridgeActorError("invalid_native_mentions", 403) from exc
    return {
        **{key: value for key, (value, _) in fields.items()},
        "version": 2,
        "posting_mode": posting_mode,
        "actor_profile_id": account["user_profile_id"],
        "recipient_targets": recipients,
        "source_labels": list(dict.fromkeys(tag[2] for tag in native_tags)),
        "user_id": identity["slack_user_id"],
        "source_event_id": link.source_message_id,
        "text": text,
    }
