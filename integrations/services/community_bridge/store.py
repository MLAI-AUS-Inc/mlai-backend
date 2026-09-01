import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.formatting import (
    emoji_to_slack_reaction,
    normalize_slack_files,
    reaction_object_id,
    sanitize_slack_text,
    slack_reaction_to_emoji,
)
from integrations.services.community_bridge.contracts import (
    BridgeAttachment,
    CanonicalBridgeEvent,
)


logger = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = [10, 30, 120, 300, 900]
PARENT_DEPENDENCY_RETRY_SECONDS = 10

# Slack conversation types that may be mirrored: public channels ("channel")
# and private channels ("group"). Direct messages ("im") and group DMs
# ("mpim") are never bridgeable. A private channel still only mirrors once an
# operator has created its CommunityBridgeChannel mapping and pointed it at a
# private MLAI Chat channel — the mapping, not this set, is the access control.
BRIDGEABLE_SLACK_CHANNEL_TYPES = frozenset({"channel", "group"})


def ingest_slack_event(payload: dict) -> dict:
    event = dict(payload.get("event") or {})
    normalized = _normalize_slack_event(payload)
    normalized_payload = normalized or {}
    receipt_key = str(payload.get("event_id") or "").strip()
    source_channel_id = str(event.get("channel") or normalized_payload.get("source_channel_id") or "").strip()
    event_type = str(event.get("subtype") or event.get("type") or "").strip() or "message"
    return ingest_inbound_event(
        source_platform=CommunityBridgePlatform.SLACK,
        receipt_key=receipt_key,
        source_channel_id=source_channel_id,
        event_type=event_type,
        normalized_event=normalized_payload or None,
        raw_payload=payload,
    )


def ingest_discord_event(
    *,
    receipt_key: str,
    source_channel_id: str,
    event_type: str,
    normalized_event: Optional[dict],
    raw_payload: Optional[dict] = None,
) -> dict:
    return ingest_inbound_event(
        source_platform=CommunityBridgePlatform.DISCORD,
        receipt_key=receipt_key,
        source_channel_id=source_channel_id,
        event_type=event_type,
        normalized_event=normalized_event,
        raw_payload=raw_payload or {},
    )


def ingest_inbound_event(
    *,
    source_platform: str,
    receipt_key: str,
    source_channel_id: str,
    event_type: str,
    normalized_event: Optional[dict],
    raw_payload: dict,
) -> dict:
    normalized_receipt_key = str(receipt_key or "").strip()
    if not normalized_receipt_key:
        return {"status": "ignored", "reason": "missing_receipt_key"}

    normalized_channel_id = str(source_channel_id or "").strip()
    channel = _get_enabled_channel(source_platform=source_platform, channel_id=normalized_channel_id)

    if normalized_event:
        try:
            normalized_event = _canonicalize_event(
                receipt_key=normalized_receipt_key,
                source_platform=source_platform,
                source_channel_id=normalized_channel_id,
                normalized_event=normalized_event,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "community_bridge_invalid_canonical_event platform=%s receipt_key=%s error=%s",
                source_platform,
                normalized_receipt_key,
                exc,
            )
            normalized_event = None

    try:
        with transaction.atomic():
            receipt = CommunityBridgeReceipt.objects.create(
                channel=channel,
                platform=source_platform,
                receipt_key=normalized_receipt_key,
                event_type=str(event_type or "").strip(),
                source_channel_id=normalized_channel_id,
                source_message_id=str((normalized_event or {}).get("source_message_id") or "").strip(),
                source_parent_message_id=str((normalized_event or {}).get("source_parent_message_id") or "").strip(),
                status=CommunityBridgeReceiptStatus.ACCEPTED,
                payload=raw_payload or {},
            )
    except IntegrityError:
        existing = CommunityBridgeReceipt.objects.filter(
            platform=source_platform,
            receipt_key=normalized_receipt_key,
        ).first()
        return {
            "status": "duplicate",
            "receipt_id": existing.id if existing else None,
            "receipt_key": normalized_receipt_key,
        }

    if channel is None:
        return _mark_receipt_ignored(receipt, reason="unmapped_channel")

    if not normalized_event:
        return _mark_receipt_ignored(receipt, reason="unsupported_or_ignored_event")

    if (
        normalized_event["delivery_type"] == CommunityBridgeDeliveryType.EDIT
        and not channel.sync_edits
    ):
        return _mark_receipt_ignored(receipt, reason="edit_sync_disabled")
    if (
        normalized_event["delivery_type"] == CommunityBridgeDeliveryType.DELETE
        and not channel.sync_deletes
    ):
        return _mark_receipt_ignored(receipt, reason="delete_sync_disabled")
    if (
        normalized_event["delivery_type"]
        not in {
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }
        and normalized_event.get("source_parent_message_id")
        and not channel.sync_replies
    ):
        return _mark_receipt_ignored(receipt, reason="reply_sync_disabled")

    target_platform = _target_platform(channel=channel, source_platform=source_platform)
    if (
        normalized_event["delivery_type"]
        in {
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }
        and CommunityBridgePlatform.BUZZ
        not in {source_platform, target_platform}
    ):
        return _mark_receipt_ignored(receipt, reason="reaction_sync_unsupported")

    delivery = CommunityBridgeDelivery.objects.create(
        channel=channel,
        receipt=receipt,
        target_platform=target_platform,
        source_platform=source_platform,
        delivery_type=normalized_event["delivery_type"],
        status=CommunityBridgeDeliveryStatus.PENDING,
        source_event_key=normalized_receipt_key,
        source_channel_id=normalized_channel_id,
        source_message_id=str(normalized_event.get("source_message_id") or "").strip(),
        source_parent_message_id=str(normalized_event.get("source_parent_message_id") or "").strip(),
        target_channel_id=_channel_id_for_platform(channel=channel, platform=target_platform),
        payload=normalized_event,
        available_at=timezone.now(),
    )
    receipt.status = CommunityBridgeReceiptStatus.ENQUEUED
    receipt.queued_delivery_count = 1
    receipt.processed_at = timezone.now()
    receipt.save(update_fields=["status", "queued_delivery_count", "processed_at", "updated_at"])
    return {
        "status": "enqueued",
        "receipt_id": receipt.id,
        "delivery_id": delivery.id,
        "target_platform": target_platform,
    }


