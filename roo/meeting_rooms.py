import hashlib
import uuid
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from core.models import User

from .models import (
    Ledger,
    MeetingRoom,
    MeetingRoomBlock,
    MeetingRoomBooking,
    PointsAccount,
)
from .services import PointsService


DEFAULT_ROOM_SLUG = 'meeting-room'
ROOM_LOCK_SCOPE = 'meeting-room'
USER_LOCK_SCOPE = 'meeting-room-user'


class MeetingRoomError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def serialize_room(room: MeetingRoom) -> dict:
    return {
        'id': str(room.id),
        'slug': room.slug,
        'name': room.name,
    }


class MeetingRoomService:
    """Transactional source of truth for Roo meeting-room reservations."""

    @staticmethod
    def _timezone() -> ZoneInfo:
        return ZoneInfo(settings.MEETING_ROOM_TIMEZONE)

    @staticmethod
    def _ensure_enabled() -> None:
        if not getattr(settings, 'MEETING_ROOM_BOOKING_ENABLED', False):
            raise MeetingRoomError(
                'feature_disabled',
                'Meeting-room booking is not enabled',
                503,
            )

    @staticmethod
    def _now() -> datetime:
        return timezone.now()

    @classmethod
    def _local_dates(cls, starts_at: datetime, ends_at: datetime) -> list[date]:
        room_tz = cls._timezone()
        first_date = starts_at.astimezone(room_tz).date()
        final_instant = ends_at - timedelta(microseconds=1)
        last_date = final_instant.astimezone(room_tz).date()
        dates = []
        cursor = first_date
        while cursor <= last_date:
            dates.append(cursor)
            cursor += timedelta(days=1)
        return dates

    @classmethod
    def _day_bounds(cls, local_date: date) -> tuple[datetime, datetime]:
        room_tz = cls._timezone()
        starts_at = datetime.combine(local_date, time.min, tzinfo=room_tz)
        ends_at = datetime.combine(
            local_date + timedelta(days=1),
            time.min,
            tzinfo=room_tz,
        )
        return starts_at, ends_at

    @staticmethod
    def _overlap_hours(
        starts_at: datetime,
        ends_at: datetime,
        window_start: datetime,
        window_end: datetime,
    ) -> float:
        overlap_start = max(starts_at, window_start)
        overlap_end = min(ends_at, window_end)
        if overlap_end <= overlap_start:
            return 0.0
        return (
            overlap_end.astimezone(datetime_timezone.utc)
            - overlap_start.astimezone(datetime_timezone.utc)
        ).total_seconds() / 3600

    @classmethod
    def validate_interval(
        cls,
        starts_at: datetime,
        ends_at: datetime,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[datetime, datetime, int]:
        if not starts_at or not ends_at:
            raise MeetingRoomError(
                'invalid_time',
                'starts_at and ends_at are required',
            )
        if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
            raise MeetingRoomError(
                'invalid_time',
                'Meeting-room timestamps must include a UTC offset',
            )

        room_tz = cls._timezone()
        local_start = starts_at.astimezone(room_tz)
        local_end = ends_at.astimezone(room_tz)
        for value in (local_start, local_end):
            if value.minute or value.second or value.microsecond:
                raise MeetingRoomError(
                    'invalid_time',
                    'Meeting-room bookings must start and end on the hour',
                )
        utc_start = starts_at.astimezone(datetime_timezone.utc)
        utc_end = ends_at.astimezone(datetime_timezone.utc)
        if utc_end <= utc_start:
            raise MeetingRoomError(
                'invalid_time',
                'Meeting-room end time must be after start time',
            )

        duration_seconds = (utc_end - utc_start).total_seconds()
        if duration_seconds % 3600:
            raise MeetingRoomError(
                'invalid_time',
                'Meeting-room duration must use whole hours',
            )
        duration_hours = int(duration_seconds // 3600)
        max_hours = getattr(settings, 'MEETING_ROOM_MAX_BOOKING_HOURS', 4)
        if duration_hours < 1 or duration_hours > max_hours:
            raise MeetingRoomError(
                'invalid_time',
                f'Meeting-room bookings must be between 1 and {max_hours} hours',
            )

        current_time = now or cls._now()
        if starts_at <= current_time:
            raise MeetingRoomError(
                'invalid_time',
                'Meeting-room bookings must start in the future',
            )
        advance_days = getattr(settings, 'MEETING_ROOM_BOOKING_ADVANCE_DAYS', 30)
        latest_date = current_time.astimezone(room_tz).date() + timedelta(
            days=advance_days
        )
        last_occupied_date = (ends_at - timedelta(microseconds=1)).astimezone(
            room_tz
        ).date()
        if local_start.date() > latest_date or last_occupied_date > latest_date:
            raise MeetingRoomError(
                'invalid_time',
                f'Meeting-room bookings can only be made {advance_days} days ahead',
            )
        return starts_at, ends_at, duration_hours

    @staticmethod
    def validate_client_request_id(value: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value or '').strip())
        except (ValueError, AttributeError, TypeError):
            raise MeetingRoomError(
                'invalid_request',
                'client_request_id must be a UUID',
            )

    @classmethod
    def validate_confirmation_expiry(cls, value: datetime) -> datetime:
        if not value or timezone.is_naive(value):
            raise MeetingRoomError(
                'invalid_request',
                'confirmation_expires_at must include a UTC offset',
            )
        if value <= cls._now():
            raise MeetingRoomError(
                'expired_confirmation',
                'This booking confirmation has expired',
                410,
            )
        return value

    @staticmethod
    def _advisory_key(scope: str, identifier: str, local_date: date) -> int:
        digest = hashlib.blake2b(
            f'{scope}:{identifier}:{local_date.isoformat()}'.encode('utf-8'),
            digest_size=8,
        ).digest()
        return int.from_bytes(digest, byteorder='big', signed=True)

    @staticmethod
    def _request_advisory_key(request_id: uuid.UUID) -> int:
        digest = hashlib.blake2b(
            f'meeting-room-request:{request_id}'.encode('utf-8'),
            digest_size=8,
        ).digest()
        return int.from_bytes(digest, byteorder='big', signed=True)

    @classmethod
    def _lock_booking_scope(
        cls,
        *,
        room: MeetingRoom,
        user: User,
        local_dates: Iterable[date],
        request_id: uuid.UUID,
    ) -> None:
        if connection.vendor != 'postgresql':
            return
        keys = {
            cls._advisory_key(ROOM_LOCK_SCOPE, str(room.id), local_date)
            for local_date in local_dates
        }
        keys.update(
            cls._advisory_key(USER_LOCK_SCOPE, str(user.pk), local_date)
            for local_date in local_dates
        )
        keys.add(cls._request_advisory_key(request_id))
        with connection.cursor() as cursor:
            for key in sorted(keys):
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])

    @classmethod
    def lock_room_interval(
        cls,
        *,
        room: MeetingRoom,
        starts_at: datetime,
        ends_at: datetime,
    ) -> None:
        """Serialize operations that can change availability for a room interval."""
        if connection.vendor != 'postgresql':
            return
        keys = {
            cls._advisory_key(ROOM_LOCK_SCOPE, str(room.id), local_date)
            for local_date in cls._local_dates(starts_at, ends_at)
        }
        with connection.cursor() as cursor:
            for key in sorted(keys):
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])

    @staticmethod
    def _get_room(room_slug: str, *, active_required: bool = True) -> MeetingRoom:
        try:
            room = MeetingRoom.objects.get(slug=room_slug)
        except MeetingRoom.DoesNotExist:
            raise MeetingRoomError('room_not_found', 'Meeting room not found', 404)
        if active_required and not room.is_active:
            raise MeetingRoomError(
                'inactive_room',
                'This meeting room is not accepting bookings',
                409,
            )
        return room

    @classmethod
    def list_rooms(cls) -> list[dict]:
        cls._ensure_enabled()
        return [serialize_room(room) for room in MeetingRoom.objects.filter(is_active=True)]

    @classmethod
    def _booked_hours_for_date(cls, user: User, local_date: date) -> float:
        day_start, day_end = cls._day_bounds(local_date)
        bookings = MeetingRoomBooking.objects.filter(
            user=user,
            status='booked',
            starts_at__lt=day_end,
            ends_at__gt=day_start,
        ).only('starts_at', 'ends_at')
        return sum(
            cls._overlap_hours(
                booking.starts_at,
                booking.ends_at,
                day_start,
                day_end,
            )
            for booking in bookings
        )

    @classmethod
    def _remaining_daily_hours(
        cls,
        user: User,
        local_dates: Iterable[date],
    ) -> dict[str, float]:
        daily_limit = getattr(settings, 'MEETING_ROOM_DAILY_MEMBER_HOURS', 4)
        return {
            local_date.isoformat(): max(
                0,
                daily_limit - cls._booked_hours_for_date(user, local_date),
            )
            for local_date in local_dates
        }

    @classmethod
    def _validate_daily_limit(
        cls,
        *,
        user: User,
        starts_at: datetime,
        ends_at: datetime,
    ) -> None:
        daily_limit = getattr(settings, 'MEETING_ROOM_DAILY_MEMBER_HOURS', 4)
        for local_date in cls._local_dates(starts_at, ends_at):
            day_start, day_end = cls._day_bounds(local_date)
            requested_hours = cls._overlap_hours(
                starts_at,
                ends_at,
                day_start,
                day_end,
            )
            booked_hours = cls._booked_hours_for_date(user, local_date)
            if booked_hours + requested_hours > daily_limit:
                raise MeetingRoomError(
                    'daily_limit',
                    f'Members can book at most {daily_limit} meeting-room hours per day',
                    409,
                )

    @staticmethod
    def _room_has_booking(
        room: MeetingRoom,
        starts_at: datetime,
        ends_at: datetime,
    ) -> bool:
        return MeetingRoomBooking.objects.filter(
            room=room,
            status='booked',
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        ).exists()

    @staticmethod
    def _room_has_block(
        room: MeetingRoom,
        starts_at: datetime,
        ends_at: datetime,
    ) -> bool:
        return MeetingRoomBlock.objects.filter(
            room=room,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        ).exists()

    @classmethod
    def _busy_intervals(
        cls,
        room: MeetingRoom,
        starts_at: datetime,
        ends_at: datetime,
    ) -> list[dict]:
        intervals = list(
            MeetingRoomBooking.objects.filter(
                room=room,
                status='booked',
                starts_at__lt=ends_at,
                ends_at__gt=starts_at,
            ).values_list('starts_at', 'ends_at')
        )
        intervals.extend(
            MeetingRoomBlock.objects.filter(
                room=room,
                starts_at__lt=ends_at,
                ends_at__gt=starts_at,
            ).values_list('starts_at', 'ends_at')
        )
        intervals.sort(key=lambda interval: (interval[0], interval[1]))
        merged: list[list[datetime]] = []
        for interval_start, interval_end in intervals:
            clipped_start = max(interval_start, starts_at)
            clipped_end = min(interval_end, ends_at)
            if merged and clipped_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], clipped_end)
            else:
                merged.append([clipped_start, clipped_end])
        room_tz = cls._timezone()
        return [
            {
                'starts_at': interval_start.astimezone(room_tz).isoformat(),
                'ends_at': interval_end.astimezone(room_tz).isoformat(),
            }
            for interval_start, interval_end in merged
        ]

    @classmethod
    def availability(
        cls,
        *,
        user: User,
        room_slug: str = DEFAULT_ROOM_SLUG,
        local_date: Optional[date] = None,
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = None,
    ) -> dict:
        cls._ensure_enabled()
        room = cls._get_room(room_slug)
        requested_interval = None
        available = None
        unavailable_reasons: list[str] = []
        points_cost = None

        if starts_at is not None or ends_at is not None:
            starts_at, ends_at, points_cost = cls.validate_interval(starts_at, ends_at)
            range_start, range_end = starts_at, ends_at
            local_dates = cls._local_dates(starts_at, ends_at)
            if cls._room_has_booking(room, starts_at, ends_at):
                unavailable_reasons.append('booking_conflict')
            if cls._room_has_block(room, starts_at, ends_at):
                unavailable_reasons.append('room_blocked')
            room_tz = cls._timezone()
            requested_interval = {
                'starts_at': starts_at.astimezone(room_tz).isoformat(),
                'ends_at': ends_at.astimezone(room_tz).isoformat(),
            }
        else:
            if local_date is None:
                raise MeetingRoomError(
                    'invalid_request',
                    'Provide a date or an exact start and end time',
                )
            today = cls._now().astimezone(cls._timezone()).date()
            advance_days = getattr(settings, 'MEETING_ROOM_BOOKING_ADVANCE_DAYS', 30)
            if local_date < today or local_date > today + timedelta(days=advance_days):
                raise MeetingRoomError(
                    'invalid_time',
                    f'Availability can only be checked from today to {advance_days} days ahead',
                )
            range_start, range_end = cls._day_bounds(local_date)
            local_dates = [local_date]

        remaining_daily_hours = cls._remaining_daily_hours(user, local_dates)
        if requested_interval:
            for local_date in local_dates:
                day_start, day_end = cls._day_bounds(local_date)
                requested_hours = cls._overlap_hours(
                    starts_at,
                    ends_at,
                    day_start,
                    day_end,
                )
                if requested_hours > remaining_daily_hours[local_date.isoformat()]:
                    unavailable_reasons.append('daily_limit')
                    break
            if cls.current_balance(user) < points_cost:
                unavailable_reasons.append('insufficient_balance')
            available = not unavailable_reasons

        return {
            'timezone': str(cls._timezone()),
            'room': serialize_room(room),
            'requested_interval': requested_interval,
            'available': available,
            'unavailable_reasons': unavailable_reasons,
            'points_cost': points_cost,
            'remaining_daily_hours': remaining_daily_hours,
            'busy_intervals': cls._busy_intervals(room, range_start, range_end),
        }

    @classmethod
    def _assert_idempotent_payload(
        cls,
        booking: MeetingRoomBooking,
        *,
        user: User,
        room: MeetingRoom,
        starts_at: datetime,
        ends_at: datetime,
    ) -> None:
        if (
            booking.user_id != user.pk
            or booking.room_id != room.pk
            or booking.starts_at != starts_at
            or booking.ends_at != ends_at
        ):
            raise MeetingRoomError(
                'payload_conflict',
                'client_request_id was already used for a different booking',
                409,
            )

    @staticmethod
    def _assert_active_replay(booking: MeetingRoomBooking) -> None:
        if booking.status == 'cancelled':
            raise MeetingRoomError(
                'booking_cancelled',
                'This booking confirmation belongs to a cancelled booking',
                409,
            )

    @classmethod
    @transaction.atomic
    def book(
        cls,
        *,
        user: User,
        requested_by_slack_id: str,
        room_slug: str,
        starts_at: datetime,
        ends_at: datetime,
        client_request_id: str,
        confirmation_expires_at: datetime,
        slack_channel_id: Optional[str] = None,
    ) -> tuple[MeetingRoomBooking, bool]:
        cls._ensure_enabled()
        request_id = cls.validate_client_request_id(client_request_id)

        existing = MeetingRoomBooking.objects.select_for_update().filter(
            client_request_id=request_id
        ).first()
        if existing:
            room = cls._get_room(room_slug, active_required=False)
            cls._assert_idempotent_payload(
                existing,
                user=user,
                room=room,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            cls._assert_active_replay(existing)
            return existing, False

        room = cls._get_room(room_slug)
        starts_at, ends_at, points_cost = cls.validate_interval(starts_at, ends_at)
        cls.validate_confirmation_expiry(confirmation_expires_at)
        local_dates = cls._local_dates(starts_at, ends_at)
        cls._lock_booking_scope(
            room=room,
            user=user,
            local_dates=local_dates,
            request_id=request_id,
        )

        room = MeetingRoom.objects.select_for_update().get(pk=room.pk)
        if not room.is_active:
            raise MeetingRoomError(
                'inactive_room',
                'This meeting room is not accepting bookings',
                409,
            )

        existing = MeetingRoomBooking.objects.select_for_update().filter(
            client_request_id=request_id
        ).first()
        if existing:
            cls._assert_idempotent_payload(
                existing,
                user=user,
                room=room,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            cls._assert_active_replay(existing)
            return existing, False

        if cls._room_has_block(room, starts_at, ends_at):
            raise MeetingRoomError(
                'room_blocked',
                'The meeting room is unavailable during that time',
                409,
            )
        if cls._room_has_booking(room, starts_at, ends_at):
            raise MeetingRoomError(
                'booking_conflict',
                'The meeting room has already been booked during that time',
                409,
            )
        cls._validate_daily_limit(
            user=user,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        try:
            with transaction.atomic():
                account = PointsAccount.objects.select_for_update().filter(
                    user=user
                ).first()
                purchased_points_cost = min(
                    account.purchased_topup_balance if account else 0,
                    points_cost,
                )
                ledger, _ = PointsService.spend(
                    user=user,
                    delta=points_cost,
                    source='MEETING_ROOM',
                    description=(
                        f'Meeting Room booking from {starts_at.isoformat()} '
                        f'to {ends_at.isoformat()}'
                    ),
                    created_by_slack_id=requested_by_slack_id,
                    idempotency_key=f'meeting_room_book:{user.pk}:{request_id}',
                    reference_type='MEETING_ROOM_BOOKING',
                    reference_id=str(request_id),
                )
                booking = MeetingRoomBooking.objects.create(
                    room=room,
                    user=user,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status='booked',
                    points_cost=points_cost,
                    purchased_points_cost=purchased_points_cost,
                    client_request_id=request_id,
                    ledger_entry=ledger,
                    requested_by_slack_id=requested_by_slack_id,
                    slack_channel_id=slack_channel_id,
                )
        except IntegrityError:
            existing = MeetingRoomBooking.objects.select_for_update().filter(
                client_request_id=request_id
            ).first()
            if existing is None:
                raise
            cls._assert_idempotent_payload(
                existing,
                user=user,
                room=room,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            cls._assert_active_replay(existing)
            return existing, False
        return booking, True

    @classmethod
    def my_bookings(cls, *, user: User) -> list[MeetingRoomBooking]:
        return list(
            MeetingRoomBooking.objects.filter(
                user=user,
                status='booked',
                ends_at__gt=cls._now(),
            )
            .select_related('room')
            .order_by('starts_at', 'id')
        )

    @classmethod
    @transaction.atomic
    def cancel(
        cls,
        *,
        user: User,
        booking_id: str,
        requested_by_slack_id: str,
    ) -> tuple[MeetingRoomBooking, bool, bool]:
        try:
            booking = MeetingRoomBooking.objects.select_for_update().select_related(
                'room'
            ).get(pk=booking_id)
        except (MeetingRoomBooking.DoesNotExist, ValidationError, ValueError):
            raise MeetingRoomError(
                'booking_not_found',
                'Meeting-room booking not found',
                404,
            )
        if booking.user_id != user.pk:
            raise MeetingRoomError(
                'not_booking_owner',
                'Members can only cancel their own meeting-room bookings',
                403,
            )
        if booking.status == 'cancelled' and booking.refund_ledger_entry_id:
            return booking, False, True
        if booking.status == 'booked' and cls._now() >= booking.starts_at:
            raise MeetingRoomError(
                'booking_started',
                'Meeting-room bookings cannot be cancelled after they start',
                409,
            )

        charge_ledger = booking.ledger_entry
        if charge_ledger is None:
            charge_ledger = Ledger.objects.filter(
                idempotency_key=(
                    f'meeting_room_book:{booking.user_id}:'
                    f'{booking.client_request_id}'
                )
            ).first()
        if charge_ledger is None:
            raise MeetingRoomError(
                'refund_unavailable',
                'The original meeting-room charge could not be verified',
                409,
            )

        purchased_points_cost = min(
            booking.purchased_points_cost or 0,
            booking.points_cost,
        )
        ledger, refund_created = PointsService.refund(
            user=user,
            delta=booking.points_cost,
            source='MEETING_ROOM',
            description=f'Refund for cancelled Meeting Room booking {booking.id}',
            created_by_slack_id=requested_by_slack_id,
            idempotency_key=f'meeting_room_refund:{booking.id}',
            reference_type='MEETING_ROOM_REFUND',
            reference_id=str(booking.id),
            purchased_delta=purchased_points_cost,
            reverse_lifetime_spent=True,
        )
        was_booked = booking.status == 'booked'
        booking.status = 'cancelled'
        booking.refund_ledger_entry = ledger
        booking.cancelled_at = booking.cancelled_at or cls._now()
        booking.save(
            update_fields=[
                'status',
                'refund_ledger_entry',
                'cancelled_at',
            ]
        )
        return booking, was_booked or refund_created, True

    @classmethod
    def serialize_booking(cls, booking: MeetingRoomBooking) -> dict:
        room_tz = cls._timezone()
        return {
            'id': str(booking.id),
            'room': serialize_room(booking.room),
            'starts_at': booking.starts_at.astimezone(room_tz).isoformat(),
            'ends_at': booking.ends_at.astimezone(room_tz).isoformat(),
            'timezone': str(room_tz),
            'status': booking.status,
            'points_cost': booking.points_cost,
            'created_at': booking.created_at.isoformat(),
            'cancelled_at': (
                booking.cancelled_at.isoformat()
                if booking.cancelled_at
                else None
            ),
        }

    @staticmethod
    def current_balance(user: User) -> int:
        account = PointsAccount.objects.filter(user=user).only('balance').first()
        return account.balance if account else 0
