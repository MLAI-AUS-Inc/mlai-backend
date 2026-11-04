"""Account-authenticated meeting-room API for MLAI Chat clients.

The Roo meeting-room service remains the authority for availability, points and
reservations.  This facade binds every request to the signed-in Chat account so
the Roo service credential is never exposed to a browser or desktop client.
"""

from datetime import date

from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.meeting_rooms import MeetingRoomError, MeetingRoomService
from roo.permissions import InsufficientBalanceError

from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class MeetingRoomAPIView(APIView):
    """Expose the Roo meeting-room contract to the authenticated account."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, MeetingRoomError):
            return Response(
                {"code": exc.code, "error": exc.message, "detail": exc.message},
                status=exc.status_code,
            )
        if isinstance(exc, InsufficientBalanceError):
            return Response(
                {
                    "code": "insufficient_balance",
                    "error": str(exc),
                    "detail": str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return super().handle_exception(exc)


def _room_slug(request) -> str:
    value = request.data.get("room_slug")
    if not isinstance(value, str) or not value.strip():
        raise MeetingRoomError("invalid_request", "room_slug is required")
    return value.strip()


def _timestamp(request, field_name: str):
    try:
        parsed = parse_datetime(str(request.data.get(field_name) or "").strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise MeetingRoomError(
            "invalid_time",
            f"{field_name} must be a valid timezone-aware ISO 8601 timestamp",
        )
    return parsed


def _local_date(request) -> date:
    try:
        parsed = parse_date(str(request.data.get("date") or "").strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise MeetingRoomError("invalid_time", "date must use YYYY-MM-DD format")
    return parsed


def _expected_points_cost(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MeetingRoomError(
            "invalid_request", "expected_points_cost must be a whole number"
        )
    try:
        parsed = int(value)
        if float(value) != parsed:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise MeetingRoomError(
            "invalid_request", "expected_points_cost must be a whole number"
        )
    if parsed < 0:
        raise MeetingRoomError(
            "invalid_request", "expected_points_cost must not be negative"
        )
    return parsed


class MeetingRoomListView(MeetingRoomAPIView):
    def get(self, request):
        return Response({"rooms": MeetingRoomService.list_rooms()})


class MeetingRoomAvailabilityView(MeetingRoomAPIView):
    def post(self, request):
        room_slug = _room_slug(request)
        has_date = bool(request.data.get("date"))
        has_start = bool(request.data.get("starts_at"))
        has_end = bool(request.data.get("ends_at"))
        if has_date and (has_start or has_end):
            raise MeetingRoomError(
                "invalid_request", "Provide either date or starts_at and ends_at, not both"
            )
        if has_start or has_end:
            if not has_start or not has_end:
                raise MeetingRoomError(
                    "invalid_request", "Both starts_at and ends_at are required for an exact interval"
                )
            result = MeetingRoomService.availability(
                user=request.user,
                room_slug=room_slug,
                starts_at=_timestamp(request, "starts_at"),
                ends_at=_timestamp(request, "ends_at"),
            )
        elif has_date:
            result = MeetingRoomService.availability(
                user=request.user, room_slug=room_slug, local_date=_local_date(request)
            )
        else:
            raise MeetingRoomError(
                "invalid_request", "Provide a date or an exact start and end time"
            )
        return Response(result)


class MeetingRoomBookView(MeetingRoomAPIView):
    def post(self, request):
        booking, created = MeetingRoomService.book(
            user=request.user,
            requested_by_slack_id=request.user.slack_id or "",
            room_slug=_room_slug(request),
            starts_at=_timestamp(request, "starts_at"),
            ends_at=_timestamp(request, "ends_at"),
            client_request_id=request.data.get("client_request_id"),
            confirmation_expires_at=_timestamp(request, "confirmation_expires_at"),
            expected_points_cost=_expected_points_cost(request.data.get("expected_points_cost")),
        )
        return Response(
            {
                "created": created,
                "already_booked": not created,
                "booking": MeetingRoomService.serialize_booking(booking),
                "points_cost": booking.points_cost,
                "remaining_balance": MeetingRoomService.current_balance(request.user),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class MyMeetingRoomBookingsView(MeetingRoomAPIView):
    def get(self, request):
        return Response(
            {
                "bookings": [
                    MeetingRoomService.serialize_booking(booking)
                    for booking in MeetingRoomService.my_bookings(user=request.user)
                ]
            }
        )


class MeetingRoomCancelView(MeetingRoomAPIView):
    def post(self, request):
        booking, cancelled, refunded = MeetingRoomService.cancel(
            user=request.user,
            booking_id=str(request.data.get("booking_id") or "").strip(),
            requested_by_slack_id=request.user.slack_id or "",
        )
        return Response(
            {
                "cancelled": cancelled,
                "already_cancelled": not cancelled,
                "refunded": refunded,
                "booking": MeetingRoomService.serialize_booking(booking),
                "remaining_balance": MeetingRoomService.current_balance(request.user),
            }
        )
