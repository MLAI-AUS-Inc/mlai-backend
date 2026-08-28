"""Consent-gated Slack direct-message migration and live mirroring.

DM content is deliberately kept out of the public community-bridge receipt,
message-link, analytics, and organization-memory tables. Queue bodies use the
same encrypted-at-rest field as OAuth credentials and are erased on completion.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from slack_sdk import WebClient

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
from integrations.services.community_bridge.identity import verified_identity_for_buzz


logger = logging.getLogger(__name__)
REQUIRED_SCOPES = {"im:read", "im:history", "im:write", "chat:write", "users:read"}
_last_registration_refresh = 0.0


class SlackDmMirrorError(RuntimeError):
    """Raised when a Slack DM grant cannot be activated safely."""


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


def status_payload(user) -> dict[str, Any]:
    connection = slack_connection_for_user(user)
    grant = (
        SlackDmMirrorGrant.objects.filter(user=user)
        .select_related("connection")
        .order_by("-updated_at")
        .first()
    )
    conversations = SlackDmMirrorConversation.objects.none()
    if grant is not None:
        conversations = SlackDmMirrorConversation.objects.filter(
            id__in=_conversation_ids_for_grant(grant)
        )
    counts = {
        key: conversations.filter(status=value).count()
        for key, value in {
            "live": SlackDmMirrorConversationStatus.LIVE,
            "waiting": SlackDmMirrorConversationStatus.AWAITING_CONSENT,
            "error": SlackDmMirrorConversationStatus.ERROR,
        }.items()
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
        "consent_version": grant.consent_version if grant else SlackDmMirrorGrant.CONSENT_VERSION,
        "last_discovery_at": grant.last_discovery_at if grant else None,
        "last_synced_at": grant.last_synced_at if grant else None,
        "last_error": grant.last_error if grant else "",
        "conversations": counts,
        "privacy": {
            "requires_both_participants": True,
            "included_in_roo": False,
            "included_in_analytics": False,
            "history_is_bounded": True,
        },
    }


def activate_connection(connection: ExternalServiceConnection) -> SlackDmMirrorGrant:
    """Record consent, bind the Slack identity, discover IMs, and provision eligible DMs."""

    if connection.provider != ExternalServiceProvider.SLACK:
        raise SlackDmMirrorError("Connection is not a Slack connection.")
    workspace_id, slack_user_id = _connection_identity(connection)
    missing = REQUIRED_SCOPES - set(connection.scopes or [])
    if missing:
        raise SlackDmMirrorError(f"Slack grant is missing scopes: {', '.join(sorted(missing))}.")
    device = _preferred_device(connection.user_id)
    if device is None:
        raise SlackDmMirrorError("Verify an MLAI Chat device before linking Slack DMs.")

    now = timezone.now()
    grant, _ = SlackDmMirrorGrant.objects.update_or_create(
        slack_workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        defaults={
            "user": connection.user,
            "connection": connection,
            "status": SlackDmMirrorGrantStatus.ACTIVE,
            "consent_version": SlackDmMirrorGrant.CONSENT_VERSION,
            "history_days": _history_days(),
            "consented_at": now,
            "paused_at": None,
            "revoked_at": None,
            "last_error": "",
        },
    )
    display_name = connection.user.full_name or connection.user.email or slack_user_id
    CommunityBridgeIdentityLink.objects.update_or_create(
        slack_workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        defaults={
            "user": connection.user,
            "buzz_pubkey": device.public_key,
            "display_name": display_name,
            "verification_method": CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE,
            "verification_reference": f"slack-oauth:{connection.pk}",
            "verified_at": now,
            "revoked_at": None,
            "revocation_reason": "",
        },
    )
    discover_conversations(grant)
    return grant


def pause_grant(grant: SlackDmMirrorGrant) -> None:
    now = timezone.now()
    grant.status = SlackDmMirrorGrantStatus.PAUSED
    grant.paused_at = now
    grant.save(update_fields=("status", "paused_at", "updated_at"))
    SlackDmMirrorConversation.objects.filter(
        id__in=_conversation_ids_for_grant(grant),
        status=SlackDmMirrorConversationStatus.LIVE,
    ).update(status=SlackDmMirrorConversationStatus.PAUSED, updated_at=now)


def resume_grant(grant: SlackDmMirrorGrant) -> None:
    grant.status = SlackDmMirrorGrantStatus.ACTIVE
    grant.paused_at = None
    grant.revoked_at = None
    grant.last_error = ""
    grant.save(
        update_fields=("status", "paused_at", "revoked_at", "last_error", "updated_at")
    )
    discover_conversations(grant)


def revoke_grant(grant: SlackDmMirrorGrant) -> None:
    connection = grant.connection
    try:
        WebClient(token=connection.access_token).auth_revoke()
    except Exception as exc:
        # Local revocation is the privacy boundary. Slack may already have
        # revoked the token, so a remote error must not retain local access.
        logger.warning(
            "slack_dm_mirror_remote_revoke_failed connection_id=%s error=%s",
            connection.pk,
            exc.__class__.__name__,
        )
    now = timezone.now()
    grant.status = SlackDmMirrorGrantStatus.REVOKED
    grant.revoked_at = now
    grant.save(update_fields=("status", "revoked_at", "updated_at"))
    CommunityBridgeIdentityLink.objects.filter(
        slack_workspace_id=grant.slack_workspace_id,
        slack_user_id=grant.slack_user_id,
        revoked_at__isnull=True,
    ).update(revoked_at=now, revocation_reason="Slack DM mirroring disconnected")
    conversation_ids = _conversation_ids_for_grant(grant)
    SlackDmMirrorConversation.objects.filter(id__in=conversation_ids).update(
        status=SlackDmMirrorConversationStatus.PAUSED,
        updated_at=now,
    )
    SlackDmMirrorDelivery.objects.filter(
        conversation_id__in=conversation_ids,
        status__in=(CommunityBridgeDeliveryStatus.PENDING, CommunityBridgeDeliveryStatus.PROCESSING),
    ).update(
        status=CommunityBridgeDeliveryStatus.DEAD,
        encrypted_text="",
        last_error="Consent revoked",
        updated_at=now,
    )
    connection.status = ExternalServiceConnectionStatus.DISCONNECTED
    connection.access_token = ""
    connection.refresh_token = ""
    connection.last_error = ""
    connection.save(
        update_fields=(
            "status",
            "access_token",
            "refresh_token",
            "last_error",
            "updated_at",
        )
    )


def discover_conversations(grant: SlackDmMirrorGrant) -> int:
    """Discover 1:1 Slack IMs. Group DMs remain excluded in v1."""

    client = WebClient(token=grant.connection.access_token)
    cursor = ""
    discovered = 0
    while True:
        response = client.conversations_list(types="im", exclude_archived=True, limit=200, cursor=cursor)
        for raw in response.get("channels") or []:
            if not isinstance(raw, dict):
                continue
            channel_id = str(raw.get("id") or "").strip()
            other_user_id = str(raw.get("user") or "").strip()
            if not channel_id.startswith("D") or not other_user_id:
                continue
            participant_ids = sorted({grant.slack_user_id, other_user_id})
            conversation, _ = SlackDmMirrorConversation.objects.update_or_create(
                slack_workspace_id=grant.slack_workspace_id,
                slack_conversation_id=channel_id,
                defaults={
                    "participant_slack_ids": participant_ids,
                },
            )
            _refresh_conversation_consent(conversation)
            discovered += 1
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break
    grant.last_discovery_at = timezone.now()
    grant.last_error = ""
    grant.save(update_fields=("last_discovery_at", "last_error", "updated_at"))
    return discovered


def ingest_slack_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    channel_id = str(event.get("channel") or "").strip()
    if not channel_id.startswith("D"):
        return None
    conversation = SlackDmMirrorConversation.objects.filter(
        slack_workspace_id=str(payload.get("team_id") or "").strip(),
        slack_conversation_id=channel_id,
        status=SlackDmMirrorConversationStatus.LIVE,
    ).first()
    if conversation is None or event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted"}:
        return {"status": "ignored"}
    message_id = str(event.get("ts") or "").strip()
    author_id = str(event.get("user") or "").strip()
    if not message_id or author_id not in set(conversation.participant_slack_ids or []):
        return {"status": "ignored"}
    if SlackDmMirrorDelivery.objects.filter(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
        metadata__slack_ts=message_id,
    ).exists():
        return {"status": "echo_ignored"}
    delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=message_id,
        operation=CommunityBridgeDeliveryType.CREATE,
        defaults={
            "source_author_id": author_id,
            "encrypted_text": str(event.get("text") or ""),
            "metadata": {"thread_ts": str(event.get("thread_ts") or "")},
            "available_at": timezone.now(),
        },
    )
    return {"status": "enqueued" if created else "duplicate", "delivery_id": str(delivery.pk)}


def ingest_mlai_dm_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Mirror one MLAI DM create to Slack using the human sender's grant."""

    channel_id = str(payload.get("source_channel_id") or "").strip()
    conversation = SlackDmMirrorConversation.objects.filter(
        mlai_channel_id=channel_id,
        status=SlackDmMirrorConversationStatus.LIVE,
    ).first()
    if conversation is None:
        return None
    normalized = payload.get("normalized_event")
    if not isinstance(normalized, dict) or normalized.get("delivery_type") != "create":
        return {"status": "ignored"}
    message_id = str(normalized.get("source_message_id") or "").strip().lower()
    author_pubkey = str(normalized.get("source_author_id") or "").strip().lower()
    if not message_id or author_pubkey not in set(conversation.participant_buzz_pubkeys or []):
        return {"status": "ignored"}
    identity = verified_identity_for_buzz(
        slack_workspace_id=conversation.slack_workspace_id,
        buzz_pubkey=author_pubkey,
    )
    if identity is None or identity["slack_user_id"] not in set(conversation.participant_slack_ids or []):
        return {"status": "ignored"}
    grant = SlackDmMirrorGrant.objects.select_related("connection").filter(
        slack_workspace_id=conversation.slack_workspace_id,
        slack_user_id=identity["slack_user_id"],
        status=SlackDmMirrorGrantStatus.ACTIVE,
        revoked_at__isnull=True,
    ).first()
    if grant is None:
        return {"status": "ignored"}
    delivery, created = SlackDmMirrorDelivery.objects.get_or_create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        source_message_id=message_id,
        operation=CommunityBridgeDeliveryType.CREATE,
        defaults={
            "source_author_id": author_pubkey,
            "encrypted_text": str(normalized.get("text") or ""),
            "available_at": timezone.now(),
        },
    )
    if not created and delivery.status == CommunityBridgeDeliveryStatus.COMPLETED:
        return {"status": "duplicate"}
    response = WebClient(token=grant.connection.access_token).chat_postMessage(
        channel=conversation.slack_conversation_id,
        text=delivery.encrypted_text,
        unfurl_links=True,
        unfurl_media=True,
    )
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.metadata = {"slack_ts": str(response.get("ts") or "")}
    delivery.completed_at = timezone.now()
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
    grant.last_synced_at = timezone.now()
    grant.save(update_fields=("last_synced_at", "updated_at"))
    return {"status": "mirrored", "delivery_id": str(delivery.pk)}


