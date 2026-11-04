"""First-party member-perk quotes and explicit, idempotent points redemption."""

import re
import uuid

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.services.luma_event_discounts import (
    LumaEventDiscountError,
    LumaEventDiscountService,
)
from roo.event_discounts import (
    EventDiscountError,
    EventDiscountService,
    _available_offer,
    account_perks_enabled,
    member_perk_terms,
    quote_payload,
    serialize_redemption,
)
from roo.models import EventDiscountOffer, EventDiscountRedemption
from roo.services import PointsService

from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class MemberPerksView(APIView):
    """Only the authenticated account can quote, confirm or read its event perk."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, (EventDiscountError, LumaEventDiscountError)):
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=exc.status_code
                if isinstance(exc, EventDiscountError)
                else (503 if exc.retryable else 409),
            )
        return super().handle_exception(exc)

    def _event(self, event_id):
        if not re.fullmatch(r"evt-[A-Za-z0-9_-]{1,96}", event_id):
            raise EventDiscountError("invalid_event", "Choose a valid event.", 400)

    def _unavailable(self, event_id, code, message):
        return Response({
            "event_id": event_id,
            "enabled": True,
            "options": [],
            "reason_code": code,
            "message": message,
        })

    def get(self, request, event_id):
        self._event(event_id)
        if request.query_params.get("redemption_id"):
            try:
                redemption_id = uuid.UUID(request.query_params["redemption_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise EventDiscountError(
                    "invalid_redemption", "This member perk was not found.", 400
                ) from exc
            row = (
                EventDiscountRedemption.objects.select_related("quote", "user")
                .filter(pk=redemption_id, user=request.user, event_id=event_id)
                .first()
            )
            if row is None:
                raise EventDiscountError(
                    "redemption_not_found", "This member perk was not found.", 404
                )
            return Response(
                {
                    "event_id": event_id,
                    "enabled": account_perks_enabled(),
                    "options": [],
                    "redemption": serialize_redemption(row),
                }
            )
        row = (
            EventDiscountRedemption.objects.select_related("quote", "user")
            .filter(
                user=request.user,
                event_id=event_id,
                status__in=("processing", "fulfilled"),
            )
            .first()
        )
        if row:
            return Response(
                {
                    "event_id": event_id,
                    "enabled": account_perks_enabled(),
                    "options": [],
                    "redemption": serialize_redemption(row),
                }
            )
        if not account_perks_enabled():
            return Response(
                {
                    "event_id": event_id,
                    "enabled": False,
                    "options": [],
                    "message": "Member perks are not available yet.",
                }
            )
        offer = (
            EventDiscountOffer.objects.select_related("reward")
            .filter(event_id=event_id)
            .first()
        )
        if offer is None:
            return self._unavailable(
                event_id,
                "offer_unavailable",
                "No member perk is available for this event. "
                "Perks cover selected paid AUD tickets priced below A$100.",
            )
        try:
            _available_offer(offer, timezone.now())
            terms, _ = member_perk_terms(offer, "free_ticket")
            snapshot = LumaEventDiscountService().validate_offer(terms, now=timezone.now())
        except EventDiscountError as exc:
            if exc.code != "offer_unavailable":
                raise
            return self._unavailable(
                event_id, exc.code,
                "No member perk is currently available for this event. "
                "Perks cover selected paid AUD tickets priced below A$100.",
            )
        except LumaEventDiscountError as exc:
            if exc.retryable or exc.code not in {
                "event_not_managed", "event_registration_closed",
                "group_registration_enabled", "event_offer_expired",
                "event_sold_out", "ticket_unavailable", "ticket_not_paid_aud",
                "ticket_price_limit", "ticket_not_supported", "ticket_sales_closed",
                "ticket_sold_out",
            }:
                raise
            return self._unavailable(event_id, exc.code, str(exc))
        balance = PointsService.microroo_to_legacy_whole(
            PointsService.get_available_microroo(request.user)
        )
        options = []
        for perk, label in (
            ("half_price", "Half-price ticket"),
            ("free_ticket", "Free ticket"),
        ):
            _, cost = member_perk_terms(offer, perk)
            options.append(
                {
                    "id": perk,
                    "label": label,
                    "points_cost": cost,
                    "percent_off": 50 if perk == "half_price" else 100,
                    "can_afford": balance >= cost,
                }
            )
        return Response(
            {
                "enabled": True,
                "event_id": event_id,
                "eligible_balance": balance,
                "ticket_price_cents": snapshot["ticket"]["cents"],
                "currency": "AUD",
                "options": options,
            }
        )

    def post(self, request, event_id):
        self._event(event_id)
        if not isinstance(request.data, dict):
            raise EventDiscountError("invalid_request", "Choose a member perk.", 400)
        if request.data.get("action") == "quote":
            if set(request.data) - {"action", "perk"}:
                raise EventDiscountError(
                    "invalid_request",
                    "Member and prices are determined by your account.",
                    400,
                )
            if request.data.get("perk") not in ("half_price", "free_ticket"):
                raise EventDiscountError(
                    "invalid_perk", "Choose a half-price or free ticket.", 400
                )
            quote = EventDiscountService.quote(
                user=request.user,
                event_query=event_id,
                account_perk=request.data.get("perk"),
            )
            return Response(
                {**quote_payload(quote), "perk": quote.snapshot["member_perk"]},
                status=201,
            )
        if request.data.get("action") != "redeem" or set(request.data) - {
            "action",
            "quote_id",
            "request_id",
            "confirmed",
        }:
            raise EventDiscountError("invalid_request", "Choose a member perk.", 400)
        if request.data.get("confirmed") is not True:
            raise EventDiscountError(
                "confirmation_required",
                "Confirm the displayed points cost before redeeming.",
                400,
            )
        from roo.models import EventDiscountQuote

        try:
            quote_id = uuid.UUID(str(request.data.get("quote_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise EventDiscountError(
                "invalid_quote", "Choose your member perk again.", 400
            ) from exc
        if not EventDiscountQuote.objects.filter(
            pk=quote_id,
            user=request.user,
            snapshot__id=event_id,
            snapshot__member_perk__in=("half_price", "free_ticket"),
        ).exists():
            raise EventDiscountError(
                "quote_not_found",
                "This event quote was not found. Choose your perk again.",
                404,
            )
        row = EventDiscountService.reserve(
            user=request.user,
            quote_id=request.data.get("quote_id"),
            request_id=request.data.get("request_id"),
            account_authenticated=True,
        )
        row = EventDiscountService.process(row.pk)
        return Response(
            serialize_redemption(row), status=202 if row.status == "processing" else 200
        )
