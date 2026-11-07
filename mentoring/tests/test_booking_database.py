"""Exercise real booking locks and Roo ledger rows on disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import MagicMock, patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from mentoring.models import MentorAvailabilityRule, MentorProfile, MentorSession
from mentoring.service import MentoringError, MentoringService
from mentoring.conflicts import read as read_calendar_conflicts
from roo.services import PointsService


@override_settings(MENTOR_BOOKING_ENABLED=True, MENTORING_GOOGLE_ENABLED=False)
@skipUnless(connection.vendor == 'postgresql', 'Booking locks require PostgreSQL')
class BookingDatabaseTests(TransactionTestCase):
    def setUp(self):
        # Notifications are outside the transaction and are not part of these tests.
        notification = patch.object(MentoringService, '_notify_safely')
        notification.start()
        self.addCleanup(notification.stop)
        provider = patch('mentoring.conflicts.read', return_value=[])
        provider.start()
        self.addCleanup(provider.stop)
        self.mentor_user = self.user('mentor')
        self.founder = self.user('founder', points=100)
        self.mentor = MentorProfile.objects.create(
            user=self.mentor_user, is_active=True, timezone='UTC',
            price_roo=40, session_minutes=30, location_type='mlai_space',
        )
        self.starts_at = (timezone.now() + timedelta(days=4)).replace(
            hour=11, minute=0, second=0, microsecond=0,
        )
        MentorAvailabilityRule.objects.create(
            mentor=self.mentor, weekday=self.starts_at.weekday(),
            start_time=time(9), end_time=time(17),
        )

    def test_missing_meeting_repair_keeps_existing_price_time_and_ledger(self):
        from types import SimpleNamespace
        from mentoring import meeting_repair
        from mentoring.calendars import google
        self.mentor.location_type = 'video'
        self.mentor.save()
        session, _ = self.request()
        MentoringService.accept(session.pk, self.mentor_user)
        session.refresh_from_db()
        frozen = (session.starts_at, session.ends_at, session.price_roo, session.charge_ledger_entry_id)
        target = {'connection_id': 1, 'calendar_id': 'synthetic', 'revision': 1}
        with patch.object(meeting_repair.calendars, 'calendar_target', return_value=target):
            meeting_repair.prepare(session.pk, connection_id=1, calendar_id='synthetic')
        client = MagicMock()
        client.configure_capture.return_value = {'verified': True, 'policy_version': 1}
        result = {'event_id': google.event_id(session.pk), 'meet_link': 'https://meet.google.com/abc-defg-hij', 'status': 'created'}
        c = SimpleNamespace(scopes=[google.MEET_SETTINGS_SCOPE])
        with patch.object(meeting_repair.calendars, 'lookup_booking', return_value=result), \
             patch.object(meeting_repair.calendars, '_target', return_value=(c, 'synthetic')), \
             patch.object(meeting_repair.calendars, '_client', return_value=client), \
             patch.object(meeting_repair.calendars, '_access_token'), \
             patch.object(meeting_repair.calendars, 'verify_booking'), \
             patch.object(meeting_repair.calendars, 'invite_attendees') as invite, \
             patch.object(meeting_repair.calendars, 'create_booking') as create:
            meeting_repair.process(session.pk)
            meeting_repair.process(session.pk)
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted')
        self.assertEqual(frozen, (session.starts_at, session.ends_at, session.price_roo, session.charge_ledger_entry_id))
        self.assertEqual(session.meet_link, result['meet_link'])
        self.assertEqual(self.balance(self.founder), 60)
        invite.assert_called_once(); create.assert_not_called()

    def test_worker_recovers_skipped_booking_after_mentor_converts_without_recharging(self):
        from types import SimpleNamespace
        from mentoring import instant_booking, meeting_repair
        from mentoring.calendars import google
        self.mentor.location_type = 'video'
        self.mentor.save()
        session, _ = self.request()
        MentoringService.accept(session.pk, self.mentor_user)
        session.refresh_from_db()
        self.assertEqual((session.meeting_status, session.meeting_error), ('unavailable', 'google_off'))
        frozen = (session.starts_at, session.ends_at, session.price_roo, session.charge_ledger_entry_id)
        self.mentor.booking_mode = 'instant'
        from mentoring.meeting_policy import REQUIRED_POLICY
        self.mentor.meeting_artifact_policy = dict(REQUIRED_POLICY)
        self.mentor.save()
        target = {'connection_id': 1, 'calendar_id': 'synthetic', 'revision': 1}
        result = {'event_id': google.event_id(session.pk), 'meet_link': 'https://meet.google.com/abc-defg-hij', 'status': 'created'}
        client = MagicMock()
        client.configure_capture.return_value = {'verified': True, 'policy_version': 1}
        c = SimpleNamespace(scopes=[google.MEET_SETTINGS_SCOPE])
        with patch.object(meeting_repair.calendars, 'calendar_target', return_value=target), \
             patch.object(meeting_repair.calendars, 'lookup_booking', return_value=None), \
             patch.object(meeting_repair.calendars, 'fetch_busy', return_value=[]), \
             patch.object(meeting_repair.calendars, 'create_booking', return_value=result) as create, \
             patch.object(meeting_repair.calendars, '_target', return_value=(c, 'synthetic')), \
             patch.object(meeting_repair.calendars, '_client', return_value=client), \
             patch.object(meeting_repair.calendars, '_access_token'), \
             patch.object(meeting_repair.calendars, 'verify_booking'), \
             patch.object(meeting_repair.calendars, 'invite_attendees') as invite:
            instant_booking.process_due_bookings()
            instant_booking.process_due_bookings()
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted')
        self.assertEqual(frozen, (session.starts_at, session.ends_at, session.price_roo, session.charge_ledger_entry_id))
        self.assertEqual(session.meet_link, result['meet_link'])
        self.assertTrue(session.meeting_provisioning['verified'])
        self.assertEqual(session.notification_status, 'sent')
        self.assertEqual(self.balance(self.founder), 60)
        create.assert_called_once(); invite.assert_called_once()

    def test_cancelled_unknown_repair_retains_time_until_event_deleted(self):
        from mentoring import meeting_repair
        session, _ = self.request()
        MentoringService.accept(session.pk, self.mentor_user)
        session.refresh_from_db()
        session.meeting_provisioning = {'repair': True, 'cleanup_pending': True, 'unknown_create': True}
        session.save()
        MentoringService.cancel(session.pk, self.founder)
        with patch.object(meeting_repair.calendars, 'cancel_booking', return_value={'outcome': 'absent'}):
            meeting_repair.process(session.pk)
        session.refresh_from_db()
        self.assertTrue(session.time_reservations.filter(active=True).exists())
        session.meeting_claimed_at = None
        session.save()
        with patch.object(meeting_repair.calendars, 'cancel_booking', return_value={'outcome': 'deleted'}):
            meeting_repair.process(session.pk)
        self.assertFalse(session.time_reservations.filter(active=True).exists())
        self.assertEqual(self.balance(self.founder), 100)

    def test_busy_calendar_rejects_legacy_request_before_session_or_charge(self):
        with patch('mentoring.conflicts.read', return_value=[(self.starts_at, self.starts_at + timedelta(hours=1))]):
            with self.assertRaises(MentoringError) as caught:
                self.request()
        self.assertEqual(caught.exception.code, 'slot_unavailable')
        self.assertEqual(MentorSession.objects.count(), 0)
        self.assertEqual(self.balance(self.founder), 100)

    def test_busy_event_added_after_request_blocks_acceptance_without_charge(self):
        session, _ = self.request()
        with patch('mentoring.conflicts.read', return_value=[(self.starts_at, self.starts_at + timedelta(hours=1))]):
            with self.assertRaises(MentoringError):
                MentoringService.accept(session.pk, self.mentor_user)
        session.refresh_from_db()
        self.assertEqual(session.status, 'requested')
        self.assertIsNone(session.charge_ledger_entry_id)
        self.assertEqual(self.balance(self.founder), 100)

    def test_personal_calendar_protects_legacy_monday_incident(self):
        from datetime import date, datetime, timezone as utc
        from mentoring.calendar_models import MentorCalendarConnection
        self.mentor.timezone = 'Australia/Melbourne'
        self.mentor.session_minutes = 15
        self.mentor.save()
        self.mentor.availability_rules.all().delete()
        MentorAvailabilityRule.objects.create(mentor=self.mentor, weekday=0, start_time=time(9), end_time=time(17))
        MentorCalendarConnection.objects.create(user=self.mentor_user, provider_subject='synthetic',
            email='mentor@example.invalid', conflict_calendar_ids=['work', 'personal'], booking_calendar_id='work')
        # Override only the synthetic upstream response; exercise source resolution,
        # HTTP-shaped availability and the real scheduling engine together.
        from mentoring import conflicts
        from mentoring.calendars import service as calendars
        start = datetime(2026, 11, 8, 22, tzinfo=utc.utc)
        with patch('mentoring.conflicts.read', side_effect=read_calendar_conflicts), \
             patch.object(calendars, 'fetch_busy', return_value=[(start, start + timedelta(hours=9))]) as provider, \
             patch('mentoring.service.timezone.now', return_value=start - timedelta(days=3)):
            result = MentoringService.availability(self.mentor.pk, date(2026, 11, 9), date(2026, 11, 9))
        self.assertEqual(result['booking_mode'], 'legacy_request')
        self.assertEqual(result['busy_check'], 'ok')
        self.assertEqual(result['slots'], [])
        provider.assert_called_once()

    def user(self, name, points=0):
        user = get_user_model().objects.create_user(
            email=f'{name}@mentoring-tests.invalid', first_name=name,
            slack_id=f'UMENTORTEST{name.upper()}',
        )
        if points:
            PointsService.award(
                user=user, delta=points, source='MANUAL',
                description='Synthetic mentoring test balance',
                created_by_slack_id='UMENTORTEST',
                idempotency_key=f'mentoring-test:{user.pk}',
            )
        return user

    def balance(self, user):
        return PointsService.get_available_microroo(user) // 1_000_000

    def request(self, founder=None, request_id=None):
        return MentoringService.request_session(
            founder=founder or self.founder, mentor_id=self.mentor.pk,
            starts_at=self.starts_at.isoformat(), expected_price=40,
            client_request_id=request_id or str(uuid.uuid4()),
            brief={'stage': 'Idea', 'struggle': 'Finding users', 'outcome': 'Next step'},
        )

    def test_request_replay_does_not_charge_or_create_another_session(self):
        request_id = str(uuid.uuid4())
        session, created = self.request(request_id=request_id)
        replay, replay_created = self.request(request_id=request_id)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(session.pk, replay.pk)
        self.assertEqual(MentorSession.objects.count(), 1)
        self.assertEqual(self.balance(self.founder), 100)
        self.assertIsNone(session.charge_ledger_entry_id)

    def test_accept_charges_once_and_pre_start_cancel_restores_balance(self):
        session, _ = self.request()
        accepted = MentoringService.accept(session.pk, self.mentor_user)
        self.assertEqual(self.balance(self.founder), 60)
        self.assertEqual(self.balance(self.mentor_user), 0)
        replay = MentoringService.accept(session.pk, self.mentor_user)
        self.assertEqual(replay.charge_ledger_entry_id, accepted.charge_ledger_entry_id)
        self.assertEqual(self.balance(self.founder), 60)
        MentoringService.cancel(session.pk, self.founder)
        session.refresh_from_db()
        self.assertEqual(session.status, MentorSession.STATUS_CANCELLED)
        self.assertEqual(self.balance(self.founder), 100)
        self.assertIsNotNone(session.charge_ledger_entry_id)
        self.assertIsNotNone(session.release_ledger_entry_id)
        self.assertIsNone(session.payout_ledger_entry_id)

    def test_completion_pays_mentor_once_and_cancellation_after_start_is_refused(self):
        session, _ = self.request()
        MentoringService.accept(session.pk, self.mentor_user)
        with patch('mentoring.service.timezone.now', return_value=self.starts_at):
            with self.assertRaises(MentoringError) as caught:
                MentoringService.cancel(session.pk, self.founder)
        self.assertEqual(caught.exception.code, 'session_started')
        with patch('mentoring.service.timezone.now', return_value=session.ends_at + timedelta(seconds=1)):
            completed = MentoringService.complete(session.pk, self.mentor_user)
            replay = MentoringService.complete(session.pk, self.mentor_user)
            self.assertEqual(replay.payout_ledger_entry_id, completed.payout_ledger_entry_id)
        session.refresh_from_db()
        self.assertEqual(session.status, MentorSession.STATUS_COMPLETED)
        self.assertIsNotNone(session.payout_ledger_entry_id)
        payout = session.payout_ledger_entry
        self.assertEqual(payout.created_by_slack_id, 'MENTORING')
        self.assertNotEqual(payout.created_by_slack_id, self.mentor_user.slack_id)
        self.assertEqual(self.balance(self.founder), 60)
        self.assertEqual(self.balance(self.mentor_user), 40)

    def test_low_balance_request_is_rejected_without_creating_a_session(self):
        poor_founder = self.user('poor')
        with self.assertRaises(MentoringError) as caught:
            self.request(founder=poor_founder)
        self.assertEqual(caught.exception.code, 'insufficient_points')
        self.assertEqual(MentorSession.objects.count(), 0)

    def test_lazy_completion_pays_when_a_session_is_read_after_24_hours(self):
        session, _ = self.request()
        MentoringService.accept(session.pk, self.mentor_user)
        with patch('mentoring.service.timezone.now', return_value=session.ends_at + timedelta(hours=24, seconds=1)):
            MentoringService.list_sessions(self.founder, 'founder')
            MentoringService.list_sessions(self.founder, 'founder')
        session.refresh_from_db()
        self.assertEqual(session.status, MentorSession.STATUS_COMPLETED)
        self.assertEqual(self.balance(self.mentor_user), 40)

    def concurrent_requests(self, founders, request_ids):
        barrier = Barrier(2)

        def run(founder, request_id):
            try:
                barrier.wait(timeout=10)
                session, created = self.request(founder, request_id)
                return str(session.pk), created
            except MentoringError as exc:
                return exc.code, False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, user, request_id) for user, request_id in zip(founders, request_ids)]
            return [future.result(timeout=30) for future in futures]

    def test_two_founders_cannot_reserve_the_same_slot(self):
        other = self.user('other', points=100)
        results = self.concurrent_requests(
            [self.founder, other], [str(uuid.uuid4()), str(uuid.uuid4())],
        )
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertIn(('slot_unavailable', False), results)
        self.assertEqual(MentorSession.objects.count(), 1)
        self.assertEqual(self.balance(self.founder), 100)
        self.assertEqual(self.balance(other), 100)

    def test_concurrent_retries_create_only_one_session(self):
        request_id = str(uuid.uuid4())
        results = self.concurrent_requests(
            [self.founder, self.founder], [request_id, request_id],
        )
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(MentorSession.objects.count(), 1)
