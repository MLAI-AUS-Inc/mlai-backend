"""Owner/device-scoped foreground refreshes using the existing private queue."""

from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from integrations.models import CommunityBridgeDeliveryStatus, CommunityBridgePlatform
from integrations.models import (
    SlackDmMirrorConversation,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
)

# A completed, content-free queue marker. It is never sent to Slack or the relay.
FOREGROUND_STATE_ID = "history-state:foreground-refresh"


# Stored in the existing conversation error field; survives worker restarts.
HISTORY_RETRY_PREFIX = "history_scan_retry:"


def exclude_recent_history_attempts(queryset, *, now):
    """Allow other imports to proceed while failed or leased scans cool down."""
    return queryset.exclude(
        Q(last_error__startswith="history_scan_processing:")
        | Q(last_error__startswith=HISTORY_RETRY_PREFIX),
        updated_at__gte=now - timedelta(minutes=5),
    )


def prioritize_open_conversations(queryset, *, conversation_field="pk"):
    """Prefer recently opened conversations without bypassing worker rate limits."""
    return queryset.annotate(
        foreground_refresh=Exists(
            SlackDmMirrorDelivery.objects.filter(
                conversation_id=OuterRef(conversation_field),
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=FOREGROUND_STATE_ID,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                available_at__gt=timezone.now(),
            )
        )
    )


def _authorized_conversation(user, channel_id, public_key, *, lock=False):
    from integrations.services.slack_dm_mirror import (
        _require_private_channel_consent,
        SlackDmMirrorAuthorizationError,
    )

    try:
        channel_id = UUID(str(channel_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({"channel_id": "Use a valid channel ID."}) from exc
    grants = SlackDmMirrorGrant.objects.filter(
        user=user,
        status="active",
        revoked_at__isnull=True,
    )
    if lock:
        grants = grants.select_for_update(of=("self",))
    # Lock grant before conversation, matching the existing worker lock order.
    grant = grants.filter(conversations__mlai_channel_id=channel_id).first()
    if grant is None:
        raise NotFound("Slack conversation is not available to this account.")
    conversations = SlackDmMirrorConversation.objects.filter(
        grant=grant,
        mlai_channel_id=channel_id,
        status="live",
    )
    if lock:
        conversations = conversations.select_for_update()
    conversation = conversations.first()
    key = str(public_key or "").strip().lower()
    if (
        conversation is None
        or not key
        or key not in (conversation.participant_buzz_pubkeys or [])
    ):
        raise NotFound("Slack conversation is not available to this device.")
    conversation.grant = grant
    try:
        _require_private_channel_consent(conversation)
    except SlackDmMirrorAuthorizationError as exc:
        raise PermissionDenied("Reconnect Slack to import private channels.") from exc
    return conversation


def _refresh_status(conversation):
    rows = (
        conversation.deliveries.filter(source_platform=CommunityBridgePlatform.SLACK)
        .exclude(source_message_id__startswith="history-state:")
        .exclude(source_message_id__startswith="registration-state:")
        .filter(
            Q(metadata__history_outside_window__isnull=True)
            | Q(metadata__history_outside_window=False)
        )
        .filter(
            Q(metadata__history_recovery_superseded__isnull=True)
            | Q(metadata__history_recovery_superseded=False)
        )
    )
    counts = rows.aggregate(
        imported_messages=Count("pk", filter=Q(status="completed", operation="create")),
        queued_messages=Count("pk", filter=Q(status__in=("pending", "processing"))),
        failed_messages=Count("pk", filter=Q(status__in=("failed", "dead"))),
    )
    failed = counts["failed_messages"] > 0
    pending = counts["queued_messages"] > 0
    scan_error = bool(
        conversation.last_error
        and not conversation.last_error.startswith("history_scan_processing:")
    )
    state = (
        "error"
        if failed or scan_error
        else (
            "syncing"
            if conversation.history_backfilled_at is None or pending
            else "complete"
        )
    )
    from integrations.services.slack_chat_catalog import conversation_metadata

    return {
        "channel_id": str(conversation.mlai_channel_id),
        "status": state,
        "source_archived": bool(
            conversation_metadata(conversation).get("source_archived")
        ),
        "history_days": conversation.grant.history_days,
        "history_scan_complete": conversation.history_backfilled_at is not None,
        "last_synced_at": conversation.history_backfilled_at,
        **counts,
    }


def conversation_refresh_status(user, channel_id, *, public_key):
    """Read refresh progress only for the account's provisioned device."""
    return _refresh_status(_authorized_conversation(user, channel_id, public_key))


@transaction.atomic
def request_conversation_refresh(user, channel_id, *, public_key):
    """Prioritize one mirror; repeated opens preserve a running scan's cursor."""
    from integrations.services.slack_dm_mirror import (
        _ensure_history_state,
        _mark_conversation_history_due,
    )

    conversation = _authorized_conversation(user, channel_id, public_key, lock=True)
    now = timezone.now()
    marker = conversation.deliveries.filter(
        source_platform=CommunityBridgePlatform.SLACK,
        source_message_id=FOREGROUND_STATE_ID,
        status=CommunityBridgeDeliveryStatus.COMPLETED,
    ).first()
    # Coalesce rapid reopen/device requests. Never restart a partially scanned import.
    recent = marker is not None and marker.updated_at > now - timedelta(seconds=30)
    if conversation.history_backfilled_at is not None and not recent:
        _mark_conversation_history_due(
            conversation,
            reason="Opened in MLAI Chat",
            reset_deliveries=False,
            reconcile_current_state=True,
        )
    marker = _ensure_history_state(
        conversation,
        source_message_id=FOREGROUND_STATE_ID,
        metadata={"history_scan_state": "foreground-refresh"},
    )
    marker.available_at = now + timedelta(minutes=5)
    marker.save(update_fields=("available_at", "updated_at"))
    return _refresh_status(conversation)