def process_ready_deliveries(limit: int = 20) -> int:
    _refresh_adapter_registrations_if_due()
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
                .select_related("conversation")
                .filter(
                    status=CommunityBridgeDeliveryStatus.PENDING,
                    available_at__lte=timezone.now(),
                )
                .order_by("available_at", "id")
                .first()
            )
            if delivery is None:
                break
            delivery.status = CommunityBridgeDeliveryStatus.PROCESSING
            delivery.save(update_fields=("status", "updated_at"))
        try:
            _deliver_to_mlai(delivery)
        except Exception as exc:  # worker boundary; retry with bounded backoff
            delivery.attempts += 1
            delivery.status = (
                CommunityBridgeDeliveryStatus.DEAD
                if delivery.attempts >= 5
                else CommunityBridgeDeliveryStatus.PENDING
            )
            delivery.available_at = timezone.now() + timedelta(seconds=min(60, 2**delivery.attempts))
            delivery.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            delivery.save(
                update_fields=("attempts", "status", "available_at", "last_error", "updated_at")
            )
            logger.exception("slack_dm_mirror_delivery_failed delivery_id=%s", delivery.pk)
            continue
        processed += 1
    return processed


def _refresh_adapter_registrations_if_due() -> None:
    global _last_registration_refresh
    now = time.monotonic()
    if now - _last_registration_refresh < 60:
        return
    _last_registration_refresh = now
    for conversation in SlackDmMirrorConversation.objects.filter(
        status=SlackDmMirrorConversationStatus.LIVE,
        mlai_channel_id__isnull=False,
    ).only("id", "mlai_channel_id", "participant_buzz_pubkeys", "status"):
        try:
            result = BuzzBridgeClient.provision_private_conversation(
                list(conversation.participant_buzz_pubkeys or [])
            )
            if str(result["channel_id"]) != str(conversation.mlai_channel_id):
                raise SlackDmMirrorError("Participant set resolved to a different MLAI DM.")
        except Exception as exc:
            logger.warning(
                "slack_dm_mirror_registration_refresh_failed conversation_id=%s error=%s",
                conversation.pk,
                exc,
            )