def claim_ready_deliveries(limit: int = 10) -> list[dict]:
    now = timezone.now()
    items = []
    with transaction.atomic():
        queryset = (
            CommunityBridgeDelivery.objects.select_related("channel")
            .filter(
                status__in=[
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.WAITING_FOR_PARENT,
                    CommunityBridgeDeliveryStatus.FAILED,
                ],
                available_at__lte=now,
            )
            .filter(attempts__lt=F("max_attempts"))
            .order_by("available_at", "id")
        )
        if connection.features.has_select_for_update_skip_locked:
            queryset = queryset.select_for_update(skip_locked=True)
        else:
            queryset = queryset.select_for_update()
        deliveries = list(queryset[: max(1, int(limit or 1))])
        for delivery in deliveries:
            delivery.status = CommunityBridgeDeliveryStatus.PROCESSING
            delivery.locked_at = now
            delivery.attempts = int(delivery.attempts or 0) + 1
            delivery.save(update_fields=["status", "locked_at", "attempts", "updated_at"])
            items.append(_serialize_delivery(delivery))
    return items


def resolve_message_link(
    *,
    source_platform: str,
    source_channel_id: str,
    source_message_id: str,
    destination_platform: str,
) -> Optional[dict]:
    link = CommunityBridgeMessageLink.objects.filter(
        source_platform=source_platform,
        source_channel_id=str(source_channel_id or "").strip(),
        source_message_id=str(source_message_id or "").strip(),
        destination_platform=destination_platform,
    ).first()
    if not link:
        return None
    return {
        "id": link.id,
        "destination_channel_id": link.destination_channel_id,
        "destination_message_id": link.destination_message_id,
        "destination_parent_message_id": link.destination_parent_message_id,
        "source_payload": link.source_payload or {},
        "destination_payload": link.destination_payload or {},
    }


