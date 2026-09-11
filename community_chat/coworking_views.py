"""Self-service coworking for the authenticated MLAI Chat member."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.models import CoworkingBooking
from roo.permissions import InsufficientBalanceError
from roo.services import CoworkingService

from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


MELBOURNE = ZoneInfo("Australia/Melbourne")


class CoworkingTodayView(APIView):
    """Read or book today's desk using the caller's existing points account.

    The Roo service owns capacity, discounts, the atomic points debit,
    and duplicate user/date protection. No client-supplied identity is used.
    """

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    @staticmethod
    def _receipt(day, booking, points_cost):
        return {
            "date": day.isoformat(),
            "status": "booked" if booking else "available",
            "booking_id": str(booking.pk) if booking else None,
            "points_cost": points_cost,
            "resets_at": datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=MELBOURNE
            ).isoformat(),
        }

    def get(self, request):
        day = timezone.localdate(timezone=MELBOURNE)
        booking = CoworkingBooking.objects.filter(
            user=request.user, date=day, status="booked"
        ).first()
        cost = (
            booking.points_cost
            if booking
            else CoworkingService.get_coworking_cost(user=request.user, booking_date=day)
        )
        return Response(self._receipt(day, booking, cost))

    def post(self, request):
        day = timezone.localdate(timezone=MELBOURNE)
        # A timeout/retry at midnight cannot silently charge for another day.
        if (
            not isinstance(request.data, dict)
            or request.data.get("date") != day.isoformat()
        ):
            return Response(
                {
                    "code": "booking_date_changed",
                    "detail": "A new day has started. Please try again.",
                },
                status=409,
            )
        try:
            booking, created = CoworkingService.book(
                user=request.user,
                booking_date=day,
                created_by_slack_id=request.user.slack_id or "",
            )
        except InsufficientBalanceError:
            return Response(
                {
                    "code": "insufficient_points",
                    "detail": "You don’t have enough Roo Points to book today.",
                },
                status=409,
            )
        except ValueError as error:
            return Response(
                {"code": "booking_unavailable", "detail": str(error)}, status=409
            )
        return Response(
            self._receipt(day, booking, booking.points_cost),
            status=201 if created else 200,
        )