def _refresh_conversation_consent(conversation: SlackDmMirrorConversation) -> None:
    participant_ids = sorted(set(conversation.participant_slack_ids or []))
    grants = list(
        SlackDmMirrorGrant.objects.filter(
            slack_workspace_id=conversation.slack_workspace_id,
            slack_user_id__in=participant_ids,
            status=SlackDmMirrorGrantStatus.ACTIVE,
            revoked_at__isnull=True,
        )
    )
    identities = list(
        CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=conversation.slack_workspace_id,
            slack_user_id__in=participant_ids,
            revoked_at__isnull=True,
        )
    )
    if len(grants) != len(participant_ids) or len(identities) != len(participant_ids):
        conversation.status = SlackDmMirrorConversationStatus.AWAITING_CONSENT
        conversation.save(update_fields=("status", "updated_at"))
        return
    pubkeys = sorted(identity.buzz_pubkey for identity in identities)
    participant_hash = hashlib.sha256(b"".join(bytes.fromhex(value) for value in pubkeys)).hexdigest()
    conversation.participant_buzz_pubkeys = pubkeys
    conversation.participant_hash = participant_hash
    conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
    conversation.last_error = ""
    conversation.save(
        update_fields=(
            "participant_buzz_pubkeys",
            "participant_hash",
            "status",
            "last_error",
            "updated_at",
        )
    )
    provisioned = BuzzBridgeClient.provision_private_conversation(pubkeys)
    conversation.mlai_channel_id = provisioned["channel_id"]
    conversation.status = SlackDmMirrorConversationStatus.LIVE
    conversation.save(update_fields=("mlai_channel_id", "status", "updated_at"))
    _enqueue_history(conversation, grants[0])