def resolve_mapped_message(
    *,
    source_platform: str,
    source_channel_id: str,
    source_message_id: str,
    destination_platform: str,
) -> Optional[dict]:
    """Resolve a target whether the current object began here or was mirrored here."""

    direct = resolve_message_link(
        source_platform=source_platform,
        source_channel_id=source_channel_id,
        source_message_id=source_message_id,
        destination_platform=destination_platform,
    )
    if direct:
        return direct

    reverse = CommunityBridgeMessageLink.objects.filter(
        source_platform=destination_platform,
        destination_platform=source_platform,
        destination_channel_id=str(source_channel_id or "").strip(),
        destination_message_id=str(source_message_id or "").strip(),
    ).first()
    if not reverse:
        return None
    return {
        "id": reverse.id,
        "destination_channel_id": reverse.source_channel_id,
        "destination_message_id": reverse.source_message_id,
        "destination_parent_message_id": reverse.source_parent_message_id,
        "source_payload": reverse.destination_payload or {},
        "destination_payload": reverse.source_payload or {},
    }


def complete_create_delivery(
    *,
    delivery_id: int,
    destination_message_id: str,
    destination_channel_id: str,
    destination_parent_message_id: str = "",
    destination_payload: Optional[dict] = None,
) -> None:
    with transaction.atomic():
        delivery = CommunityBridgeDelivery.objects.select_related("channel").get(id=delivery_id)
        payload = dict(delivery.payload or {})
        CommunityBridgeMessageLink.objects.update_or_create(
            source_platform=delivery.source_platform,
            source_channel_id=delivery.source_channel_id,
            source_message_id=delivery.source_message_id,
            destination_platform=delivery.target_platform,
            defaults={
                "channel": delivery.channel,
                "source_parent_message_id": delivery.source_parent_message_id,
                "source_author_id": str(payload.get("source_author_id") or "").strip(),
                "destination_channel_id": str(destination_channel_id or "").strip(),
                "destination_message_id": str(destination_message_id or "").strip(),
                "destination_parent_message_id": str(destination_parent_message_id or "").strip(),
                "source_payload": payload,
                "destination_payload": destination_payload or {},
                "source_deleted_at": None,
                "destination_deleted_at": None,
            },
        )
        delivery.status = CommunityBridgeDeliveryStatus.COMPLETED
        delivery.completed_at = timezone.now()
        delivery.locked_at = None
        delivery.last_error = ""
        delivery.save(update_fields=["status", "completed_at", "locked_at", "last_error", "updated_at"])
        _wake_waiting_child_deliveries(delivery)


def complete_delivery(*, delivery_id: int) -> None:
    CommunityBridgeDelivery.objects.filter(id=delivery_id).update(
        status=CommunityBridgeDeliveryStatus.COMPLETED,
        completed_at=timezone.now(),
        locked_at=None,
        last_error="",
        updated_at=timezone.now(),
    )


def mark_link_deleted(
    *,
    source_platform: str,
    source_channel_id: str,
    source_message_id: str,
    destination_platform: str,
) -> None:
    timestamp = timezone.now()
    CommunityBridgeMessageLink.objects.filter(
        source_platform=source_platform,
        source_channel_id=str(source_channel_id or "").strip(),
        source_message_id=str(source_message_id or "").strip(),
        destination_platform=destination_platform,
    ).update(source_deleted_at=timestamp, destination_deleted_at=timestamp, updated_at=timestamp)


