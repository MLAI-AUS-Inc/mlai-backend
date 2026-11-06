"""Real participant reservations/refunds; synthetic Google reads only."""
from datetime import timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock, patch
import uuid

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from mentoring import calendar_sync as sync, instant_booking as instant
from mentoring.calendars import google
from mentoring.calendars.errors import CalendarError
from mentoring.models import MentorSession, MentorTimeReservation
from mentoring.service import MentoringService
from mentoring.tests import test_instant_booking_database as existing


@skipUnless(connection.vendor=='postgresql','Requires approved disposable PostgreSQL')
@override_settings(MENTOR_INSTANT_BOOKING_ENABLED=True)
class CalendarSyncDatabaseTests(TransactionTestCase):
    user=existing.InstantBookingDatabaseTests.user
    balance=existing.InstantBookingDatabaseTests.balance
    quote=existing.InstantBookingDatabaseTests.quote

    def setUp(self):
        existing.InstantBookingDatabaseTests.setUp(self)
        attempt=instant.book(founder=self.founder,quote=self.quote(),
            brief={'stage':'idea','struggle':'users','outcome':'plan'},client_request_id=str(uuid.uuid4()))
        self.created['event_id']=google.event_id(attempt.session_id)
        instant.process_attempt(attempt.pk)
        instant.process_attempt(attempt.pk)
        self.session=MentorSession.objects.get(pk=attempt.session_id)
        self.client=MagicMock()
        self.event={'id':google.event_id(self.session.pk),'status':'confirmed','etag':'v2',
            'start':{'dateTime':google.iso(self.start+timedelta(hours=1))},
            'end':{'dateTime':google.iso(self.start+timedelta(hours=1,minutes=15))},
            'extendedProperties':{'private':{'mlai_booking_id':str(self.session.pk)}}}
        self.client.read_booking_change.return_value=self.event
        for target,kwargs in [('_target',{'return_value':(SimpleNamespace(revision=1),'synthetic-calendar')}),
                              ('_client',{'return_value':self.client}),('check_after_create',{'return_value':None})]:
            p=patch.object(sync.calendars,target,**kwargs);p.start();self.addCleanup(p.stop)

    def test_move_updates_both_reservations_without_second_charge(self):
        charge=self.session.charge_ledger_entry_id
        sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at,self.start+timedelta(hours=1))
        self.assertEqual(self.session.charge_ledger_entry_id,charge)
        self.assertEqual(self.balance(self.founder),60)
        self.assertEqual(self.session.time_reservations.filter(active=True).count(),2)
        self.assertEqual(self.session.time_reservations.get(principal=self.founder).starts_at,self.session.starts_at)
        sync.sync_session(self.session.pk)
        self.assertEqual(self.balance(self.founder),60)

    def test_google_cancellation_refunds_exactly_once(self):
        self.client.read_booking_change.return_value={'id':google.event_id(self.session.pk),'status':'cancelled'}
        sync.sync_session(self.session.pk);sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status,'cancelled')
        self.assertEqual(self.balance(self.founder),100)
        self.assertIsNotNone(self.session.release_ledger_entry_id)

    def test_permission_failure_and_404_never_cancel_or_release_time(self):
        for code in ('permission','not_found','provider_unavailable'):
            self.client.read_booking_change.side_effect=CalendarError(code)
            sync.sync_session(self.session.pk)
            self.session.refresh_from_db()
            self.assertEqual(self.session.status,'accepted')
            self.assertEqual(self.session.starts_at,self.start)
            self.assertEqual(self.balance(self.founder),60)
            self.assertEqual(self.session.time_reservations.filter(active=True).count(),2)
            self.assertEqual(self.session.meeting_provisioning['calendar_sync']['code'],code)

    def test_second_provider_read_fences_another_google_edit(self):
        self.client.read_booking_change.side_effect=[self.event,{**self.event,'etag':'v3'}]
        sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at,self.start)
        self.assertEqual(self.session.meeting_provisioning['calendar_sync']['status'],'attention')

    def test_concurrent_app_cancellation_cannot_be_undone_by_sync(self):
        def read(*args):
            MentoringService.cancel(self.session.pk,self.host)
            return self.event
        self.client.read_booking_change.side_effect=read
        sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status,'cancelled')
        self.assertEqual(self.balance(self.founder),100)

    def test_external_conflict_keeps_old_reservations_and_surfaces_attention(self):
        with patch.object(sync.calendars,'check_after_create',side_effect=CalendarError('calendar_conflict')):
            sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at,self.start)
        self.assertEqual(self.session.meeting_provisioning['calendar_sync']['code'],'calendar_conflict')
        self.assertIn('proposed_start',self.session.meeting_provisioning['calendar_sync'])

    def test_sync_worker_selects_a_new_linked_session(self):
        with patch.object(sync,'sync_session') as call:
            self.assertEqual(sync.sync_due_sessions()['processed'],1)
        call.assert_called_once_with(self.session.pk)

    def test_internal_conflict_rolls_back_both_participant_moves(self):
        other=MentorSession.objects.get(pk=self.session.pk)
        other.pk=uuid.uuid4();other.client_request_id=uuid.uuid4()
        other.starts_at=self.start+timedelta(hours=1)
        other.ends_at=other.starts_at+timedelta(minutes=15)
        other.save(force_insert=True)
        MentorTimeReservation.objects.create(session=other,principal=self.host,
            starts_at=other.starts_at,ends_at=other.ends_at)
        sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at,self.start)
        self.assertEqual(self.session.time_reservations.get(principal=self.founder).starts_at,self.start)
        self.assertEqual(self.session.meeting_provisioning['calendar_sync']['code'],'calendar_conflict')

    def test_google_cancellation_after_start_requires_review_without_refund(self):
        MentorSession.objects.filter(pk=self.session.pk).update(
            starts_at=timezone.now()-timedelta(minutes=1),ends_at=timezone.now()+timedelta(minutes=14))
        self.client.read_booking_change.return_value={'id':google.event_id(self.session.pk),'etag':'v3','status':'cancelled'}
        sync.sync_session(self.session.pk)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status,'accepted')
        self.assertEqual(self.balance(self.founder),60)
        self.assertEqual(self.session.meeting_provisioning['calendar_sync']['code'],'calendar_change_after_start')
