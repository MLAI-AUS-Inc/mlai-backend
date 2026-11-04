"""Owner-scoped verification of acknowledged Chat messages sent to Public Roo."""

import hashlib
import re

from django.conf import settings
from django.db.models import Q

from community_chat.privacy import has_ai_consent
from integrations.models import SlackDmMirrorConversation, SlackDmMirrorDelivery
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_roo import public_roo_target
from .identity import community_account_for_key
from .roo_actor import BridgeActorError


def roo_dm_receipt(delivery, acknowledged_text):
    """Retain a digest and authority generation, never the private message body."""
    conversation = delivery.conversation
    grant = conversation.grant
    target = public_roo_target()
    if (not target or target[0] != conversation.slack_workspace_id
            or not conversation.slack_conversation_id.startswith("D")
            or set(conversation.participant_slack_ids or []) != {grant.slack_user_id, target[1]}):
        return {}
    return {"roo_dm_v1": {
        "text_sha256": hashlib.sha256(acknowledged_text.encode("utf-8")).hexdigest(),
        "connection_id": grant.connection_id,
        "oauth_generation": dm.connection_slack_oauth_generation(grant.connection),
        "consent_generation": dm._grant_consent_generation(grant),
        "mlai_channel_id": str(conversation.mlai_channel_id),
    }}


def resolve_roo_dm_actor(*, workspace_id, channel_id, message_id, thread_ts, bridge_user_id):
    """Verify a private delivery independently of public bridge permissions.

    The signed Slack event supplies the text. Roo must compare its digest with
    this provider acknowledgement before dispatch. Reads perform no provider I/O.
    """
    context = dict(workspace_id=workspace_id, channel_id=channel_id, message_id=message_id,
                   thread_ts=thread_ts, bridge_user_id=bridge_user_id)
    patterns = dict(workspace_id=r"T[A-Z0-9]+", channel_id=r"D[A-Z0-9]+",
                    message_id=r"[0-9]{10,}\.[0-9]+", thread_ts=r"[0-9]{10,}\.[0-9]+",
                    bridge_user_id=r"[UW][A-Z0-9]+")
    if any(not isinstance(value, str) or not re.fullmatch(patterns[key], value)
           for key, value in context.items()):
        raise BridgeActorError("invalid_bridge_context", 400)
    target = public_roo_target()
    if not target or target[0] != workspace_id or bridge_user_id == target[1]:
        raise BridgeActorError("unknown_bridge_sender", 404)
    conversation = SlackDmMirrorConversation.objects.select_related("grant__user", "grant__connection").filter(
        slack_workspace_id=workspace_id, slack_conversation_id=channel_id,
        grant__slack_workspace_id=workspace_id, grant__slack_user_id=bridge_user_id,
    ).first()
    if conversation is None:
        raise BridgeActorError("unknown_bridge_sender", 404)
    grant = conversation.grant
    if (conversation.status != "live" or not conversation.mlai_channel_id
            or grant.status != "active" or grant.revoked_at is not None or not grant.user.is_active
            or set(conversation.participant_slack_ids or []) != {bridge_user_id, target[1]}):
        raise BridgeActorError("bridge_context_revoked", 403)
    try:
        dm._assert_grant_connection_authorized(grant)
        dm._assert_slack_conversation_writable(conversation)
    except dm.SlackDmMirrorError as exc:
        raise BridgeActorError("bridge_context_revoked", 403) from exc
    if getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True) and not has_ai_consent(grant.user_id):
        raise BridgeActorError("bridge_actor_consent_required", 403)
    rows = SlackDmMirrorDelivery.objects.filter(conversation=conversation, source_platform="buzz")
    delivery = rows.filter(operation="create", status="completed", metadata__slack_ts=message_id).first()
    if delivery is None:
        # Slack can deliver the callback before the posting transaction commits.
        if rows.filter(operation="create", status__in=("pending", "processing")).exists():
            raise BridgeActorError("bridge_delivery_pending", 409)
        raise BridgeActorError("unknown_bridge_sender", 404)
    metadata = delivery.metadata or {}
    proof = metadata.get("roo_dm_v1")
    source_id = delivery.source_message_id
    if (not delivery.completed_at or not isinstance(source_id, str) or not re.fullmatch(r"[0-9a-f]{64}", source_id)
            or metadata.get("source_event_id") != source_id
            or metadata.get("participant_hash") != conversation.participant_hash
            or thread_ts != (metadata.get("thread_ts") or message_id)
            or not isinstance(proof, dict)
            or not isinstance(proof.get("text_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["text_sha256"])
            or proof.get("connection_id") != grant.connection_id
            or proof.get("oauth_generation") != dm.connection_slack_oauth_generation(grant.connection)
            or proof.get("consent_generation") != dm._grant_consent_generation(grant)
            or proof.get("mlai_channel_id") != str(conversation.mlai_channel_id)):
        raise BridgeActorError("bridge_actor_unverified", 403)
    if rows.filter(operation__in=("edit", "delete")).filter(
        Q(metadata__target_source_message_id=source_id) | Q(metadata__source_event_id=source_id)
    ).exists():
        raise BridgeActorError("bridge_context_revoked", 403)
    if (delivery.source_author_id not in dm._conversation_owner_device_pubkeys(conversation)
            or dm._active_verified_device(grant.user_id, delivery.source_author_id) is None):
        raise BridgeActorError("bridge_actor_unverified", 403)
    account = community_account_for_key(slack_workspace_id=workspace_id, buzz_pubkey=delivery.source_author_id)
    if not account or account["user_id"] != grant.user_id:
        raise BridgeActorError("bridge_actor_unverified", 403)
    return {**context, "version": 2, "posting_mode": "member", "user_id": bridge_user_id,
            "actor_profile_id": account["user_profile_id"], "source_event_id": source_id,
            "message_text_sha256": proof["text_sha256"], "recipient_targets": [], "source_labels": []}
