"""Account and Slack service interfaces for the same private request queue."""

from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.views import VolunteerView
from core.models import User
from core.permissions import HasStrictRooApiKey
from .manual_time import (
    SLACK_RECEIPT, SLACK_REVIEW, SLACK_STAGES, approve, enabled, profile_recipient,
    request_dto, requests_for, require_admin, submit,
)
from .manual_time_rules import CATALOGUE, ManualTimeError
from .models import PointsRequest, PointsRequestDelivery
from .permissions import is_points_admin_user
from .services import PointsService


class ManualTimeErrors:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not enabled():
            raise NotFound()

    def handle_exception(self, exc):
        if isinstance(exc, ManualTimeError):
            return Response({"error": str(exc)}, status=exc.status)
        return super().handle_exception(exc)


class ChatManualTimeView(ManualTimeErrors, VolunteerView):
    def get(self, request, request_id=None):
        actor = request.user
        if request_id is not None:
            row = PointsRequest.objects.filter(pk=request_id, manual_time_key__isnull=False).select_related("native_recipient").first()
            if row is None:
                raise NotFound()
            private = row.native_recipient_id == actor.pk
            if not private:
                require_admin(actor)
            return Response(request_dto(row, review=not private), headers={"Cache-Control": "private, no-store"})
        mode = request.query_params.get("mode")
        if mode == "members":
            require_admin(actor)
            query = request.query_params.get("q", "").strip()[:80]
            if len(query) < 2:
                return Response({"members": []})
            users = User.objects.filter(
                is_active=True,
                pk__in=CommunityChatDevice.objects.filter(
                    status="verified", revoked_at__isnull=True,
                ).values("user_id"),
            ).exclude(pk=actor.pk).filter(
                Q(first_name__icontains=query) | Q(last_name__icontains=query),
            ).order_by("first_name", "last_name", "pk")[:20]
            return Response({"members": [
                {"id": str(user.community_chat_profile_id), "name": user.full_name}
                for user in users if user.community_chat_profile_id
            ]})
        if mode == "badge-candidates":
            require_admin(actor)
            rows = (PointsRequest.objects.filter(
                manual_time_key__isnull=False, status="approved", ledger_entry__isnull=False,
            ).exclude(manual_time__badge_candidates=[]).select_related("native_recipient")
                .order_by("-approved_at", "-pk"))
            return Response({"requests": [request_dto(row, review=True) for row in rows[:50]]},
                            headers={"Cache-Control": "private, no-store"})
        review = mode == "review"
        rows = requests_for(actor, review=review)
        focus = request.query_params.get("request", "")
        if review and focus.isascii() and focus.isdecimal() and len(focus) <= 18:
            rows = rows.filter(pk=int(focus))
        return Response({
            "catalogue": CATALOGUE,
            "can_admin": is_points_admin_user(actor),
            "requests": [request_dto(row, review=review) for row in rows[:50]],
        }, headers={"Cache-Control": "private, no-store"})

    def post(self, request, request_id=None):
        actor = request.user
        if request_id is not None:
            return Response(request_dto(approve(actor, request_id)))
        if not isinstance(request.data, dict):
            raise ManualTimeError("Invalid request.")
        target = request.data.get("target_profile_id")
        if target is not None:
            require_admin(actor)
        recipient = profile_recipient(target) if target is not None else actor
        return Response(request_dto(submit(actor, recipient, request.data, direct=target is not None)))


class SlackManualTimeView(ManualTimeErrors, APIView):
    permission_classes = (HasStrictRooApiKey,)

    def get(self, request, operation=None):
        if operation is not None:
            raise NotFound()
        return Response(CATALOGUE)

    def post(self, request, operation):
        data = request.data
        if not isinstance(data, dict):
            raise ManualTimeError("Invalid request.")
        workspace = getattr(settings, "COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID", "")
        if not workspace or data.get("workspace") != workspace:
            raise ManualTimeError("Slack workspace is unavailable.", 403)
        if operation == "claim":
            return Response(claim())
        if operation == "finish":
            finish(data)
            return Response({"ok": True})
        actor_id = data.get("actor")
        actor = PointsService.get_user_by_slack_id(actor_id) if isinstance(actor_id, str) and actor_id else None
        if actor is None:
            raise ManualTimeError("Link an active MLAI account to Slack first.", 403)
        if operation == "check-admin":
            require_admin(actor)
            return Response({"ok": True})
        if operation == "approve":
            if type(data.get("request_id")) is not int:
                raise ManualTimeError("Invalid request number.")
            return Response(request_dto(approve(
                actor, data.get("request_id"), slack_message=(data.get("channel"), data.get("ts")),
            )))
        if operation == "award":
            require_admin(actor)
            target = data.get("target")
            recipient = PointsService.get_user_by_slack_id(target) if isinstance(target, str) and target else None
            if recipient is None:
                raise ManualTimeError("Choose an active linked member.", 404)
            return Response(request_dto(submit(actor, recipient, data, direct=True)))
        if operation == "request":
            return Response(request_dto(submit(actor, actor, data)))
        raise NotFound()


@transaction.atomic
def claim():
    row = (
        PointsRequestDelivery.objects.select_for_update(skip_locked=True)
        .filter(stage__in=SLACK_STAGES, available_at__lte=timezone.now(), delivered_at__isnull=True)
        .order_by("available_at", "pk").first()
    )
    if row is None:
        return None
    record = row.request
    if row.stage == SLACK_REVIEW and record.status != "pending":
        row.delivered_at = timezone.now()
        row.save(update_fields=["delivered_at"])
        return None
    user = record.native_recipient
    if row.stage == SLACK_RECEIPT and (not user or not user.is_active or not user.slack_id):
        row.delivered_at = timezone.now()
        row.save(update_fields=["delivered_at"])
        return None
    token = uuid4().hex
    row.transition = {"lease": token}
    row.attempts += 1
    row.available_at = timezone.now() + timedelta(seconds=90)
    row.save(update_fields=["transition", "attempts", "available_at"])
    return {
        "id": row.pk, "stage": row.stage, "lease": token, "attempts": row.attempts,
        "target": user.slack_id if row.stage == SLACK_RECEIPT else "",
        "request": request_dto(record), "created": record.created_at.timestamp(),
    }


@transaction.atomic
def finish(data):
    delivery = PointsRequestDelivery.objects.filter(pk=data.get("id"), stage__in=SLACK_STAGES).first()
    if delivery is None:
        raise NotFound()
    # Lock the request first, matching the transaction that queues an approval.
    record = PointsRequest.objects.select_for_update().get(pk=delivery.request_id)
    row = PointsRequestDelivery.objects.select_for_update().get(pk=delivery.pk)
    if row.delivered_at:
        return
    if row.transition.get("lease") != data.get("lease"):
        raise ManualTimeError("Delivery lease expired.", 409)
    channel, ts = data.get("channel"), data.get("ts")
    if not isinstance(channel, str) or not channel or len(channel) > 50 or not isinstance(ts, str) or not ts or len(ts) > 50:
        raise ManualTimeError("Missing delivery receipt.")
    if row.stage == SLACK_REVIEW:
        record.slack_channel_id = channel
        record.slack_summary_message_ts = ts
        record.save(update_fields=["slack_channel_id", "slack_summary_message_ts", "updated_at"])
    row.external_confirmation_id = ts
    row.delivered_at = timezone.now()
    row.save(update_fields=["external_confirmation_id", "delivered_at"])
