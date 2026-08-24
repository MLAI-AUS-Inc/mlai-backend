from datetime import date

from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasStrictRooApiKey

from .meeting_rooms import MeetingRoomError, MeetingRoomService
from .permissions import InsufficientBalanceError, is_points_admin
from .services import PointsService
from .views import clean_slack_id


def _error_response(exc: MeetingRoomError) -> Response:
    return Response(
        {'code': exc.code, 'error': exc.message},
        status=exc.status_code,
    )


def _parse_timestamp(value, field_name: str):
    try:
        parsed = parse_datetime(str(value or '').strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise MeetingRoomError(
            'invalid_time',
            f'{field_name} must be a valid timezone-aware ISO 8601 timestamp',
        )
    return parsed


def _parse_local_date(value) -> date:
    try:
        parsed = parse_date(str(value or '').strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise MeetingRoomError(
            'invalid_time',
            'date must use YYYY-MM-DD format',
        )
    return parsed


def _active_user_for_slack_id(slack_user_id: str, *, target: bool = False):
    if not slack_user_id:
        raise MeetingRoomError(
            'unlinked_user',
            (
                'The tagged member does not have a linked MLAI account'
                if target
                else 'A linked Slack member account is required'
            ),
            status.HTTP_404_NOT_FOUND,
        )
    user = PointsService.get_user_by_slack_id(slack_user_id)
    if user is None:
        raise MeetingRoomError(
            'unlinked_user',
            'A linked Slack member account is required',
            status.HTTP_404_NOT_FOUND,
        )
    if not user.is_active:
        raise MeetingRoomError(
            'inactive_user',
            (
                'The tagged MLAI member account is inactive'
                if target
                else 'This MLAI member account is inactive'
            ),
            status.HTTP_403_FORBIDDEN,
        )
    return user


def _request_user(request):
    slack_user_id = clean_slack_id(request.data.get('slack_user_id'))
    return _active_user_for_slack_id(slack_user_id), slack_user_id


def _booking_user(request):
    requester_slack_user_id = clean_slack_id(request.data.get('slack_user_id'))
    target_slack_user_id = clean_slack_id(
        request.data.get('target_slack_user_id')
    )
    if target_slack_user_id and target_slack_user_id != requester_slack_user_id:
        if not is_points_admin(requester_slack_user_id):
            raise MeetingRoomError(
                'admin_required',
                'Only full Roo Points Admins can book the meeting room for another member',
                status.HTTP_403_FORBIDDEN,
            )
        return (
            _active_user_for_slack_id(target_slack_user_id, target=True),
            requester_slack_user_id,
            target_slack_user_id,
            True,
        )

    user = _active_user_for_slack_id(requester_slack_user_id)
    return user, requester_slack_user_id, requester_slack_user_id, False


def _optional_points_cost(value):
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        raise MeetingRoomError(
            'invalid_request',
            'expected_points_cost must be a whole number',
        )
    try:
        parsed = int(value)
        if float(value) != parsed:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise MeetingRoomError(
            'invalid_request',
            'expected_points_cost must be a whole number',
        )
    if parsed < 0:
        raise MeetingRoomError(
            'invalid_request',
            'expected_points_cost must not be negative',
        )
    return parsed


def _required_room_slug(request) -> str:
    value = request.data.get('room_slug')
    if not isinstance(value, str) or not value.strip():
        raise MeetingRoomError(
            'invalid_request',
            'room_slug is required',
        )
    return value.strip()


class MeetingRoomAPIView(APIView):
    permission_classes = [HasStrictRooApiKey]

    def handle_exception(self, exc):
        if isinstance(exc, MeetingRoomError):
            return _error_response(exc)
        if isinstance(exc, InsufficientBalanceError):
            return Response(
                {'code': 'insufficient_balance', 'error': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return super().handle_exception(exc)


class MeetingRoomListView(MeetingRoomAPIView):
    def get(self, request):
        return Response({'rooms': MeetingRoomService.list_rooms()})


class MeetingRoomAvailabilityView(MeetingRoomAPIView):
    def post(self, request):
        user, _, _, admin_booking = _booking_user(request)
        room_slug = _required_room_slug(request)
        raw_date = request.data.get('date')
        raw_start = request.data.get('starts_at')
        raw_end = request.data.get('ends_at')

        if raw_date and (raw_start or raw_end):
            raise MeetingRoomError(
                'invalid_request',
                'Provide either date or starts_at and ends_at, not both',
            )
        if raw_start or raw_end:
            if not raw_start or not raw_end:
                raise MeetingRoomError(
                    'invalid_request',
                    'Both starts_at and ends_at are required for an exact interval',
                )
            result = MeetingRoomService.availability(
                user=user,
                room_slug=room_slug,
                starts_at=_parse_timestamp(raw_start, 'starts_at'),
                ends_at=_parse_timestamp(raw_end, 'ends_at'),
            )
        elif raw_date:
            result = MeetingRoomService.availability(
                user=user,
                room_slug=room_slug,
                local_date=_parse_local_date(raw_date),
            )
        else:
            raise MeetingRoomError(
                'invalid_request',
                'Provide a date or an exact start and end time',
            )
        return Response({**result, 'admin_booking': admin_booking})


class MeetingRoomBookView(MeetingRoomAPIView):
    def post(self, request):
        if request.data.get('target_user_id'):
            raise MeetingRoomError(
                'unsupported_target',
                'Use target_slack_user_id when booking for another member',
            )
        user, requester_slack_user_id, target_slack_user_id, admin_booking = (
            _booking_user(request)
        )
        booking, created = MeetingRoomService.book(
            user=user,
            requested_by_slack_id=requester_slack_user_id,
            room_slug=_required_room_slug(request),
            starts_at=_parse_timestamp(request.data.get('starts_at'), 'starts_at'),
            ends_at=_parse_timestamp(request.data.get('ends_at'), 'ends_at'),
            client_request_id=request.data.get('client_request_id'),
            confirmation_expires_at=_parse_timestamp(
                request.data.get('confirmation_expires_at'),
                'confirmation_expires_at',
            ),
            slack_channel_id=str(
                request.data.get('slack_channel_id') or ''
            ).strip() or None,
            expected_points_cost=_optional_points_cost(
                request.data.get('expected_points_cost')
            ),
        )
        return Response(
            {
                'created': created,
                'already_booked': not created,
                'booking': MeetingRoomService.serialize_booking(booking),
                'points_cost': booking.points_cost,
                'remaining_balance': MeetingRoomService.current_balance(user),
                'admin_booking': admin_booking,
                'booked_for_slack_user_id': target_slack_user_id,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class MyMeetingRoomBookingsView(MeetingRoomAPIView):
    def post(self, request):
        user, _ = _request_user(request)
        bookings = MeetingRoomService.my_bookings(user=user)
        return Response(
            {
                'bookings': [
                    MeetingRoomService.serialize_booking(booking)
                    for booking in bookings
                ],
            }
        )


class MeetingRoomCancelView(MeetingRoomAPIView):
    def post(self, request):
        user, slack_user_id = _request_user(request)
        booking, cancelled, refunded = MeetingRoomService.cancel(
            user=user,
            booking_id=str(request.data.get('booking_id') or '').strip(),
            requested_by_slack_id=slack_user_id,
        )
        return Response(
            {
                'cancelled': cancelled,
                'already_cancelled': not cancelled,
                'refunded': refunded,
                'booking': MeetingRoomService.serialize_booking(booking),
                'remaining_balance': MeetingRoomService.current_balance(user),
            }
        )
