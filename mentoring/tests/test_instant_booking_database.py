"""Real rows/ledger/exclusion with a synthetic external calendar only.

Requires explicit approval of the disposable test database migration inventory.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import MagicMock, patch
import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from mentoring import instant_booking as instant
from mentoring.calendars.errors import CalendarError
from mentoring.models import MentorAvailabilityRule, MentorBookingAttempt, MentorProfile, MentorSession, MentorTimeReservation
from mentoring.service import MentoringError, MentoringService
from roo.models import Ledger
from roo.services import PointsService


@skipUnless(connection.vendor == 'postgresql', 'Instant booking requires PostgreSQL')
@override_settings(MENTOR_INSTANT_BOOKING_ENABLED=True)
class InstantBookingDatabaseTests(TransactionTestCase):
    def setUp(self):
        self.host = self.user('host')
        self.founder = self.user('founder', 100)
        self.profile = MentorProfile.objects.create(user=self.host, is_active=True,
            accepting_bookings=True, booking_mode='instant', timezone='UTC', session_minutes=15, price_roo=40,
            location_type='mlai_space')
        self.start = (timezone.now()+timedelta(days=4)).replace(hour=11,minute=0,second=0,microsecond=0)
        MentorAvailabilityRule.objects.create(mentor=self.profile,weekday=self.start.weekday(),start_time=time(9),end_time=time(17))
        self.provider = MagicMock()
        self.provider.calendar_target.return_value = {'connection_id':1,'calendar_id':'synthetic-calendar','revision':1}
        self.provider.fetch_busy.return_value = []
        self.provider.lookup_booking.return_value = None
        self.created = {'event_id':'mlaimentor-synthetic','meet_link':'','status':'created'}
        self.provider.create_booking.return_value = self.created
        self.provider.verify_booking.return_value = None
        self.provider.cancel_booking.return_value = {'outcome':'deleted'}
        p=patch.object(instant,'_calendars',return_value=self.provider)
        p.start()
        self.addCleanup(p.stop)
        p=patch.object(MentoringService,'_notify_safely')
        p.start()
        self.addCleanup(p.stop)

    def user(self,name,points=0):
        u=get_user_model().objects.create_user(email=f'{name}@instant-tests.invalid',first_name=name,slack_id=f'UINSTANT{name}')
        if points:
            PointsService.award(user=u,delta=points,source='MANUAL',description='Synthetic balance',
                created_by_slack_id='UTEST',idempotency_key=f'instant-test:{u.pk}')
        return u

    def balance(self,user):
        return PointsService.get_available_microroo(user)//1_000_000

    def quote(self,founder=None):
        return instant.make_quote(founder=founder or self.founder,mentor_id=self.profile.pk,
            starts_at=self.start.isoformat(),expected_price=40)['quote']

    def book(self,founder=None,quote=None,request_id=None):
        return instant.book(founder=founder or self.founder,quote=quote or self.quote(founder),
            brief={'stage':'Idea','struggle':'Finding users','outcome':'Next steps'},
            client_request_id=request_id or str(uuid.uuid4()))

    def test_debit_is_provisional_and_confirmation_is_automatic(self):
        attempt=self.book()
        self.assertEqual(attempt.session.status,'provisioning')
        self.assertEqual(self.balance(self.founder),60)
        self.assertEqual(self.balance(self.host),0)
        self.assertEqual(attempt.session.time_reservations.filter(active=True).count(),2)
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        attempt.session.refresh_from_db()
        self.assertEqual(attempt.status,'confirmed')
        self.assertEqual(attempt.session.status,'accepted')
        self.assertEqual(self.balance(self.founder),60)
        self.assertIsNone(attempt.session.payout_ledger_entry_id)
        instant.process_attempt(attempt.pk)
        self.provider.invite_attendees.assert_called_once()
        self.assertEqual(Ledger.objects.filter(idempotency_key=f'mentor_charge:{attempt.session_id}').count(),1)

    def test_same_key_replays_before_quote_expiry_validation(self):
        quote=self.quote();request_id=str(uuid.uuid4())
        first=self.book(quote=quote,request_id=request_id)
        with patch.object(instant,'_load_quote',side_effect=AssertionError('Quote must not be revalidated')):
            replay=self.book(quote=quote,request_id=request_id)
        self.assertEqual(first.pk,replay.pk)
        self.assertEqual(self.balance(self.founder),60)
        with self.assertRaises(MentoringError) as caught:
            instant.book(founder=self.founder,quote=quote,brief={'stage':'changed'},client_request_id=request_id)
        self.assertEqual(caught.exception.code,'idempotency_conflict')

    def test_definitive_failure_refunds_once_and_retains_cleanup(self):
        attempt=self.book()
        self.provider.create_booking.side_effect=CalendarError('permission')
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db();attempt.session.refresh_from_db()
        self.assertEqual(attempt.status,'failed')
        self.assertEqual(self.balance(self.founder),100)
        self.assertIsNotNone(attempt.session.release_ledger_entry_id)
        self.assertTrue(attempt.cleanup_pending)
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertFalse(attempt.cleanup_pending)
        self.assertEqual(self.balance(self.founder),100)
        self.assertFalse(attempt.session.time_reservations.filter(active=True).exists())

    def test_ambiguous_remote_success_recovers_without_second_insert(self):
        attempt=self.book()
        self.provider.create_booking.side_effect=CalendarError('network',uncertain=True)
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status,'reconciling')
        self.assertEqual(self.balance(self.founder),60)
        self.provider.lookup_booking.return_value=self.created
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status,'confirmed')
        self.provider.create_booking.assert_called_once()
        self.assertEqual(self.balance(self.founder),60)

    def test_cancel_while_create_in_flight_never_resurrects_confirmation(self):
        attempt=self.book()
        def late_create(*args,**kwargs):
            instant.cancel_attempt(attempt.pk,self.founder)
            return self.created
        self.provider.create_booking.side_effect=late_create
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db();attempt.session.refresh_from_db()
        self.assertEqual(attempt.status,'cancelled')
        self.assertEqual(attempt.session.status,'cancelled')
        self.assertEqual(self.balance(self.founder),100)
        self.assertTrue(attempt.cleanup_pending)
        MentorBookingAttempt.objects.filter(pk=attempt.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        instant.process_attempt(attempt.pk)
        self.provider.cancel_booking.assert_called_once()
        attempt.refresh_from_db()
        self.assertFalse(attempt.cleanup_pending)

    def test_confirmed_cancel_uses_personal_calendar_and_refunds(self):
        attempt=self.book();instant.process_attempt(attempt.pk)
        session=MentoringService.cancel(attempt.session_id,self.founder)
        self.assertEqual(session.status,'cancelled')
        self.assertEqual(self.balance(self.founder),100)
        attempt.refresh_from_db()
        self.assertTrue(attempt.cleanup_pending)
        instant.process_attempt(attempt.pk)
        self.provider.cancel_booking.assert_called_once()

    def test_deadline_refunds_even_when_provider_outage_continues(self):
        attempt=self.book()
        self.provider.lookup_booking.side_effect=CalendarError('network')
        instant.process_attempt(attempt.pk)
        MentorBookingAttempt.objects.filter(pk=attempt.pk).update(deadline=timezone.now()-timedelta(seconds=1),lease_until=None)
        self.provider.cancel_booking.side_effect=CalendarError('network',uncertain=True)
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status,'failed')
        self.assertEqual(self.balance(self.founder),100)
        self.assertTrue(attempt.cleanup_pending)
        self.assertTrue(attempt.session.time_reservations.filter(active=True).exists())

    def test_database_excludes_overlaps_across_attending_and_hosting(self):
        attempt=self.book()
        other=MentorSession.objects.create(mentor=self.profile,founder=self.founder,
            starts_at=self.start,ends_at=self.start+timedelta(minutes=15),price_roo=40,
            location_type='mlai_space',client_request_id=uuid.uuid4(),requested_at=timezone.now(),expires_at=self.start)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MentorTimeReservation.objects.create(session=other,principal=self.founder,
                starts_at=self.start,ends_at=self.start+timedelta(minutes=15))
        self.assertEqual(attempt.session.time_reservations.count(),2)

    def test_two_founders_race_only_one_debit_and_reservation(self):
        other=self.user('second-founder',100)
        quotes=[self.quote(),self.quote(other)]
        barrier=Barrier(2)
        def worker(user_id,quote):
            connections.close_all()
            user=get_user_model().objects.get(pk=user_id)
            barrier.wait(timeout=5)
            try:
                return ('ok',str(self.book(founder=user,quote=quote).pk))
            except MentoringError as exc:
                return ('error',exc.code)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda item:worker(*item),[(self.founder.pk,quotes[0]),(other.pk,quotes[1])]))
        self.assertEqual(sum(r[0]=='ok' for r in results),1)
        self.assertEqual(MentorBookingAttempt.objects.count(),1)
        self.assertEqual(self.balance(self.founder)+self.balance(other),160)

    def test_legacy_request_does_not_charge_or_convert(self):
        self.profile.accepting_bookings=False;self.profile.booking_mode='legacy_request';self.profile.save()
        session,_=MentoringService.request_session(founder=self.founder,mentor_id=self.profile.pk,
            starts_at=self.start.isoformat(),brief={'stage':'Idea','struggle':'Users','outcome':'Plan'},
            client_request_id=str(uuid.uuid4()),expected_price=40)
        self.assertEqual(session.status,'requested')
        self.assertEqual(self.balance(self.founder),100)
        self.assertIsNone(session.charge_ledger_entry_id)

    def test_account_teardown_rejects_funded_then_prunes_refunded_cleanup_complete(self):
        from mentoring.account_teardown import prepare_account_deletion
        attempt=self.book()
        with transaction.atomic(), self.assertRaises(MentoringError) as caught:
            prepare_account_deletion(self.founder)
        self.assertEqual(caught.exception.code,'mentoring_obligations_pending')
        instant.cancel_attempt(attempt.pk,self.founder)
        with transaction.atomic():
            prepare_account_deletion(self.founder)
            self.founder.delete()
        self.assertFalse(MentorBookingAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertFalse(MentorTimeReservation.objects.filter(session_id=attempt.session_id).exists())

    def test_quote_and_signed_payload_do_not_disclose_private_calendar_target(self):
        response=instant.make_quote(founder=self.founder,mentor_id=self.profile.pk,
            starts_at=self.start.isoformat(),expected_price=40)
        terms=instant.signing.loads(response['quote'],salt=instant.QUOTE_SALT)
        self.assertNotIn('calendar_target',response)
        self.assertNotIn('calendar_target',terms)
        self.assertNotIn('connection_id',terms)
        self.assertNotIn('calendar_id',terms)
        self.assertNotIn('synthetic-calendar',str(response))
        self.assertIn('calendar_fingerprint',terms)

    def test_refund_restores_exact_purchased_and_earned_split(self):
        from roo.models import PointsAccount
        PointsAccount.objects.filter(user=self.founder).update(
            purchased_topup_balance=20, earned_balance=80,
            purchased_topup_balance_microroo=20_000_000, earned_balance_microroo=80_000_000)
        attempt=self.book()
        attempt.session.refresh_from_db()
        self.assertEqual(attempt.session.purchased_points_cost_microroo,20_000_000)
        instant.cancel_attempt(attempt.pk,self.founder)
        account=PointsAccount.objects.get(user=self.founder)
        self.assertEqual(account.purchased_topup_balance_microroo,20_000_000)
        self.assertEqual(account.earned_balance_microroo,80_000_000)
        self.assertEqual(account.balance_microroo,100_000_000)
        self.assertEqual(account.lifetime_spent_microroo,0)

    def test_booking_and_another_product_cannot_overspend_same_wallet(self):
        from roo.permissions import InsufficientBalanceError
        quote=self.quote();barrier=Barrier(2)
        def spend(kind):
            connections.close_all()
            user=get_user_model().objects.get(pk=self.founder.pk)
            barrier.wait(timeout=5)
            try:
                if kind=='booking':
                    self.book(founder=user,quote=quote)
                else:
                    PointsService.spend(user=user,delta=70,source='MANUAL',description='Synthetic other product',
                        created_by_slack_id='UTEST',idempotency_key='instant-test-other-spend')
                return 'ok'
            except (MentoringError,InsufficientBalanceError):
                return 'insufficient'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(spend,['booking','other']))
        self.assertEqual(results.count('ok'),1)
        self.assertIn(self.balance(self.founder),(30,60))
        self.assertEqual(Ledger.objects.filter(user=self.founder,kind='SPEND').count(),1)

    def test_unknown_create_404_cleanup_keeps_quarantine_until_late_event_deleted(self):
        attempt=self.book()
        self.provider.create_booking.side_effect=CalendarError('network',uncertain=True)
        instant.process_attempt(attempt.pk)
        instant.cancel_attempt(attempt.pk,self.founder)
        self.provider.cancel_booking.return_value={'outcome':'absent'}
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status,'cancelled')
        self.assertTrue(attempt.cleanup_pending)
        self.assertTrue(attempt.session.time_reservations.filter(active=True).exists())
        self.assertEqual(self.balance(self.founder),100)
        # The remote create finishes after the HTTP worker has already exited.
        # A later positive lookup plus acknowledged delete resolves uncertainty.
        self.provider.cancel_booking.return_value={'outcome':'deleted'}
        instant.process_attempt(attempt.pk)
        attempt.refresh_from_db()
        self.assertFalse(attempt.cleanup_pending)
        self.assertFalse(attempt.session.time_reservations.filter(active=True).exists())
        self.assertEqual(self.balance(self.founder),100)

    def test_worker_expires_legacy_request_after_mentor_converts(self):
        self.profile.accepting_bookings=False
        self.profile.booking_mode='legacy_request'
        self.profile.save()
        legacy,_=MentoringService.request_session(founder=self.founder,mentor_id=self.profile.pk,
            starts_at=self.start.isoformat(),brief={'stage':'Idea','struggle':'Users','outcome':'Plan'},
            client_request_id=str(uuid.uuid4()),expected_price=40)
        MentorSession.objects.filter(pk=legacy.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.profile.accepting_bookings=True
        self.profile.booking_mode='instant'
        self.profile.save()
        instant.process_due_bookings()
        legacy.refresh_from_db()
        self.assertEqual(legacy.status,'expired')
        self.assertFalse(legacy.time_reservations.filter(active=True).exists())
        quote=self.quote()
        attempt=self.book(quote=quote)
        self.assertEqual(attempt.session.status,'provisioning')

    def test_profile_update_locks_user_before_only_the_profile_row(self):
        locked=[]
        def record(execute,sql,params,many,context):
            if 'FOR UPDATE' in sql:
                locked.append(sql)
            return execute(sql,params,many,context)
        with connection.execute_wrapper(record):
            MentoringService.update_profile(self.host,{'bio':'Updated profile'})
        self.assertIn('"core_user"',locked[0])
        profile_locks=[sql for sql in locked if '"mentoring_mentorprofile"' in sql]
        self.assertTrue(profile_locks)
        self.assertIn('FOR UPDATE OF "mentoring_mentorprofile"',profile_locks[0])
