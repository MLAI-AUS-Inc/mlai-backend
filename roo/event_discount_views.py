"""Strict Public Roo service boundary for private, confirmed event discounts."""

import re
from django.contrib.auth import get_user_model
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from core.permissions import HasIsolatedRooApiKey
from .event_discounts import EventDiscountError, EventDiscountService, list_redemptions, quote_payload, serialize_redemption, _scope
from .models import EventDiscountRedemption


class EventDiscountThrottle(SimpleRateThrottle):
    """Bound a trusted service caller's attempts per verified Slack actor."""

    scope = "event_discount_service"
    rate = "30/min"

    def get_cache_key(self, request, view):
        data = request.query_params if request.method == "GET" else request.data
        actor = str(data.get("slack_user_id") or "")[:30]
        return self.cache_format % {"scope": self.scope, "ident": actor + ":" + self.get_ident(request)}


def _actor(request):
    data = request.query_params if request.method == "GET" else request.data
    if any(key in data for key in ("user_id", "target_user_id", "target_slack_user_id")):
        raise EventDiscountError("invalid_actor", "Event discounts can only be redeemed for yourself.", 403)
    actor = data.get("slack_user_id")
    if not isinstance(actor, str) or not re.fullmatch(r"[UW][A-Z0-9]{1,29}", actor):
        raise EventDiscountError("invalid_actor", "A verified MLAI member is required.", 403)
    scope = _scope({key: data.get("slack_" + key, "") for key in ("team_id", "channel_id", "thread_ts")})
    user = get_user_model().objects.filter(slack_id=actor, is_active=True).first()
    if user is None:
        raise EventDiscountError("unlinked_member", "Link your MLAI account before redeeming an event discount.", 403)
    return user, scope


class EventDiscountAPIView(APIView):
    """Accept only Roo's isolated service key and its verified private context."""

    authentication_classes = []
    permission_classes = [HasIsolatedRooApiKey]
    throttle_classes = [EventDiscountThrottle]

    def handle_exception(self, exc):
        if isinstance(exc, EventDiscountError):
            return Response({"code": exc.code, "detail": exc.detail, **exc.extra}, status=exc.status_code,
                            headers={"Cache-Control": "private, no-store"})
        return super().handle_exception(exc)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class EventDiscountQuoteView(EventDiscountAPIView):
    """Quote the exact server-priced offer without reserving or spending points."""

    def post(self, request):
        user, scope = _actor(request)
        quote = EventDiscountService.quote(user=user, scope=scope, event_query=request.data.get("event_query"))
        return Response(quote_payload(quote), status=201)


class EventDiscountRedeemView(EventDiscountAPIView):
    """Persist one explicitly confirmed request, then attempt its fulfilment."""

    def post(self, request):
        user, scope = _actor(request)
        if request.data.get("confirmed") is not True:
            raise EventDiscountError("confirmation_required", "Confirm the quoted points cost in your private Roo thread.", 400)
        request_id = request.data.get("request_id")
        if not request_id or request.headers.get("Idempotency-Key") != request_id:
            raise EventDiscountError("invalid_request_id", "Matching request and idempotency IDs are required.", 400)
        row = EventDiscountService.reserve(user=user, quote_id=request.data.get("quote_id"), request_id=request_id, scope=scope)
        row = EventDiscountService.process(row.pk)
        return Response(serialize_redemption(row), status=202 if row.status == "processing" else 200)


class EventDiscountRedemptionsView(EventDiscountAPIView):
    """Read only the verified member's own durable event receipts."""

    def get(self, request, redemption_id=None):
        user, scope = _actor(request)
        if redemption_id is None:
            return Response(list_redemptions(user))
        row = EventDiscountRedemption.objects.select_related("quote", "user").filter(pk=redemption_id, user=user).first()
        if row is None or (row.quote.slack_team_id, row.quote.slack_channel_id, row.quote.slack_thread_ts) != tuple(scope[key] for key in ("team_id", "channel_id", "thread_ts")):
            raise EventDiscountError("redemption_not_found", "This redemption was not found.", 404)
        return Response(serialize_redemption(row))
