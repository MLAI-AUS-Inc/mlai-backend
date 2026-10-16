"""Authenticated demand for the next fifty Slack timeline entries.

This schedules existing workers; HTTP requests perform no Slack content I/O.
The request key is an opaque timeline boundary used only for coalescing. It is
never trusted as a Slack cursor, membership proof, or history permission.
"""
import math
import re
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from integrations.models import BridgeSyncState, CommunityBridgeChannel, CommunityBridgeDelivery
from integrations.services.message_sync import recent_history
from integrations.services.message_sync.history import ensure_state
from integrations.services.message_sync.scheduler import schedule_job
from integrations.services.slack_owner_inventory_api import InventoryError


def _wake(job):
    # An explicit scroll never steals a lease or bypasses provider backoff.
    if job.lease_token is None and not job.backoff_seconds and not job.last_error_code:
        job.due_at = timezone.now()
        job.save(update_fields=["due_at"])


def _public_progress(state, key):
    job = schedule_job(state, "archive")
    job = state.jobs.select_for_update().get(pk=job.pk)
    from integrations.services.message_sync.head_repair import head_scope
    progress = dict(job.checkpoint or {})
    scope = head_scope(state)
    if progress.get("authority_generation") != scope:
        progress = {"authority_generation": scope}
    complete = bool(progress.get("timeline_complete"))
    updated = recent_history.request_batch(progress, key, complete=complete)
    if updated != job.checkpoint:
        job.checkpoint = updated
        job.save(update_fields=["checkpoint"])
    scanning = not complete and recent_history.request_pending(updated, key)
    if scanning:
        _wake(job)
    deliveries = CommunityBridgeDelivery.objects.filter(
        channel=state.public_channel, target_platform="buzz",
        source_platform="slack", source_channel_id=state.source_channel_id,
    )
    queued = deliveries.filter(Q(status__in=["pending", "processing"]) | Q(status="failed", attempts__lt=F("max_attempts"))).exists()
    failed = deliveries.filter(status="failed", attempts__gte=F("max_attempts")).exists()
    error = job.last_error_code or ("delivery_failed" if failed else "")
    return _payload(complete, scanning or queued, error, job=job)


def _private_progress(conversation, key):
    from integrations.services.slack_chat_refresh import _refresh_status
    marker = recent_history.private_progress(conversation, lock=True)
    complete = conversation.history_backfilled_at is not None or bool(
        marker is not None and (marker.metadata or {}).get("complete")
    )
    progress = dict(marker.metadata or {}) if marker else {}
    updated = recent_history.request_batch(progress, key, complete=complete)
    if marker is not None and updated != progress:
        marker.metadata = updated
        marker.save(update_fields=["metadata", "updated_at"])
    state = ensure_state(conversation)
    state = BridgeSyncState.objects.select_for_update().get(pk=state.pk)
    if state.status in {"paused", "revoked"}:
        raise InventoryError("inventory_conversation_unavailable", 404)
    job = state.jobs.select_for_update().get(pk=schedule_job(state, "archive").pk)
    scanning = not complete and recent_history.request_pending(updated, key)
    if scanning:
        _wake(job)
    status = _refresh_status(conversation)
    error = job.last_error_code or ("delivery_failed" if status.get("failed_messages", 0) else "")
    return _payload(complete, scanning or status["queued_messages"] > 0, error, job=job)


def _payload(complete, pending, error, *, job=None):
    deferred = error in {"BudgetDeferred", "SlackDmMirrorRateLimited", "ratelimited"}
    retry = max(3, math.ceil((job.due_at - timezone.now()).total_seconds())) if deferred and job is not None else 3
    return {
        "has_more": not complete, "pending": pending,
        "retry_after_seconds": retry,
        **({"error": "slack_history_unavailable"} if error and not deferred else {}),
    }


def request_history_page(user, *, public_key, channel_id, request_key):
    """Authorize an exact owner/device/source before extending its import target."""
    if not recent_history.enabled():
        # Legacy workers already import the whole permitted archive.
        return _payload(True, False, "")
    try:
        channel_id = str(UUID(str(channel_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InventoryError("invalid_channel_id", 400) from exc
    if not isinstance(request_key, str) or not re.fullmatch(r"[0-9a-f]{64}", request_key):
        raise InventoryError("invalid_history_request", 400)
    from community_chat.models import CommunityChatDevice
    from community_chat.onboarding import require_community_access
    require_community_access(user)
    key = str(public_key or "").strip().lower()
    if not CommunityChatDevice.objects.filter(
        user=user, public_key=key, status="verified", revoked_at__isnull=True,
    ).exists():
        raise InventoryError("device_unverified", 403)
    mapping = CommunityBridgeChannel.objects.filter(
        enabled=True,
        destination_platform="buzz", destination_channel_id=channel_id,
    ).first()
    if mapping is not None:
        # A public bridge is already approved to import this shared source.
        # This hint grants no room access and returns no content or names. It
        # also works for community members who have not linked personal Slack.
        # Public inbox routing locks state before account/device authority.
        with transaction.atomic():
            state = ensure_state(mapping)
            state = BridgeSyncState.objects.select_for_update().get(pk=state.pk)
            if state.status in {"paused", "revoked"}:
                raise InventoryError("inventory_conversation_unavailable", 404)
            if not CommunityChatDevice.objects.filter(
                user=user, public_key=key, status="verified", revoked_at__isnull=True,
            ).exists():
                raise InventoryError("device_unverified", 403)
            current = CommunityBridgeChannel.objects.select_for_update().get(pk=mapping.pk)
            if (not current.enabled or current.destination_platform != "buzz"
                    or current.destination_channel_id != channel_id
                    or current.destination_workspace_id != mapping.destination_workspace_id
                    or current.slack_workspace_id != state.workspace_id
                    or current.slack_channel_id != state.source_channel_id):
                raise InventoryError("inventory_source_changed", 409)
            return _public_progress(state, request_key)
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_refresh import _authorized_conversation
    conversation = _authorized_conversation(user, channel_id, key)
    authority = dm._capture_slack_grant_api_authority(conversation.grant, refresh_token=False)
    with transaction.atomic():
        dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        if dm._locked_active_verified_device(user.pk, key) is None:
            raise InventoryError("device_unverified", 403)
        conversation = _authorized_conversation(user, channel_id, key, lock=True)
        if conversation.grant_id != authority.grant_id:
            raise InventoryError("slack_authority_changed", 403)
        return _private_progress(conversation, request_key)
