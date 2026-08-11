import json
import threading
import uuid
from datetime import datetime, time, timedelta
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .meeting_rooms import MeetingRoomError, MeetingRoomService
from .models import Ledger, MeetingRoom, MeetingRoomBlock, MeetingRoomBooking, PointsAccount


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

    def test_one_to_four_hours_charge_one_point_per_hour(self):
        for duration in range(1, 5):
            with self.subTest(duration=duration):
                response = self.book(future_local(duration, 9), duration)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data['points_cost'], duration)

        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 30)
        self.assertEqual(
            Ledger.objects.filter(user=self.user, source='MEETING_ROOM').count(),
            4,
        )

    def test_invalid_intervals_are_rejected(self):
        valid_start = future_local(1, 9)
        cases = (
            ('partial hour', valid_start.replace(minute=30), 1),
            ('zero duration', valid_start, 0),
            ('more than four hours', valid_start, 5),
            ('past', future_local(-1, 9), 1),
            ('more than thirty days', future_local(31, 9), 1),
        )
        for label, starts_at, duration in cases:
            with self.subTest(case=label):
                response = self.book(starts_at, duration)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data['code'], 'invalid_time')

        self.assertFalse(MeetingRoomBooking.objects.exists())

    def test_cross_midnight_cost_and_daily_allowance_are_split(self):
        starts_at = future_local(1, 22)
        response = self.book(starts_at, 4)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        availability = self.client.post(
            reverse('meeting-room-availability'),
            {
                'slack_user_id': self.user.slack_id,
                'starts_at': starts_at.isoformat(),
                'ends_at': (starts_at + timedelta(hours=4)).isoformat(),
            },
            format='json',
        )
        self.assertEqual(
            list(availability.data['remaining_daily_hours'].values()),
            [2.0, 2.0],
        )

    def test_daylight_saving_duration_uses_actual_elapsed_hours(self):
        starts_at = datetime(2026, 10, 4, 1, tzinfo=MELBOURNE)
        ends_at = datetime(2026, 10, 4, 5, tzinfo=MELBOURNE)
        now = datetime(2026, 9, 20, 9, tzinfo=MELBOURNE)

        _, _, points_cost = MeetingRoomService.validate_interval(
            starts_at,
            ends_at,
            now=now,
        )
        day_start, day_end = MeetingRoomService._day_bounds(starts_at.date())

        self.assertEqual(points_cost, 3)
        self.assertEqual(
            MeetingRoomService._overlap_hours(
                starts_at,
                ends_at,
                day_start,
                day_end,
            ),
            3,
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
        block = MeetingRoomBlock(
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )

        with self.assertRaises(ValidationError):
            block.full_clean()

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

    def test_unlinked_inactive_insufficient_and_targeted_requests_fail(self):
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
        self.assertEqual(targeted.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(targeted.data['code'], 'unsupported_target')

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
        self.assertEqual(MeetingRoomBooking.objects.filter(status='booked').count(), 2)

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
