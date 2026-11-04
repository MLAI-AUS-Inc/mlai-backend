"""Exercise real booking locks and Roo ledger rows on disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from mentoring.models import MentorAvailabilityRule, MentorProfile, MentorSession
from mentoring.service import MentoringError, MentoringService
from roo.services import PointsService


@override_settings(MENTOR_BOOKING_ENABLED=True, MENTORING_GOOGLE_ENABLED=False)
@skipUnless(connection.vendor == 'postgresql', 'Booking locks require PostgreSQL')
class BookingDatabaseTests(TransactionTestCase):
    def setUp(self):
        # Notifications are outside the transaction and are not part of these tests.
        notification = patch.object(MentoringService, '_notify_safely')
        notification.start()
        self.addCleanup(notification.stop)
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
            starts_at=self.starts_at, expected_price=40,
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
        MentoringService.accept(session.pk, self.mentor_user)
        self.assertEqual(self.balance(self.founder), 60)
        self.assertEqual(self.balance(self.mentor_user), 0)
        with self.assertRaises(MentoringError):
            MentoringService.accept(session.pk, self.mentor_user)
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
            MentoringService.complete(session.pk, self.mentor_user)
            with self.assertRaises(MentoringError):
                MentoringService.complete(session.pk, self.mentor_user)
        session.refresh_from_db()
        self.assertEqual(session.status, MentorSession.STATUS_COMPLETED)
        self.assertIsNotNone(session.payout_ledger_entry_id)
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
