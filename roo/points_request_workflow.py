"""Shared approval and delivery rules for Slack and native Roo requests."""

import re
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.profile_progression import public_position
from community_chat.volunteer.services import contribution_total, lock_member, state_for
from integrations.models import CommunityBridgeChannel
from roo.models import PointsRequestDelivery
from roo.services import PointsService, record_volunteer_credit

ROO_POINTS_CHANNEL_ID = UUID("e39d3ec7-0ea1-4d78-95fa-adb0d7cafb65")
EVENT_ID = re.compile(r"[0-9a-f]{64}")
CONFIRMATION = "confirmation"
CELEBRATION = "celebration"
REPOST = "repost"
SUMMARY = "summary"
SLACK_CONFIRMATION = "slack_confirmation"


def verified_devices(user):
    return CommunityChatDevice.objects.filter(
        user=user, status="verified", revoked_at__isnull=True,
    ).order_by("public_key")


def mention(user):
    name = " ".join((user.full_name or "MLAI member").split())[:80]
    return f"@{name}", list(verified_devices(user).values_list("public_key", flat=True)[:20])


def bridge_channel(slack_channel_id):
    return CommunityBridgeChannel.objects.filter(
        slack_workspace_id=settings.COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID,
        slack_channel_id=slack_channel_id,
        destination_platform="buzz",
        audience="community",
        mirror_decision="mirror",
        enabled=True,
        sync_replies=True,
    ).first()


def enqueue(request, stage, channel_id, parent_id, content, mention_keys, transition=None):
    return PointsRequestDelivery.objects.get_or_create(
        request=request, stage=stage, defaults=dict(
            channel_id=channel_id, parent_id=parent_id, content=content,
            mention_keys=mention_keys, transition=transition or {},
        ),
    )[0]


def slack_confirmation_text(request):
    return (
        f"✅ Points request approved by <@{request.approved_by_slack_id}>.\n\n"
        f"<@{request.target_slack_id}> received {request.points} points.\nReason: {request.reason}"
    )


def approve_locked_request(request, user, *, admin_slack_id=None, admin=None, recover_slack=False):
    """Award an already locked pending request and persist its notifications."""
    user = lock_member(user)
    native = request.native_channel_id is not None
    bridge = None
    if getattr(settings, "ROO_POINTS_LEVEL_UP_ENABLED", False) and not native:
        bridge = bridge_channel(request.slack_channel_id)
    capture = native or bridge is not None
    state = state_for(user) if capture else None
    before = public_position(contribution_total(user, state)) if capture else None
    ledger, _ = PointsService.award(
        user=user, delta=request.points, source="MANUAL", description=request.reason,
        created_by_slack_id=admin_slack_id,
        idempotency_key=f"points_request:{request.pk}:approve",
        reference_type="POINTS_REQUEST", reference_id=str(request.pk),
    )
    record_volunteer_credit(ledger)
    request.status = "approved"
    request.approved_at = timezone.now()
    request.approved_by_slack_id = admin_slack_id
    request.ledger_entry = ledger
    request.save(update_fields=[
        "status", "approved_at", "approved_by_slack_id", "ledger_entry", "updated_at",
    ])
    if not capture:
        return ledger

    if recover_slack and bridge and request.slack_summary_message_ts:
        enqueue(request, SLACK_CONFIRMATION, bridge.destination_channel_id, "",
                slack_confirmation_text(request), [])

    label, keys = mention(user)
    if native:
        approver_label, approver_keys = mention(admin)
        enqueue(request, CONFIRMATION, request.native_channel_id, request.native_root_id,
                f"✅ Points request approved by {approver_label}.\n\n"
                f"{label} received {request.points} points.\nReason: {request.reason}",
                sorted(set(keys + approver_keys)))

    if not getattr(settings, "ROO_POINTS_LEVEL_UP_ENABLED", False):
        return ledger
    state.refresh_from_db()
    after = public_position(contribution_total(user, state))
    if not keys or before is None or after is None or after["level"] <= before["level"]:
        return ledger
    transition = {
        "policy": before["policy"], "from": before["level"], "to": after["level"],
        "ledger_id": ledger.pk,
    }
    text = f"🎉 {label} hopped from Level {before['level']} to Level {after['level']}! Way to go! 🦘"
    channel_id = request.native_channel_id if native else bridge.destination_channel_id
    enqueue(request, CELEBRATION, channel_id, request.native_root_id, text, keys, transition)
    enqueue(request, REPOST, ROO_POINTS_CHANNEL_ID, "", text, keys, transition)
    return ledger