def _enqueue_history(conversation: SlackDmMirrorConversation, grant: SlackDmMirrorGrant) -> None:
    oldest = max(0, int(time.time()) - grant.history_days * 86_400)
    client = WebClient(token=grant.connection.access_token)
    cursor = ""
    history: list[dict[str, Any]] = []
    while True:
        response = client.conversations_history(
            channel=conversation.slack_conversation_id,
            oldest=str(oldest),
            inclusive=True,
            limit=200,
            cursor=cursor,
        )
        for message in response.get("messages") or []:
            if not isinstance(message, dict) or message.get("bot_id"):
                continue
            message_id = str(message.get("ts") or "").strip()
            author_id = str(message.get("user") or "").strip()
            if not message_id or author_id not in set(conversation.participant_slack_ids or []):
                continue
            history.append(message)
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break

    history.sort(key=lambda message: _slack_ts_sort_key(str(message.get("ts") or "")))
    for message in history:
        message_id = str(message.get("ts") or "").strip()
        author_id = str(message.get("user") or "").strip()
        SlackDmMirrorDelivery.objects.get_or_create(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=message_id,
            operation=CommunityBridgeDeliveryType.CREATE,
            defaults={
                "source_author_id": author_id,
                "encrypted_text": str(message.get("text") or ""),
                "metadata": {
                    "backfill": True,
                    "thread_ts": str(message.get("thread_ts") or ""),
                },
                "available_at": timezone.now(),
            },
        )
    if history:
        conversation.oldest_synced_ts = str(history[0].get("ts") or "")
        conversation.latest_synced_ts = str(history[-1].get("ts") or "")
        conversation.save(update_fields=("oldest_synced_ts", "latest_synced_ts", "updated_at"))


def _deliver_to_mlai(delivery: SlackDmMirrorDelivery) -> None:
    conversation = delivery.conversation
    if conversation.status != SlackDmMirrorConversationStatus.LIVE or not conversation.mlai_channel_id:
        raise SlackDmMirrorError("Conversation is not live.")
    identity = CommunityBridgeIdentityLink.objects.get(
        slack_workspace_id=conversation.slack_workspace_id,
        slack_user_id=delivery.source_author_id,
        revoked_at__isnull=True,
    )
    BuzzBridgeClient.deliver_private(
        delivery_id=str(delivery.pk),
        created_at=_slack_ts_sort_key(delivery.source_message_id)[0],
        operation=delivery.operation,
        channel_id=str(conversation.mlai_channel_id),
        participant_pubkeys=list(conversation.participant_buzz_pubkeys or []),
        text=delivery.encrypted_text,
        source_workspace_id=conversation.slack_workspace_id,
        source_channel_id=conversation.slack_conversation_id,
        source_message_id=delivery.source_message_id,
        source_author_id=delivery.source_author_id,
        linked_pubkey=identity.buzz_pubkey,
    )
    now = timezone.now()
    delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
    delivery.encrypted_text = ""
    delivery.metadata = {}
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
    conversation.latest_synced_ts = delivery.source_message_id
    conversation.save(update_fields=("last_synced_at", "latest_synced_ts", "updated_at"))


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
    authed_user = metadata.get("authed_user") if isinstance(metadata.get("authed_user"), dict) else {}
    workspace_id = str(team.get("id") or connection.external_account_id or "").strip()
    slack_user_id = str(authed_user.get("id") or "").strip()
    if not workspace_id or not slack_user_id:
        raise SlackDmMirrorError("Slack OAuth response did not identify the workspace and user.")
    return workspace_id, slack_user_id


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


def _conversation_ids_for_grant(grant: SlackDmMirrorGrant) -> list[int]:
    """Return a member's conversations without database-specific JSON operators."""

    rows = SlackDmMirrorConversation.objects.filter(
        slack_workspace_id=grant.slack_workspace_id
    ).values_list("id", "participant_slack_ids")
    return [
        conversation_id
        for conversation_id, participants in rows
        if grant.slack_user_id in set(participants or [])
    ]
