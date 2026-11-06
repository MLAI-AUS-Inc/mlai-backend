"""Provider identity, deletion and frozen-term guards without database access."""
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import uuid

from django.utils import timezone
from mentoring import calendar_sync as sync
from mentoring.calendars import google
from mentoring.calendars.errors import CalendarError


class CalendarSyncUnitTests(TestCase):
    def setUp(self):
        start = timezone.now() + timedelta(days=5)
        self.session = SimpleNamespace(pk=uuid.uuid4(), starts_at=start,
            ends_at=start+timedelta(minutes=15), location_type='video', calendar_snapshot={},
            calendar_event_id='', meeting_provisioning={})
        self.event = {'id':google.event_id(self.session.pk), 'etag':'v2', 'status':'confirmed',
            'start':{'dateTime':google.iso(start+timedelta(hours=1))},
            'end':{'dateTime':google.iso(start+timedelta(hours=1,minutes=15))},
            'hangoutLink':'https://meet.google.com/abc-defg-hij',
            'extendedProperties':{'private':{'mlai_booking_id':str(self.session.pk)}}}

    def test_move_preserves_duration_and_requires_bound_identity(self):
        self.assertEqual(sync.event_change(self.session,self.event)['start'],self.session.starts_at+timedelta(hours=1))
        self.event['extendedProperties'] = {}
        with self.assertRaises(CalendarError):sync.event_change(self.session,self.event)

    def test_explicit_deleted_tombstone_retains_cancellation(self):
        row={'id':google.event_id(self.session.pk),'status':'cancelled','etag':'v3'}
        client=google.GoogleCalendarClient('synthetic')
        with patch.object(client,'api',return_value=row):
            self.assertTrue(sync.event_change(self.session,client.read_booking_change('calendar',self.session.pk))['cancelled'])

    def test_404_is_never_a_cancellation(self):
        client=google.GoogleCalendarClient('synthetic')
        with patch.object(client,'api',side_effect=CalendarError('not_found')),self.assertRaises(CalendarError):
            client.read_booking_change('calendar',self.session.pk)

    def test_minimal_deletion_tombstone_does_not_require_removed_metadata(self):
        row={'id':google.event_id(self.session.pk),'status':'cancelled'}
        self.assertTrue(sync.event_change(self.session,row)['cancelled'])

    def test_all_day_recurrence_and_duration_changes_need_attention(self):
        for changes in ({'start':{'date':'2030-01-01'}},{'recurrence':['RRULE:FREQ=DAILY']},
                        {'end':{'dateTime':google.iso(self.session.ends_at+timedelta(hours=2))}}):
            with self.subTest(changes=changes),self.assertRaises(CalendarError):
                sync.event_change(self.session,{**self.event,**changes})

    def test_missing_or_foreign_meet_link_cannot_replace_verified_link(self):
        with self.assertRaises(CalendarError):
            sync.event_change(self.session,{**self.event,'hangoutLink':'https://example.invalid/meet'})

    def test_stale_sync_cannot_automatically_settle(self):
        self.session.calendar_snapshot={'connection_id':1}
        self.session.calendar_event_id=google.event_id(self.session.pk)
        self.assertFalse(sync.settlement_ready(self.session))
        self.session.meeting_provisioning={'calendar_sync':{'status':'synced','checked_at':timezone.now().isoformat()}}
        self.assertTrue(sync.settlement_ready(self.session))
        self.session.meeting_provisioning['calendar_sync']['claim']='active'
        self.assertFalse(sync.settlement_ready(self.session))

    def test_public_status_does_not_leak_event_ids_or_lease_claims(self):
        self.session.calendar_snapshot={'connection_id':1}
        self.session.calendar_event_id=google.event_id(self.session.pk)
        self.session.meeting_provisioning={'calendar_sync':{'status':'attention','claim':'secret','etag':'private','code':'calendar_conflict'}}
        self.assertEqual(sync.public_status(self.session),{'status':'attention','code':'calendar_conflict'})