def mark_delivery_retry(*, delivery_id: int, error_text: str, permanent: bool = False) -> None:
    delivery = CommunityBridgeDelivery.objects.filter(id=delivery_id).first()
    if not delivery:
        return
    now = timezone.now()
    error_message = str(error_text or "").strip()
    if permanent or int(delivery.attempts or 0) >= int(delivery.max_attempts or 0):
        delivery.status = CommunityBridgeDeliveryStatus.DEAD
        delivery.available_at = now
    else:
        delivery.status = CommunityBridgeDeliveryStatus.FAILED
        backoff = RETRY_DELAYS_SECONDS[min(max(int(delivery.attempts or 1) - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)]
        delivery.available_at = now + timedelta(seconds=backoff)
    delivery.locked_at = None
    delivery.last_error = error_message[:2000]
    delivery.save(update_fields=["status", "available_at", "locked_at", "last_error", "updated_at"])


def mark_delivery_waiting_for_parent(
    *, delivery_id: int, parent_message_id: str
) -> None:
    """Park a delivery until its source parent has a destination mapping.

    Dependency waits are deliberately separate from provider retries: no Slack,
    Discord, or Buzz request has happened yet, so waiting must not consume the
    delivery's bounded provider-attempt budget.
    """

    delivery = CommunityBridgeDelivery.objects.filter(id=delivery_id).first()
    if not delivery:
        return

    now = timezone.now()
    first_seen = delivery.dependency_first_seen_at or now
    dependency_attempts = int(delivery.dependency_attempts or 0) + 1
    max_age_seconds = max(
        1,
        int(
            getattr(
                settings,
                "COMMUNITY_BRIDGE_PARENT_DEPENDENCY_MAX_AGE_SECONDS",
                3600,
            )
            or 3600
        ),
    )
    max_attempts = max(
        1,
        int(
            getattr(
                settings,
                "COMMUNITY_BRIDGE_PARENT_DEPENDENCY_MAX_ATTEMPTS",
                360,
            )
            or 360
        ),
    )
    expired = (
        dependency_attempts >= max_attempts
        or (now - first_seen).total_seconds() >= max_age_seconds
    )

    delivery.status = (
        CommunityBridgeDeliveryStatus.DEAD
        if expired
        else CommunityBridgeDeliveryStatus.WAITING_FOR_PARENT
    )
    delivery.attempts = max(0, int(delivery.attempts or 0) - 1)
    delivery.dependency_attempts = dependency_attempts
    delivery.dependency_first_seen_at = first_seen
    delivery.available_at = (
        now if expired else now + timedelta(seconds=PARENT_DEPENDENCY_RETRY_SECONDS)
    )
    delivery.locked_at = None
    delivery.last_error = (
        f"parent_mapping_timeout:{str(parent_message_id or '').strip()}"
        if expired
        else f"parent_mapping_pending:{str(parent_message_id or '').strip()}"
    )
    delivery.save(
        update_fields=[
            "status",
            "attempts",
            "dependency_attempts",
            "dependency_first_seen_at",
            "available_at",
            "locked_at",
            "last_error",
            "updated_at",
        ]
    )


def _wake_waiting_child_deliveries(parent: CommunityBridgeDelivery) -> int:
    """Make children immediately eligible after their parent link is committed."""

    now = timezone.now()
    return CommunityBridgeDelivery.objects.filter(
        channel=parent.channel,
        source_platform=parent.source_platform,
        source_channel_id=parent.source_channel_id,
        source_parent_message_id=parent.source_message_id,
        target_platform=parent.target_platform,
        status=CommunityBridgeDeliveryStatus.WAITING_FOR_PARENT,
    ).update(
        status=CommunityBridgeDeliveryStatus.PENDING,
        available_at=now,
        locked_at=None,
        last_error="",
        updated_at=now,
    )


def reset_stale_processing_deliveries(max_age_seconds: int = 300) -> int:
    cutoff = timezone.now() - timedelta(seconds=max(1, int(max_age_seconds or 300)))
    return CommunityBridgeDelivery.objects.filter(
        status=CommunityBridgeDeliveryStatus.PROCESSING,
        locked_at__lt=cutoff,
    ).update(
        status=CommunityBridgeDeliveryStatus.FAILED,
        available_at=timezone.now(),
        locked_at=None,
        updated_at=timezone.now(),
    )


def _normalize_slack_event(payload: dict) -> Optional[dict]:
    event = dict(payload.get("event") or {})
    event_type = str(event.get("type") or "").strip()
    if event_type in {"reaction_added", "reaction_removed"}:
        return _normalize_slack_reaction(event, event_type=event_type)
    if event_type != "message":
        return None
    if str(event.get("channel_type") or "").strip() not in BRIDGEABLE_SLACK_CHANNEL_TYPES:
        return None
    if bool(event.get("is_ext_shared_channel")) or bool(event.get("is_shared")):
        return None

    subtype = str(event.get("subtype") or "").strip()
    if bool(event.get("hidden")) and subtype not in {"message_changed", "message_deleted"}:
        return None
    bridge_bot_user_id = str(getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") or "").strip()

    if subtype in {"", "bot_message", "thread_broadcast"}:
        source_message_id = str(event.get("ts") or "").strip()
        user_id = str(event.get("user") or "").strip()
        if not source_message_id or not user_id or user_id == bridge_bot_user_id:
            return None
        source_parent_message_id = _normalize_parent_message_id(
            thread_ts=str(event.get("thread_ts") or "").strip(),
            source_message_id=source_message_id,
        )
        raw_text = str(event.get("text") or "")
        return {
            "delivery_type": CommunityBridgeDeliveryType.CREATE,
            "source_channel_id": str(event.get("channel") or "").strip(),
            "source_message_id": source_message_id,
            "source_parent_message_id": source_parent_message_id,
            "source_author_id": user_id,
            "source_author_display_name": "",
            "text": sanitize_slack_text(raw_text),
            "attachments": normalize_slack_files(event.get("files") or []),
            "metadata": {
                "broadcast": subtype == "thread_broadcast"
                or bool(event.get("reply_broadcast")),
                "slack_created_at": _slack_timestamp_seconds(source_message_id),
                "slack_raw_text": raw_text,
            },
        }

    if subtype == "message_changed":
        message = dict(event.get("message") or {})
        source_message_id = str(message.get("ts") or "").strip()
        user_id = str(message.get("user") or "").strip()
        if not source_message_id or not user_id or user_id == bridge_bot_user_id:
            return None
        source_parent_message_id = _normalize_parent_message_id(
            thread_ts=str(message.get("thread_ts") or "").strip(),
            source_message_id=source_message_id,
        )
        raw_text = str(message.get("text") or "")
        return {
            "delivery_type": CommunityBridgeDeliveryType.EDIT,
            "source_channel_id": str(event.get("channel") or "").strip(),
            "source_message_id": source_message_id,
            "source_parent_message_id": source_parent_message_id,
            "source_author_id": user_id,
            "source_author_display_name": "",
            "text": sanitize_slack_text(raw_text),
            "attachments": normalize_slack_files(message.get("files") or []),
            "metadata": {
                "broadcast": str(message.get("subtype") or "").strip()
                == "thread_broadcast"
                or bool(message.get("reply_broadcast")),
                "slack_created_at": _slack_timestamp_seconds(source_message_id),
                "slack_raw_text": raw_text,
            },
        }

    if subtype == "message_deleted":
        previous = dict(event.get("previous_message") or {})
        source_message_id = str(event.get("deleted_ts") or previous.get("ts") or "").strip()
        user_id = str(previous.get("user") or "").strip()
        if not source_message_id or not user_id or user_id == bridge_bot_user_id:
            return None
        source_parent_message_id = _normalize_parent_message_id(
            thread_ts=str(previous.get("thread_ts") or "").strip(),
            source_message_id=source_message_id,
        )
        return {
            "delivery_type": CommunityBridgeDeliveryType.DELETE,
            "source_channel_id": str(event.get("channel") or "").strip(),
            "source_message_id": source_message_id,
            "source_parent_message_id": source_parent_message_id,
            "source_author_id": user_id,
            "source_author_display_name": "",
            "text": "",
            "attachments": [],
        }

    return None


def _normalize_slack_reaction(event: dict, *, event_type: str) -> Optional[dict]:
    item = dict(event.get("item") or {})
    channel_id = str(item.get("channel") or "").strip()
    target_message_id = str(item.get("ts") or "").strip()
    author_id = str(event.get("user") or "").strip()
    slack_reaction = str(event.get("reaction") or "").strip().lower()
    bridge_bot_user_id = str(
        getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") or ""
    ).strip()
    emoji = slack_reaction_to_emoji(slack_reaction)
    if (
        str(item.get("type") or "").strip() != "message"
        or not channel_id
        or not target_message_id
        or not author_id
        or author_id == bridge_bot_user_id
        or not emoji
    ):
        return None
    return {
        "delivery_type": (
            CommunityBridgeDeliveryType.REACTION_ADD
            if event_type == "reaction_added"
            else CommunityBridgeDeliveryType.REACTION_REMOVE
        ),
        "source_channel_id": channel_id,
        "source_message_id": reaction_object_id(
            message_id=target_message_id,
            reaction=slack_reaction,
            author_id=author_id,
        ),
        "source_parent_message_id": target_message_id,
        "source_author_id": author_id,
        "source_author_display_name": "",
        "text": emoji,
        "attachments": [],
        "metadata": {
            "slack_message_id": target_message_id,
            "slack_reaction": slack_reaction,
        },
    }


def _get_enabled_channel(*, source_platform: str, channel_id: str) -> Optional[CommunityBridgeChannel]:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    filters = {"enabled": True}
    if source_platform == CommunityBridgePlatform.SLACK:
        filters["slack_channel_id"] = normalized_channel_id
    else:
        filters["destination_platform"] = source_platform
        filters["destination_channel_id"] = normalized_channel_id
    return CommunityBridgeChannel.objects.filter(**filters).first()


def _target_platform(*, channel: CommunityBridgeChannel, source_platform: str) -> str:
    if source_platform == CommunityBridgePlatform.SLACK:
        return channel.destination_platform
    if source_platform == channel.destination_platform:
        return CommunityBridgePlatform.SLACK
    raise ValueError("source platform does not match the mapped destination")


def _channel_id_for_platform(*, channel: CommunityBridgeChannel, platform: str) -> str:
    if platform == CommunityBridgePlatform.SLACK:
        return channel.slack_channel_id
    if platform == channel.destination_platform:
        return channel.destination_channel_id
    return ""


def _canonicalize_event(
    *,
    receipt_key: str,
    source_platform: str,
    source_channel_id: str,
    normalized_event: dict,
) -> dict:
    delivery_type = str(normalized_event.get("delivery_type") or "")
    if delivery_type in {
        CommunityBridgeDeliveryType.REACTION_ADD,
        CommunityBridgeDeliveryType.REACTION_REMOVE,
    } and not emoji_to_slack_reaction(str(normalized_event.get("text") or "")):
        raise ValueError("reaction is not in the approved bridge set")
    attachments = tuple(
        BridgeAttachment(
            title=str(item.get("title") or item.get("url") or "Attachment"),
            url=str(item.get("url") or ""),
        )
        for item in (normalized_event.get("attachments") or [])
        if isinstance(item, dict)
    )
    event = CanonicalBridgeEvent(
        receipt_key=receipt_key,
        source_platform=source_platform,
        source_channel_id=source_channel_id,
        source_message_id=str(normalized_event.get("source_message_id") or ""),
        source_parent_message_id=str(normalized_event.get("source_parent_message_id") or ""),
        source_author_id=str(normalized_event.get("source_author_id") or ""),
        source_author_display_name=str(normalized_event.get("source_author_display_name") or ""),
        delivery_type=delivery_type,
        text=str(normalized_event.get("text") or ""),
        attachments=attachments,
        metadata=normalized_event.get("metadata") or {},
    )
    return event.normalized_payload()


def _mark_receipt_ignored(receipt: CommunityBridgeReceipt, *, reason: str) -> dict:
    receipt.status = CommunityBridgeReceiptStatus.IGNORED
    receipt.error_text = str(reason or "").strip()
    receipt.processed_at = timezone.now()
    receipt.save(update_fields=["status", "error_text", "processed_at", "updated_at"])
    return {"status": "ignored", "receipt_id": receipt.id, "reason": reason}


def _normalize_parent_message_id(*, thread_ts: str, source_message_id: str) -> str:
    normalized_thread_ts = str(thread_ts or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    if normalized_thread_ts and normalized_thread_ts != normalized_source_message_id:
        return normalized_thread_ts
    return ""


def _slack_timestamp_seconds(value: str) -> int:
    try:
        seconds = int(str(value or "").split(".", 1)[0])
    except (TypeError, ValueError):
        return 0
    return max(0, seconds)


def _serialize_delivery(delivery: CommunityBridgeDelivery) -> dict:
    return {
        "id": delivery.id,
        "created_at": int(delivery.created_at.timestamp()),
        "channel_id": delivery.channel_id,
        "target_platform": delivery.target_platform,
        "source_platform": delivery.source_platform,
        "delivery_type": delivery.delivery_type,
        "source_event_key": delivery.source_event_key,
        "source_channel_id": delivery.source_channel_id,
        "source_message_id": delivery.source_message_id,
        "source_parent_message_id": delivery.source_parent_message_id,
        "target_channel_id": delivery.target_channel_id,
        "payload": dict(delivery.payload or {}),
        "attempts": int(delivery.attempts or 0),
        "max_attempts": int(delivery.max_attempts or 0),
        "channel": {
            "slack_workspace_id": delivery.channel.slack_workspace_id,
            "slack_channel_id": delivery.channel.slack_channel_id,
            "destination_platform": delivery.channel.destination_platform,
            "destination_workspace_id": delivery.channel.destination_workspace_id,
            "destination_channel_id": delivery.channel.destination_channel_id,
            "destination_channel_name": delivery.channel.destination_channel_name,
            "discord_channel_id": delivery.channel.discord_channel_id,
            "slack_channel_name": delivery.channel.slack_channel_name,
            "discord_channel_name": delivery.channel.discord_channel_name,
        },
    }
