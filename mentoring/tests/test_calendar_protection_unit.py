"""Incident and Meet configuration regressions with no database/network access."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mentoring import conflicts, meeting_policy
from mentoring.calendars import google, service
from mentoring.calendars.errors import CalendarError
from mentoring.service import MentoringError, MentoringService
from mentoring import meeting_repair

START = datetime(2026, 11, 8, 22, tzinfo=timezone.utc)  # Mon 09:00 AEDT
END = START + timedelta(hours=9)


class CalendarProtectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = SimpleNamespace(pk=1, user_id=7, booking_revision=3,
            booking_mode='legacy_request', busy_calendar_email='', timezone='Australia/Melbourne',
            buffer_before_minutes=15, buffer_after_minutes=15)
        self.manager = MagicMock()
        p = patch.object(conflicts.MentorCalendarConnection, 'objects', self.manager)
        p.start(); self.addCleanup(p.stop)

    def rows(self, *rows):
        self.manager.filter.return_value.__iter__.side_effect = lambda: iter(rows)

    def test_legacy_mode_reads_selected_personal_calendar(self):
        self.rows(SimpleNamespace(conflict_calendar_ids=['work', 'personal'], booking_calendar_id='work'))
        with patch.object(service, 'fetch_busy', return_value=[(START, END)]) as busy:
            self.assertEqual(conflicts.read(self.profile, START, END), [(START, END)])
        busy.assert_called_once_with(self.profile, START, END)

    def test_disconnect_history_cannot_fall_back_to_old_organizer(self):
        self.rows(SimpleNamespace(conflict_calendar_ids=[], booking_calendar_id=''))
        self.profile.busy_calendar_email = 'legacy@example.invalid'
        with patch('mentoring.google.busy_times') as old, self.assertRaises(CalendarError):
            conflicts.read(self.profile, START, END)
        old.assert_not_called()

    def test_destination_must_also_be_checked(self):
        self.rows(SimpleNamespace(conflict_calendar_ids=['personal'], booking_calendar_id='work'))
        with self.assertRaises(CalendarError):
            conflicts.read(self.profile, START, END)

    def test_no_verified_calendar_is_unavailable(self):
        self.rows()
        with self.assertRaises(CalendarError):
            conflicts.read(self.profile, START, END)

    def test_legacy_provider_reads_are_fresh(self):
        self.rows()
        self.profile.busy_calendar_email = 'legacy@example.invalid'
        with patch('mentoring.google.is_enabled', return_value=True), patch('mentoring.google.busy_times', return_value=[]) as old:
            self.assertEqual(conflicts.read(self.profile, START, END), [])
        self.assertTrue(old.call_args.kwargs['fresh'])

    def test_melbourne_day_window_includes_buffers_and_previous_utc_day(self):
        day = datetime(2026, 11, 9).date()
        start, end = conflicts.window(self.profile, day, day)
        self.assertEqual(start.isoformat(), '2026-11-08T12:45:00+00:00')
        self.assertEqual(end.isoformat(), '2026-11-09T13:15:00+00:00')

    def test_configuration_change_and_old_evidence_block_admission(self):
        with patch.object(conflicts, 'fingerprint', return_value='current'):
            for config, checked in [('old', START), ('current', START)]:
                with self.subTest(config=config), self.assertRaises(MentoringError):
                    conflicts.validate(self.profile, {'config': config, 'checked_at': checked})

    def test_incident_day_has_no_legacy_slots_and_failed_check_is_closed(self):
        self.profile.session_minutes = 15
        self.profile.price_roo = 60
        self.profile.is_active = True
        day = datetime(2026, 11, 9).date()
        with patch.object(MentoringService, '_get_active_mentor', return_value=self.profile), \
             patch.object(MentoringService, 'sweep'), \
             patch.object(MentoringService, '_google_busy', return_value=([], 'failed')), \
             patch('mentoring.service.MentorSession.objects'):
            self.profile.availability_rules = MagicMock()
            response = MentoringService.availability(1, day, day)
        self.assertEqual(response['slots'], [])
        self.assertNotEqual(response['availability_status'], 'verified')


class MeetCaptureTests(unittest.TestCase):
    link = 'https://meet.google.com/abc-defg-hij'
    def space(self, enabled=True):
        return {'name': 'spaces/Canonical_123', 'meetingUri': self.link, 'config': {'artifactConfig': {
            'transcriptionConfig': {'autoTranscriptionGeneration': 'ON' if enabled else 'OFF'},
            'smartNotesConfig': {'autoSmartNotesGeneration': 'ON' if enabled else 'OFF'},
        }}}

    def test_both_features_patched_on_same_space_and_read_back(self):
        client = google.GoogleCalendarClient('synthetic')
        with patch.object(client, 'meet_api', side_effect=[self.space(False), {}, self.space()]) as api:
            result = client.configure_capture(self.link)
        self.assertTrue(result['verified'])
        patch_call = api.call_args_list[1]
        self.assertEqual(patch_call.args, ('PATCH', '/spaces/Canonical_123'))
        self.assertNotIn('*', patch_call.kwargs['params']['updateMask'])
        self.assertNotIn('recordingConfig', patch_call.kwargs['json']['config']['artifactConfig'])
        self.assertEqual(api.call_args_list[-1].args, ('GET', '/spaces/Canonical_123'))

    def test_retry_after_lost_patch_reads_enabled_settings_without_duplicate_patch(self):
        client = google.GoogleCalendarClient('synthetic')
        with patch.object(client, 'meet_api', side_effect=[self.space(), self.space()]) as api:
            client.configure_capture(self.link)
        self.assertEqual([c.args[0] for c in api.call_args_list], ['GET', 'GET'])

    def test_readback_off_or_other_meeting_cannot_confirm(self):
        client = google.GoogleCalendarClient('synthetic')
        for last in [self.space(False), {**self.space(), 'meetingUri': self.link + 'other'}]:
            with patch.object(client, 'meet_api', side_effect=[self.space(False), {}, last]), self.assertRaises(CalendarError):
                client.configure_capture(self.link)

    def test_foreign_url_or_invalid_meet_code_is_rejected_before_network(self):
        client = google.GoogleCalendarClient('synthetic')
        for link in ['https://attacker.invalid/abc-defg-hij', self.link + '?redirect=x', 'https://meet.google.com/../spaces/x']:
            with patch.object(client, 'meet_api') as api, self.assertRaises(CalendarError):
                client.configure_capture(link)
            api.assert_not_called()

    def test_calendar_scope_set_remains_independent_of_new_meet_permission(self):
        self.assertIn(google.MEET_SETTINGS_SCOPE, google.SCOPES)
        self.assertNotIn(google.MEET_SETTINGS_SCOPE, google.CALENDAR_SCOPES)

    def test_unknown_policy_cannot_silently_disable_required_capture(self):
        with self.assertRaises(MentoringError):
            meeting_policy.validate_policy({'version': 2})
        with self.assertRaises(CalendarError):
            meeting_policy.required(SimpleNamespace(location_type='video', booking_snapshot={'meeting_artifact_policy': {'bad': True}}))
        self.assertFalse(meeting_policy.required(SimpleNamespace(location_type='video', booking_snapshot={})))


class LegacyMeetingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(pk=uuid.uuid4(), status='accepted', location_type='video',
            starts_at=datetime.now(timezone.utc)+timedelta(days=3), booking_mode='legacy_request',
            mentor=SimpleNamespace(booking_mode='instant'), meeting_status='unavailable',
            meeting_error='google_off', meet_link='', calendar_event_id='', calendar_snapshot={},
            meeting_path='', meeting_provisioning={})
        self.manager = MagicMock()
        self.manager.select_related.return_value.get.return_value = self.session
        patcher = patch.object(meeting_repair.MentorSession, 'objects', self.manager)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_skipped_legacy_booking_uses_verified_owner_target(self):
        target = {'connection_id': 7, 'calendar_id': 'owner', 'revision': 2}
        with patch.object(meeting_repair.calendars, 'calendar_target', return_value=target) as verify, \
                patch.object(meeting_repair, 'prepare') as prepare:
            meeting_repair.recover_unlinked(self.session.pk)
        verify.assert_called_once_with(self.session.mentor, verify=True)
        prepare.assert_called_once_with(self.session.pk, connection_id=7, calendar_id='owner', automatic=True)

    def test_ambiguous_or_ineligible_bookings_never_adopt_a_new_calendar(self):
        for field, value in [('status','cancelled'), ('location_type','mlai_space'),
                ('starts_at',datetime.now(timezone.utc)-timedelta(seconds=1)), ('booking_mode','instant'),
                ('meeting_status','failed'), ('meeting_error','timeout'),
                ('meet_link','https://meet.google.com/abc-defg-hij'), ('calendar_event_id','legacy-id'),
                ('calendar_snapshot',{'connection_id': 9}), ('meeting_path','calendar'),
                ('meeting_provisioning',{'unknown_create':True})]:
            original = getattr(self.session, field)
            with self.subTest(field=field), patch.object(meeting_repair.calendars, 'calendar_target') as target:
                setattr(self.session, field, value)
                meeting_repair.recover_unlinked(self.session.pk)
                target.assert_not_called()
            setattr(self.session, field, original)
        self.session.mentor.booking_mode = 'legacy_request'
        self.assertFalse(meeting_repair.automatically_repairable(self.session))

    def test_missing_permission_defers_without_changing_charge_or_status(self):
        with patch.object(meeting_repair.calendars, 'calendar_target', side_effect=CalendarError('meet_reconnect_required')), \
                patch.object(meeting_repair, 'prepare') as prepare:
            meeting_repair.recover_unlinked(self.session.pk)
        prepare.assert_not_called()
        update = self.manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(set(update), {'meeting_claimed_at', 'updated_at'})
        self.assertEqual(self.manager.filter.call_args.kwargs['meeting_provisioning'], {})

    def test_cancellation_during_readiness_check_cannot_prepare_repair(self):
        initial = self.session
        cancelled = SimpleNamespace(**vars(initial)); cancelled.status = 'cancelled'
        self.manager.select_for_update.return_value.select_related.return_value.get.return_value = cancelled
        target = {'connection_id': 7, 'calendar_id': 'owner', 'revision': 2}
        with patch.object(meeting_repair.transaction, 'atomic'), patch.object(meeting_repair, '_lock_users'), \
                patch.object(meeting_repair.calendars, 'calendar_target', return_value=target), self.assertRaises(CalendarError):
            initial.founder_id = 2; initial.mentor.user_id = 7
            meeting_repair.prepare(initial.pk, connection_id=7, calendar_id='owner', automatic=True)


class RepairedMeetingCleanupTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(pk=uuid.uuid4(), status='cancelled', founder_id=2,
            mentor=SimpleNamespace(user_id=7), meeting_claimed_at=None,
            meeting_provisioning={'repair': True, 'unknown_create': True,
                                 'cleanup_pending': True, 'verified': True},
            meet_link='https://meet.google.com/abc-defg-hij', notification_status='sent',
            save=MagicMock(), time_reservations=MagicMock())
        self.session.calendar_event_id = google.event_id(self.session.pk)
        self.manager = MagicMock()
        self.manager.select_related.return_value.get.return_value = self.session
        self.manager.select_for_update.return_value.select_related.return_value.get.return_value = self.session
        self.manager.get.return_value = self.session
        self.manager.filter.return_value.update.return_value = 1
        for target, attribute, options in (
            (meeting_repair.MentorSession, 'objects', {'new': self.manager}),
            (meeting_repair.transaction, 'atomic', {}),
            (meeting_repair, '_lock_users', {}),
            (meeting_repair.calendars, 'cancel_booking', {'return_value': {'outcome': 'absent'}}),
        ):
            patcher = patch.object(target, attribute, **options)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_confirmed_repair_deleted_in_google_releases_old_uncertainty(self):
        meeting_repair.process(self.session.pk)
        self.manager.filter.return_value.update.assert_called_once()
        state = self.manager.filter.return_value.update.call_args.kwargs['meeting_provisioning']
        self.assertFalse(state['cleanup_pending'])
        self.assertFalse(state['unknown_create'])
        self.session.time_reservations.update.assert_called_once_with(active=False)

    def test_unconfirmed_unknown_create_still_keeps_quarantine_on_absence(self):
        self.session.calendar_event_id = ''
        self.session.meeting_provisioning['verified'] = False
        meeting_repair.process(self.session.pk)
        self.manager.filter.return_value.update.assert_not_called()
        self.session.time_reservations.update.assert_not_called()

    def test_partial_or_mismatched_creation_evidence_keeps_quarantine(self):
        for field, value in (('calendar_event_id', 'unrelated'), ('meet_link', ''), ('verified', False)):
            with self.subTest(field=field):
                self.session.meeting_claimed_at = None
                self.session.calendar_event_id = google.event_id(self.session.pk)
                self.session.meet_link = 'https://meet.google.com/abc-defg-hij'
                self.session.meeting_provisioning['verified'] = True
                if field == 'verified':
                    self.session.meeting_provisioning[field] = value
                else:
                    setattr(self.session, field, value)
                meeting_repair.process(self.session.pk)
                self.manager.filter.return_value.update.assert_not_called()
                self.session.time_reservations.update.assert_not_called()

    def test_provider_failure_cannot_release_cancelled_reservations(self):
        with patch.object(meeting_repair.calendars, 'cancel_booking', side_effect=CalendarError('permission')):
            meeting_repair.process(self.session.pk)
        self.session.time_reservations.update.assert_not_called()
        self.assertEqual(self.manager.filter.return_value.update.call_args.kwargs,
                         {'meeting_status': 'failed', 'meeting_error': 'permission'})

    def test_successful_repair_clears_create_uncertainty(self):
        self.session.status = 'accepted'
        self.session.meet_link = ''
        client = MagicMock()
        client.configure_capture.return_value = {'verified': True}
        result = {'status': 'created', 'event_id': google.event_id(self.session.pk),
                  'meet_link': 'https://meet.google.com/abc-defg-hij'}
        with patch.object(meeting_repair.calendars, 'lookup_booking', return_value=result), \
             patch.object(meeting_repair.calendars, '_target', return_value=(
                 SimpleNamespace(scopes={google.MEET_SETTINGS_SCOPE}), 'synthetic-calendar')), \
             patch.object(meeting_repair.calendars, '_client', return_value=client), \
             patch.object(meeting_repair.calendars, '_access_token'), \
             patch.object(meeting_repair.calendars, 'verify_booking'), \
             patch.object(meeting_repair.calendars, 'invite_attendees'):
            meeting_repair.process(self.session.pk)
        state = self.manager.filter.return_value.update.call_args.kwargs['meeting_provisioning']
        self.assertTrue(state['verified'])
        self.assertFalse(state['unknown_create'])
