import json
import threading
import uuid
from datetime import datetime, time, timedelta
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from mlai.settings import _validated_timezone_name

from .meeting_rooms import MeetingRoomError, MeetingRoomService
from .models import (
    Ledger,
    MeetingRoom,
    MeetingRoomBlock,
    MeetingRoomBooking,
    PointsAccount,
    PointsAdmin,
)


User = get_user_model()
MELBOURNE = ZoneInfo('Australia/Melbourne')
TEST_SETTINGS = {
    'MEETING_ROOM_BOOKING_ENABLED': True,
    'ROO_API_KEY': 'roo-meeting-room-test-key',
    'INTERNAL_API_KEY': '',
}


def future_local(day_offset=1, hour=9):
    local_day = timezone.now().astimezone(MELBOURNE).date() + timedelta(
        days=day_offset
    )
    return datetime.combine(local_day, time(hour=hour), tzinfo=MELBOURNE)


@override_settings(**TEST_SETTINGS)
class MeetingRoomApiTests(APITestCase):
    def setUp(self):
        self.client.credentials(HTTP_X_API_KEY=TEST_SETTINGS['ROO_API_KEY'])
        self.room, _ = MeetingRoom.objects.get_or_create(
            slug='meeting-room',
            defaults={'name': 'Meeting Room'},
        )
        self.user = self.create_member('UROOMMEMBER', balance=40)

    def create_member(self, slack_id, *, balance=10, active=True):
        user = User.objects.create_user(
            email=f'{slack_id.lower()}@example.com',
            slack_id=slack_id,
        )
        if not active:
            user.is_active = False
            user.save(update_fields=['is_active'])
        PointsAccount.objects.create(
            user=user,
            balance=balance,
            earned_balance=balance,
            lifetime_earned=balance,
        )
        return user

    def book_payload(self, starts_at, duration=1, **overrides):
        payload = {
            'slack_user_id': self.user.slack_id,
            'room_slug': self.room.slug,
            'starts_at': starts_at.isoformat(),
            'ends_at': (starts_at + timedelta(hours=duration)).isoformat(),
            'client_request_id': str(uuid.uuid4()),
            'confirmation_expires_at': (
                timezone.now() + timedelta(minutes=10)
            ).isoformat(),
            'slack_channel_id': 'DROOM',
        }
        payload.update(overrides)
        return payload

    def book(self, starts_at, duration=1, **overrides):
        return self.client.post(
            reverse('meeting-room-book'),
            self.book_payload(starts_at, duration, **overrides),
            format='json',
        )

    def test_seeded_room_is_listed_without_private_data(self):
        response = self.client.get(reverse('meeting-room-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['rooms'][0]['slug'], 'meeting-room')
        self.assertEqual(set(response.data['rooms'][0]), {'id', 'slug', 'name'})

    def test_half_hour_increments_charge_each_started_hour(self):
        cases = ((1, 1), (1.5, 2), (2, 2))
        for day_offset, (duration, expected_cost) in enumerate(cases, start=1):
            with self.subTest(duration=duration):
                starts_at = future_local(day_offset, 9).replace(
                    minute=30 if duration == 1.5 else 0
                )
                response = self.book(starts_at, duration)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data['points_cost'], expected_cost)

        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 35)
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            3,
        )

    def test_invalid_intervals_are_rejected(self):
        valid_start = future_local(1, 9)
        cases = (
            ('quarter-hour start', valid_start.replace(minute=15), 1),
            ('less than one hour', valid_start, 0.5),
            ('quarter-hour duration', valid_start, 1.25),
            ('zero duration', valid_start, 0),
            ('more than two hours', valid_start, 2.5),
            ('past', future_local(-1, 9), 1),
            ('more than thirty days', future_local(31, 9), 1),
        )
        for label, starts_at, duration in cases:
            with self.subTest(case=label):
                response = self.book(starts_at, duration)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data['code'], 'invalid_time')

        self.assertFalse(MeetingRoomBooking.objects.exists())

    def test_invalid_calendar_values_return_invalid_time(self):
        availability = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'date': '2026-02-30',
            },
            format='json',
        )
        payload = self.book_payload(future_local(1, 9))
        payload['starts_at'] = '2026-02-30T09:00:00+11:00'
        invalid_booking = self.client.post(
            reverse('meeting-room-book'),
            payload,
            format='json',
        )

        for response in (availability, invalid_booking):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data['code'], 'invalid_time')

    def test_booking_cannot_spill_past_advance_window(self):
        now = datetime(2026, 8, 21, 9, tzinfo=MELBOURNE)
        starts_at = datetime(2026, 9, 20, 23, tzinfo=MELBOURNE)

        with self.assertRaises(MeetingRoomError) as raised:
            MeetingRoomService.validate_interval(
                starts_at,
                starts_at + timedelta(hours=2),
                now=now,
            )

        self.assertEqual(raised.exception.code, 'invalid_time')

    def test_invalid_meeting_room_timezone_is_rejected_during_configuration(self):
        with self.assertRaises(ImproperlyConfigured):
            _validated_timezone_name('Mars/Olympus_Mons', 'MEETING_ROOM_TIMEZONE')

    def test_cross_midnight_cost_and_daily_allowance_are_split(self):
        starts_at = future_local(1, 23).replace(minute=30)
        response = self.book(starts_at, 1.5)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['points_cost'], 2)
        availability = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=1.5)).isoformat(),
            },
            format='json',
        )
        self.assertEqual(
            list(availability.data['remaining_daily_hours'].values()),
            [3.5, 3.0],
        )

    def test_daylight_saving_duration_uses_actual_elapsed_hours(self):
        starts_at = datetime(2026, 10, 4, 1, tzinfo=MELBOURNE)
        ends_at = datetime(2026, 10, 4, 3, 30, tzinfo=MELBOURNE)
        now = datetime(2026, 9, 20, 9, tzinfo=MELBOURNE)

        _, _, points_cost = MeetingRoomService.validate_interval(
            starts_at,
            ends_at,
            now=now,
        )
        day_start, day_end = MeetingRoomService._day_bounds(starts_at.date())

        self.assertEqual(points_cost, 2)
        self.assertEqual(
            MeetingRoomService._overlap_hours(
                starts_at,
                ends_at,
                day_start,
                day_end,
            ),
            1.5,
        )

    def test_daily_limit_is_enforced_across_bookings(self):
        day = future_local(1, 8)
        self.assertEqual(self.book(day, 2).status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.book(day.replace(hour=11), 2).status_code,
            status.HTTP_201_CREATED,
        )

        response = self.book(day.replace(hour=14), 1)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'daily_limit')

    def test_overlap_fails_and_back_to_back_succeeds(self):
        other_user = self.create_member('UROOMOTHER')
        starts_at = future_local(1, 9)
        self.assertEqual(self.book(starts_at, 2).status_code, status.HTTP_201_CREATED)

        overlap = self.book(
            starts_at.replace(hour=10),
            1,
            slack_user_id=other_user.slack_id,
        )
        adjacent = self.book(
            starts_at.replace(hour=11),
            1,
            slack_user_id=other_user.slack_id,
        )

        self.assertEqual(overlap.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(overlap.data['code'], 'booking_conflict')
        self.assertEqual(adjacent.status_code, status.HTTP_201_CREATED)

    def test_room_blocks_and_inactive_rooms_prevent_booking(self):
        starts_at = future_local(1, 9)
        MeetingRoomBlock.objects.create(
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            reason='Maintenance',
        )
        blocked = self.book(starts_at, 1)
        self.assertEqual(blocked.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(blocked.data['code'], 'room_blocked')

        self.room.is_active = False
        self.room.save(update_fields=['is_active'])
        inactive = self.book(starts_at + timedelta(hours=2), 1)
        self.assertEqual(inactive.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(inactive.data['code'], 'inactive_room')

    def test_block_validation_rejects_existing_active_booking(self):
        starts_at = future_local(1, 9)
        self.assertEqual(self.book(starts_at, 1).status_code, status.HTTP_201_CREATED)

        with self.assertRaises(ValidationError):
            MeetingRoomBlock.objects.create(
                room=self.room,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )

        self.assertFalse(MeetingRoomBlock.objects.exists())

    def test_availability_exposes_busy_times_without_member_identity(self):
        starts_at = future_local(1, 9)
        self.assertEqual(self.book(starts_at, 1).status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'date': starts_at.date().isoformat(),
            },
            format='json',
        )
        serialized = json.dumps(response.data).lower()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['busy_intervals']), 1)
        self.assertNotIn(self.user.email, serialized)
        self.assertNotIn(self.user.slack_id.lower(), serialized)
        self.assertNotIn('user', serialized)

    def test_exact_availability_accounts_for_balance_and_daily_limit(self):
        starts_at = future_local(2, 9)
        account = self.user.points_account
        account.balance = 0
        account.earned_balance = 0
        account.save(update_fields=['balance', 'earned_balance'])

        insufficient = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=1)).isoformat(),
            },
            format='json',
        )

        self.assertFalse(insufficient.data['available'])
        self.assertEqual(
            insufficient.data['unavailable_reasons'],
            ['insufficient_balance'],
        )

        account.balance = 10
        account.earned_balance = 10
        account.save(update_fields=['balance', 'earned_balance'])
        self.assertEqual(self.book(starts_at, 2).status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.book(starts_at.replace(hour=11), 2).status_code,
            status.HTTP_201_CREATED,
        )
        later = starts_at.replace(hour=14)
        daily_limit = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': later.isoformat(),
                'ends_at': (later + timedelta(hours=1)).isoformat(),
            },
            format='json',
        )

        self.assertFalse(daily_limit.data['available'])
        self.assertEqual(daily_limit.data['unavailable_reasons'], ['daily_limit'])

    def test_long_availability_window_reports_room_state_without_booking_price(self):
        starts_at = future_local(3, 9)
        account = self.user.points_account
        account.balance = 0
        account.earned_balance = 0
        account.save(update_fields=['balance', 'earned_balance'])

        response = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=4)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['available'])
        self.assertFalse(response.data['bookable'])
        self.assertIsNone(response.data['points_cost'])
        self.assertEqual(response.data['unavailable_reasons'], [])

        MeetingRoomBlock.objects.create(
            room=self.room,
            starts_at=starts_at + timedelta(hours=2),
            ends_at=starts_at + timedelta(hours=3),
            reason='Maintenance',
        )
        blocked = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=4)).isoformat(),
            },
            format='json',
        )

        self.assertFalse(blocked.data['available'])
        self.assertEqual(blocked.data['unavailable_reasons'], ['room_blocked'])

    def test_half_hour_availability_is_allowed_but_not_bookable(self):
        starts_at = future_local(4, 9)

        response = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(minutes=30)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['available'])
        self.assertFalse(response.data['bookable'])
        self.assertIsNone(response.data['points_cost'])

    def test_availability_window_cannot_exceed_twenty_four_hours(self):
        starts_at = future_local(5, 9)

        response = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=24, minutes=30)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['code'], 'invalid_time')

    def test_unlinked_inactive_insufficient_and_non_admin_targeted_requests_fail(self):
        starts_at = future_local(1, 9)
        unlinked = self.book(starts_at, 1, slack_user_id='UMISSING')
        self.assertEqual(unlinked.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(unlinked.data['code'], 'unlinked_user')

        inactive_user = self.create_member('UINACTIVE', active=False)
        inactive = self.book(starts_at, 1, slack_user_id=inactive_user.slack_id)
        self.assertEqual(inactive.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(inactive.data['code'], 'inactive_user')

        poor_user = self.create_member('UPOOR', balance=0)
        insufficient = self.book(starts_at, 1, slack_user_id=poor_user.slack_id)
        self.assertEqual(insufficient.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(insufficient.data['code'], 'insufficient_balance')

        targeted = self.book(starts_at, 1, target_slack_user_id='USOMEONE')
        self.assertEqual(targeted.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(targeted.data['code'], 'admin_required')

    def test_full_points_admin_roles_can_book_for_a_target_member(self):
        target_account = self.user.points_account
        for day_offset, role in enumerate(
            ('admin', 'committee', 'portfolio_lead'),
            start=4,
        ):
            admin = self.create_member(f'UROOM{role.upper()}', balance=7)
            PointsAdmin.objects.create(
                slack_user_id=admin.slack_id,
                user=admin,
                role=role,
                is_active=True,
            )
            with self.subTest(role=role):
                response = self.book(
                    future_local(day_offset, 9),
                    1.5,
                    slack_user_id=admin.slack_id,
                    target_slack_user_id=self.user.slack_id,
                    expected_points_cost=2,
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertTrue(response.data['admin_booking'])
                self.assertEqual(
                    response.data['booked_for_slack_user_id'],
                    self.user.slack_id,
                )
                booking = MeetingRoomBooking.objects.get(
                    pk=response.data['booking']['id']
                )
                self.assertEqual(booking.user, self.user)
                self.assertEqual(booking.requested_by_slack_id, admin.slack_id)
                ledger = Ledger.objects.get(pk=booking.ledger_entry_id)
                self.assertEqual(ledger.user, self.user)
                self.assertEqual(ledger.created_by_slack_id, admin.slack_id)
                admin.points_account.refresh_from_db()
                self.assertEqual(admin.points_account.balance, 7)

        target_account.refresh_from_db()
        self.assertEqual(target_account.balance, 34)

    def test_partner_and_inactive_points_admins_cannot_book_for_others(self):
        starts_at = future_local(8, 9)
        for slack_user_id, role, active in (
            ('UROOMPARTNER', 'partner', True),
            ('UROOMINACTIVEADMIN', 'admin', False),
        ):
            PointsAdmin.objects.create(
                slack_user_id=slack_user_id,
                role=role,
                is_active=active,
            )
            with self.subTest(role=role, active=active):
                response = self.book(
                    starts_at,
                    1,
                    slack_user_id=slack_user_id,
                    target_slack_user_id=self.user.slack_id,
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(response.data['code'], 'admin_required')

        self.assertFalse(MeetingRoomBooking.objects.exists())
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 40)

    def test_admin_availability_uses_target_limits_and_balance(self):
        PointsAdmin.objects.create(
            slack_user_id='UROOMADMINONLY',
            role='admin',
            is_active=True,
        )
        account = self.user.points_account
        account.balance = 1
        account.earned_balance = 1
        account.save(update_fields=['balance', 'earned_balance'])
        starts_at = future_local(9, 9)

        response = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': 'UROOMADMINONLY',
                'target_slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=1.5)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['admin_booking'])
        self.assertFalse(response.data['available'])
        self.assertEqual(response.data['points_cost'], 2)
        self.assertEqual(
            response.data['unavailable_reasons'],
            ['insufficient_balance'],
        )

    def test_confirmation_rejects_changed_preview_price_without_charge(self):
        starts_at = future_local(10, 9)
        response = self.book(
            starts_at,
            1.5,
            expected_points_cost=1,
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'price_changed')
        self.assertFalse(MeetingRoomBooking.objects.exists())
        self.assertFalse(Ledger.objects.filter(source='MEETING_ROOM').exists())
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 40)

    def test_booking_replay_does_not_duplicate_charge(self):
        payload = self.book_payload(future_local(1, 9), 2)
        first = self.client.post(reverse('meeting-room-book'), payload, format='json')
        replay = self.client.post(reverse('meeting-room-book'), payload, format='json')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertTrue(replay.data['already_booked'])
        self.assertEqual(first.data['booking']['id'], replay.data['booking']['id'])
        self.assertEqual(MeetingRoomBooking.objects.count(), 1)
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            1,
        )
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 38)

    def test_admin_cannot_reuse_request_id_for_a_different_target(self):
        admin = self.create_member('UROOMRETARGETADMIN', balance=7)
        other_target = self.create_member('UROOMOTHERTARGET', balance=9)
        PointsAdmin.objects.create(
            slack_user_id=admin.slack_id,
            user=admin,
            role='admin',
            is_active=True,
        )
        payload = self.book_payload(
            future_local(11, 9),
            1,
            slack_user_id=admin.slack_id,
            target_slack_user_id=self.user.slack_id,
            expected_points_cost=1,
        )
        first = self.client.post(
            reverse('meeting-room-book'), payload, format='json'
        )
        payload['target_slack_user_id'] = other_target.slack_id

        retargeted = self.client.post(
            reverse('meeting-room-book'), payload, format='json'
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(retargeted.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(retargeted.data['code'], 'payload_conflict')
        self.assertEqual(MeetingRoomBooking.objects.count(), 1)
        other_target.points_account.refresh_from_db()
        self.assertEqual(other_target.points_account.balance, 9)

    def test_cancelled_booking_confirmation_cannot_report_already_booked(self):
        payload = self.book_payload(future_local(1, 9), 1)
        booked = self.client.post(
            reverse('meeting-room-book'), payload, format='json'
        )
        self.client.post(
            reverse('meeting-room-cancel'),
            {
                'slack_user_id': self.user.slack_id,
                'booking_id': booked.data['booking']['id'],
            },
            format='json',
        )

        replay = self.client.post(
            reverse('meeting-room-book'), payload, format='json'
        )

        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data['code'], 'booking_cancelled')
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            2,
        )

    def test_reused_request_id_with_different_payload_is_rejected(self):
        payload = self.book_payload(future_local(1, 9), 1)
        self.assertEqual(
            self.client.post(reverse('meeting-room-book'), payload, format='json').status_code,
            status.HTTP_201_CREATED,
        )
        payload['ends_at'] = (future_local(1, 9) + timedelta(hours=2)).isoformat()

        response = self.client.post(reverse('meeting-room-book'), payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'payload_conflict')

    def test_expired_confirmation_is_rejected_without_charge(self):
        response = self.book(
            future_local(1, 9),
            1,
            confirmation_expires_at=(timezone.now() - timedelta(seconds=1)).isoformat(),
        )

        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertEqual(response.data['code'], 'expired_confirmation')
        self.assertFalse(MeetingRoomBooking.objects.exists())

    def test_cancellation_refunds_once_and_replay_is_idempotent(self):
        booked = self.book(future_local(1, 9), 2)
        booking_id = booked.data['booking']['id']
        payload = {'slack_user_id': self.user.slack_id, 'booking_id': booking_id}

        cancelled = self.client.post(
            reverse('meeting-room-cancel'), payload, format='json'
        )
        replay = self.client.post(
            reverse('meeting-room-cancel'), payload, format='json'
        )

        self.assertTrue(cancelled.data['cancelled'])
        self.assertTrue(cancelled.data['refunded'])
        self.assertTrue(replay.data['already_cancelled'])
        self.assertTrue(replay.data['refunded'])
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            2,
        )
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 40)

    def test_cancellation_restores_original_point_buckets(self):
        account = self.user.points_account
        account.balance = 5
        account.earned_balance = 3
        account.purchased_topup_balance = 2
        account.lifetime_spent = 0
        account.save()
        booked = self.book(future_local(1, 9), 2)
        booking = MeetingRoomBooking.objects.get(pk=booked.data['booking']['id'])

        self.assertEqual(booking.purchased_points_cost, 2)
        account.refresh_from_db()
        self.assertEqual(account.purchased_topup_balance, 0)
        self.assertEqual(account.earned_balance, 3)
        self.assertEqual(account.lifetime_spent, 2)

        cancelled = self.client.post(
            reverse('meeting-room-cancel'),
            {
                'slack_user_id': self.user.slack_id,
                'booking_id': str(booking.id),
            },
            format='json',
        )

        self.assertTrue(cancelled.data['refunded'])
        account.refresh_from_db()
        self.assertEqual(account.balance, 5)
        self.assertEqual(account.purchased_topup_balance, 2)
        self.assertEqual(account.earned_balance, 3)
        self.assertEqual(account.lifetime_spent, 0)

    def test_cancelled_booking_without_refund_is_recovered_once(self):
        booked = self.book(future_local(1, 9), 2)
        booking = MeetingRoomBooking.objects.get(pk=booked.data['booking']['id'])
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=['status', 'cancelled_at'])

        recovered = self.client.post(
            reverse('meeting-room-cancel'),
            {
                'slack_user_id': self.user.slack_id,
                'booking_id': str(booking.id),
            },
            format='json',
        )
        replay = self.client.post(
            reverse('meeting-room-cancel'),
            {
                'slack_user_id': self.user.slack_id,
                'booking_id': str(booking.id),
            },
            format='json',
        )

        self.assertTrue(recovered.data['cancelled'])
        self.assertTrue(recovered.data['refunded'])
        self.assertTrue(replay.data['refunded'])
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            2,
        )
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 40)

    def test_invalid_or_missing_cancel_booking_id_returns_not_found(self):
        for booking_id in ('', 'not-a-uuid'):
            with self.subTest(booking_id=booking_id):
                response = self.client.post(
                    reverse('meeting-room-cancel'),
                    {
                        'slack_user_id': self.user.slack_id,
                        'booking_id': booking_id,
                    },
                    format='json',
                )

                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(response.data['code'], 'booking_not_found')

    def test_started_booking_cannot_be_cancelled(self):
        booking = MeetingRoomBooking.objects.create(
            room=self.room,
            user=self.user,
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
            status='booked',
            points_cost=2,
            client_request_id=uuid.uuid4(),
            requested_by_slack_id=self.user.slack_id,
        )
        response = self.client.post(
            reverse('meeting-room-cancel'),
            {'slack_user_id': self.user.slack_id, 'booking_id': str(booking.id)},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'booking_started')

    def test_my_bookings_returns_only_requesters_upcoming_active_bookings(self):
        other_user = self.create_member('ULISTOTHER')
        own = self.book(future_local(1, 9), 1).data['booking']['id']
        self.assertEqual(
            self.book(
                future_local(2, 9), 1, slack_user_id=other_user.slack_id
            ).status_code,
            status.HTTP_201_CREATED,
        )

        response = self.client.post(
            reverse('meeting-room-my-bookings'),
            {'slack_user_id': self.user.slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['id'] for row in response.data['bookings']], [own])

    def test_strict_roo_key_and_disabled_feature_are_enforced(self):
        self.client.credentials(HTTP_X_API_KEY='wrong-key')
        denied = self.client.get(reverse('meeting-room-list'))
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.credentials(HTTP_X_API_KEY=TEST_SETTINGS['ROO_API_KEY'])
        with override_settings(MEETING_ROOM_BOOKING_ENABLED=False):
            disabled = self.client.get(reverse('meeting-room-list'))
        self.assertEqual(disabled.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(disabled.data['code'], 'feature_disabled')

    def test_disabled_feature_still_allows_listing_and_cancelling_bookings(self):
        booked = self.book(future_local(1, 9), 1)
        booking_id = booked.data['booking']['id']

        with override_settings(MEETING_ROOM_BOOKING_ENABLED=False):
            listed = self.client.post(
                reverse('meeting-room-my-bookings'),
                {'slack_user_id': self.user.slack_id},
                format='json',
            )
            cancelled = self.client.post(
                reverse('meeting-room-cancel'),
                {
                    'slack_user_id': self.user.slack_id,
                    'booking_id': booking_id,
                },
                format='json',
            )

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(listed.data['bookings'][0]['id'], booking_id)
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.assertTrue(cancelled.data['refunded'])


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL advisory lock test')
@override_settings(**TEST_SETTINGS)
class MeetingRoomConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.room, _ = MeetingRoom.objects.get_or_create(
            slug='meeting-room',
            defaults={'name': 'Meeting Room'},
        )
        self.users = []
        for number in range(2):
            user = User.objects.create_user(
                email=f'concurrent{number}@example.com',
                slack_id=f'UCONCURRENT{number}',
            )
            PointsAccount.objects.create(
                user=user,
                balance=10,
                earned_balance=10,
                lifetime_earned=10,
            )
            self.users.append(user)

    def test_only_one_concurrent_overlapping_booking_succeeds(self):
        starts_at = future_local(1, 9)
        barrier = threading.Barrier(2)
        results = []

        def attempt(user_id):
            connections.close_all()
            user = User.objects.get(pk=user_id)
            barrier.wait()
            try:
                MeetingRoomService.book(
                    user=user,
                    requested_by_slack_id=user.slack_id,
                    room_slug=self.room.slug,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=1),
                    client_request_id=str(uuid.uuid4()),
                    confirmation_expires_at=timezone.now() + timedelta(minutes=10),
                )
                results.append('created')
            except MeetingRoomError as exc:
                results.append(exc.code)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=attempt, args=(user.pk,))
            for user in self.users
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(sorted(results), ['booking_conflict', 'created'])
        self.assertEqual(MeetingRoomBooking.objects.filter(status='booked').count(), 1)
        self.assertEqual(Ledger.objects.filter(source='MEETING_ROOM').count(), 1)

    def test_concurrent_requests_cannot_bypass_daily_member_limit(self):
        user = self.users[0]
        starts_at = future_local(2, 8)
        MeetingRoomService.book(
            user=user,
            requested_by_slack_id=user.slack_id,
            room_slug=self.room.slug,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            client_request_id=str(uuid.uuid4()),
            confirmation_expires_at=timezone.now() + timedelta(minutes=10),
        )
        MeetingRoomService.book(
            user=user,
            requested_by_slack_id=user.slack_id,
            room_slug=self.room.slug,
            starts_at=starts_at + timedelta(hours=2),
            ends_at=starts_at + timedelta(hours=3),
            client_request_id=str(uuid.uuid4()),
            confirmation_expires_at=timezone.now() + timedelta(minutes=10),
        )
        barrier = threading.Barrier(2)
        results = []

        def attempt(hour):
            connections.close_all()
            member = User.objects.get(pk=user.pk)
            request_start = starts_at.replace(hour=hour)
            barrier.wait()
            try:
                MeetingRoomService.book(
                    user=member,
                    requested_by_slack_id=member.slack_id,
                    room_slug=self.room.slug,
                    starts_at=request_start,
                    ends_at=request_start + timedelta(hours=1),
                    client_request_id=str(uuid.uuid4()),
                    confirmation_expires_at=timezone.now() + timedelta(minutes=10),
                )
                results.append('created')
            except MeetingRoomError as exc:
                results.append(exc.code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(hour,)) for hour in (12, 14)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(sorted(results), ['created', 'daily_limit'])
        self.assertEqual(MeetingRoomBooking.objects.filter(status='booked').count(), 3)

    def test_concurrent_block_and_booking_cannot_overlap(self):
        starts_at = future_local(3, 10)
        barrier = threading.Barrier(2)
        results = []

        def create_booking():
            connections.close_all()
            member = User.objects.get(pk=self.users[0].pk)
            barrier.wait()
            try:
                MeetingRoomService.book(
                    user=member,
                    requested_by_slack_id=member.slack_id,
                    room_slug=self.room.slug,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(hours=1),
                    client_request_id=str(uuid.uuid4()),
                    confirmation_expires_at=timezone.now() + timedelta(minutes=10),
                )
                results.append('booking_created')
            except MeetingRoomError as exc:
                results.append(exc.code)
            finally:
                connections.close_all()

        def create_block():
            connections.close_all()
            room = MeetingRoom.objects.get(pk=self.room.pk)
            barrier.wait()
            try:
                with transaction.atomic():
                    MeetingRoomService.lock_room_interval(
                        room=room,
                        starts_at=starts_at,
                        ends_at=starts_at + timedelta(hours=1),
                    )
                    block = MeetingRoomBlock(
                        room=room,
                        starts_at=starts_at,
                        ends_at=starts_at + timedelta(hours=1),
                    )
                    block.full_clean()
                    block.save()
                results.append('block_created')
            except ValidationError:
                results.append('block_conflict')
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=create_booking),
            threading.Thread(target=create_block),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertIn(
            sorted(results),
            (
                ['block_created', 'room_blocked'],
                ['block_conflict', 'booking_created'],
            ),
        )
        self.assertFalse(
            MeetingRoomBooking.objects.filter(status='booked').exists()
            and MeetingRoomBlock.objects.exists()
        )
