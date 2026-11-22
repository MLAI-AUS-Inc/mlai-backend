"""Private manual-time requests for both the signed Slack service and Chat."""

import hashlib
from uuid import UUID

from django.conf import settings
from django.db import transaction

from community_chat.models import CommunityChatDevice
from core.models import User
from .manual_time_rules import ACTIONS, ManualTimeError, normalize, roo_text
from .models import PointsAdmin, PointsRequest
from .permissions import FULL_POINTS_ADMIN_ROLES, is_points_admin_user
from .points_request_workflow import approve_locked_request, enqueue

SLACK_REVIEW = "time_slack_review"
SLACK_RECEIPT = "time_slack_receipt"
SLACK_STAGES = (SLACK_REVIEW, SLACK_RECEIPT)


def enabled():
    return getattr(settings, "ROO_MANUAL_TIME_ENABLED", False)


def require_admin(actor):
    if not actor.is_active or not is_points_admin_user(actor):
        raise ManualTimeError("Points Admin access is required.", 403)


def profile_recipient(value):
    try:
        profile_id = UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ManualTimeError("Choose a member.") from None
    user = User.objects.filter(
        community_chat_profile_id=profile_id, is_active=True,
        pk__in=CommunityChatDevice.objects.filter(status="verified", revoked_at__isnull=True).values("user_id"),
    ).first()
    if user is None:
        raise ManualTimeError("That member is unavailable.", 404)
    return user


def queue_slack(row, stage):
    # Non-relay deliveries share the durable outbox and are consumed only by Roo.
    enqueue(row, stage, UUID(int=0), "", "", [])


@transaction.atomic
def submit(actor, recipient, data, *, direct=False):
    key, details = normalize(data)
    key = hashlib.sha256(f"{actor.pk}:{key}".encode()).hexdigest()
    if direct:
        require_admin(actor)
        if actor.pk == recipient.pk:
            raise ManualTimeError("Choose another member for a direct award.")
    elif actor.pk != recipient.pk:
        raise ManualTimeError("Request points for your own account.", 403)
    details.update(
        actor_id=actor.pk, direct=direct,
        badge_candidates=sorted({
            badge for action in details["action_ids"] for badge in ACTIONS[action]["badge_candidates"]
        }),
    )
    titles = " · ".join(ACTIONS[action]["label"] for action in details["action_ids"])
    row, created = PointsRequest.objects.get_or_create(
        manual_time_key=key,
        defaults={
            "manual_time": details,
            "native_recipient": recipient,
            "requester_slack_id": actor.slack_id or "",
            "target_slack_id": recipient.slack_id or "",
            "points": min(details["volunteer_minutes"] // 10, 2**31 - 1),
            "reason": titles,
        },
    )
    if not created:
        if row.native_recipient_id != recipient.pk or any(
            row.manual_time.get(name) != value for name, value in details.items() if name != "badge_candidates"
        ):
            raise ManualTimeError("This submission was used with different details.", 409)
        return row
    if direct:
        return approve(actor, row.pk, direct=True)
    queue_slack(row, SLACK_REVIEW)
    return row


@transaction.atomic
def approve(actor, request_id, *, direct=False, slack_message=None):
    require_admin(actor)
    row = PointsRequest.objects.select_for_update().filter(
        pk=request_id, manual_time_key__isnull=False,
    ).first()
    if row is None:
        raise ManualTimeError("Request is unavailable.", 404)
    if slack_message is not None and (
        (row.slack_channel_id, row.slack_summary_message_ts) != slack_message
        or not row.slack_summary_message_ts
    ):
        raise ManualTimeError("This review message is unavailable.", 404)
    if row.status == "approved":
        return row
    if row.status != "pending" or not row.native_recipient_id:
        raise ManualTimeError("This request is no longer pending.", 409)
    accounts = {
        user.pk: user for user in User.objects.select_for_update()
        .filter(pk__in={actor.pk, row.native_recipient_id}).order_by("pk")
    }
    actor = accounts[actor.pk]
    recipient = accounts[row.native_recipient_id]
    require_admin(actor)
    if not recipient.is_active:
        raise ManualTimeError("That member is unavailable.", 404)
    if direct:
        admin = PointsAdmin.objects.select_for_update().filter(
            user=actor, slack_user_id=actor.slack_id, is_active=True, role__in=FULL_POINTS_ADMIN_ROLES,
        ).first()
        if admin is None and not actor.is_superuser:
            raise ManualTimeError("A linked Points Admin account is required to make direct awards.", 403)
        if admin:
            from .services import PointsService

            allowance = PointsService.get_admin_allowance_status(admin.slack_user_id)
            if row.manual_time["amount_microroo"] > allowance.get("remaining_microroo", 0):
                raise ManualTimeError("This award exceeds your remaining weekly allowance.", 409)
    approve_locked_request(row, recipient, admin_slack_id=actor.slack_id or None, admin=actor)
    if recipient.slack_id:
        queue_slack(row, SLACK_RECEIPT)
    return row


def request_dto(row, *, review=False):
    details = row.manual_time
    result = {
        "id": row.pk,
        "status": row.status,
        "display_name": row.native_recipient.full_name or "MLAI member",
        "action_ids": details["action_ids"],
        "activity": row.reason,
        "volunteer_minutes": details["volunteer_minutes"],
        "roo": roo_text(details["volunteer_minutes"]),
        "ledger_id": row.ledger_entry_id,
    }
    if review:
        result["note"] = details["note"]
        if row.status == "approved":
            result["badge_candidates"] = details["badge_candidates"]
    return result


def requests_for(actor, *, review=False):
    rows = PointsRequest.objects.filter(manual_time_key__isnull=False).select_related("native_recipient")
    if review:
        require_admin(actor)
        return rows.filter(status="pending").order_by("created_at", "pk")
    return rows.filter(native_recipient=actor).order_by("-created_at", "-pk")
