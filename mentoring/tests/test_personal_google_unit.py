"""Personal Google calendar protocol/security tests; no network or database."""
import base64
import hashlib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import requests
from django.core import signing
from django.core.cache import cache
from django.test import override_settings

from mentoring.calendars import google, service
from mentoring.calendars.errors import CalendarError

START=datetime(2026,11,9,1,0,tzinfo=timezone.utc)
SESSION=SimpleNamespace(pk=uuid.UUID('cfa6cf92-210b-4930-90a3-7a4e92a9a70a'),starts_at=START,
    ends_at=START+timedelta(minutes=15),location_type='mlai_space',
    mentor=SimpleNamespace(user=SimpleNamespace(email='mentor@example.com')),
    founder=SimpleNamespace(email='founder@example.com'))


def reply(status=200,body=None):
    result=MagicMock(status_code=status)
    result.content=b'x' if body is not None else b''
    result.json.return_value=body
    return result


def event(session=SESSION):
    return {'id':google.event_id(session.pk),'start':{'dateTime':google.iso(session.starts_at)},
        'end':{'dateTime':google.iso(session.ends_at)},'etag':'"v1"',
        'extendedProperties':{'private':{'mlai_booking_id':str(session.pk)}}}


class PersonalGoogleProtocolTests(unittest.TestCase):
    def test_deterministic_google_valid_id(self):
        self.assertEqual(google.event_id(SESSION.pk),'mlaimentorcfa6cf92210b493090a37a4e92a9a70a')

    def test_network_write_outcome_is_unknown(self):
        with patch.object(google.requests,'request',side_effect=requests.Timeout), self.assertRaises(CalendarError) as out:
            google.send('POST',google.API+'/freeBusy')
        self.assertTrue(out.exception.uncertain)
        self.assertEqual(out.exception.code,'network')

    def test_read_error_is_not_an_absent_event(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',side_effect=CalendarError('provider_unavailable')),self.assertRaises(CalendarError):
            client.get_booking('calendar',SESSION.pk)

    def test_http_error_never_exposes_provider_body(self):
        with patch.object(google.requests,'request',return_value=reply(401,{'secret':'private provider details'})),self.assertRaises(CalendarError) as out:
            google.send('GET',google.API)
        self.assertEqual(str(out.exception),'reconnect_required')

    def test_requests_do_not_follow_redirects(self):
        with patch.object(google.requests,'request',return_value=reply(200,{})) as call:
            google.send('GET',google.API)
        self.assertFalse(call.call_args.kwargs['allow_redirects'])

    def test_freebusy_unions_and_deduplicates(self):
        busy={'start':google.iso(START),'end':google.iso(START+timedelta(minutes=10))}
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'calendars':{'one':{'busy':[busy]},'two':{'busy':[busy]}}}):
            self.assertEqual(client.fetch_busy(['one','two'],START,START+timedelta(days=1)),[(START,START+timedelta(minutes=10))])

    def test_http200_partial_calendar_error_fails_closed(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'calendars':{'one':{'busy':[]},'two':{'errors':[{'reason':'notFound'}]}}}),self.assertRaises(CalendarError) as out:
            client.fetch_busy(['one','two'],START,START+timedelta(days=1))
        self.assertEqual(out.exception.code,'incomplete_busy_check')

    def test_missing_calendar_is_not_free(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'calendars':{}}),self.assertRaises(CalendarError):
            client.fetch_busy(['one'],START,START+timedelta(days=1))

    def test_naive_provider_busy_times_rejected(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'calendars':{'one':{'busy':[{'start':'2026-11-09T01:00:00','end':'2026-11-09T02:00:00'}]}}}),self.assertRaises(CalendarError):
            client.fetch_busy(['one'],START,START+timedelta(days=1))

    def test_bad_or_unbounded_selection_rejected(self):
        client=google.GoogleCalendarClient('token')
        for ids in ([],[str(i) for i in range(51)]):
            with self.subTest(ids=len(ids)),self.assertRaises(CalendarError):
                client.fetch_busy(ids,START,START+timedelta(days=1))

    def test_list_paginates_and_requires_owned_destination(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',side_effect=[{'items':[{'id':'one','accessRole':'writer'}],'nextPageToken':'p2'},
                {'items':[{'id':'two','accessRole':'owner','conferenceProperties':{'allowedConferenceSolutionTypes':['hangoutsMeet']}}]}]):
            rows=client.list_calendars()
        self.assertFalse(rows[0]['can_write'])
        self.assertTrue(rows[1]['can_write'])
        self.assertTrue(rows[1]['can_create_meet'])

    def test_get_missing_is_authoritative_absence(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',side_effect=CalendarError('not_found')):
            self.assertIsNone(client.get_booking('one',SESSION.pk))

    def test_get_id_collision_is_not_own_event(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'id':google.event_id(SESSION.pk)}),self.assertRaises(CalendarError) as out:
            client.get_booking('one',SESSION.pk)
        self.assertEqual(out.exception.code,'event_identity_mismatch')

    def test_create_has_no_early_attendees_and_stable_identity(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',return_value=None),patch.object(client,'api',return_value=event()) as api:
            result=client.create_booking('cal/name',SESSION)
        self.assertEqual(result['status'],'created')
        self.assertNotIn('attendees',api.call_args.kwargs['json'])
        self.assertEqual(api.call_args.kwargs['params']['sendUpdates'],'none')
        self.assertEqual(api.call_args.kwargs['json']['id'],google.event_id(SESSION.pk))
        self.assertIn('cal%2Fname',api.call_args.args[1])

    def test_duplicate_create_recovers_same_event(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',side_effect=[None,event()]),patch.object(client,'api',side_effect=CalendarError('conflict')):
            self.assertEqual(client.create_booking('one',SESSION)['event_id'],google.event_id(SESSION.pk))

    def test_meet_pending_is_not_confirmation(self):
        video=SimpleNamespace(**{**vars(SESSION),'location_type':'video'})
        self.assertEqual(google.GoogleCalendarClient.booking_result(event(),video)['status'],'pending')

    def test_meet_link_required_and_domain_checked(self):
        video=SimpleNamespace(**{**vars(SESSION),'location_type':'video'})
        row=event()
        row['hangoutLink']='https://attacker.example/meeting'
        self.assertEqual(google.GoogleCalendarClient.booking_result(row,video)['status'],'pending')
        row['hangoutLink']='https://meet.google.com/abc-defg-hij'
        self.assertEqual(google.GoogleCalendarClient.booking_result(row,video)['status'],'created')

    def test_wrong_event_time_is_not_recovered_as_success(self):
        row=event()
        row['start']['dateTime']=google.iso(START+timedelta(minutes=5))
        with self.assertRaises(CalendarError):
            google.GoogleCalendarClient.booking_result(row,SESSION)

    def test_invitation_retry_does_not_send_duplicate_provider_updates(self):
        client=google.GoogleCalendarClient('token')
        row=event()
        row['attendees']=[{'email':'MENTOR@EXAMPLE.COM'},{'email':'founder@example.com'}]
        with patch.object(client,'get_booking',return_value=row),patch.object(client,'api') as api:
            result=client.invite_attendees('one',SESSION)
        self.assertIs(result,row)
        api.assert_not_called()

    def test_postwrite_self_exclusion_checks_identity(self):
        client=google.GoogleCalendarClient('token')
        row=event()
        with patch.object(client,'api',return_value={'items':[row]}):
            client.check_destination_conflicts('one',START,START+timedelta(minutes=15),SESSION.pk)
        row['extendedProperties']['private']['mlai_booking_id']='not-ours'
        with patch.object(client,'api',return_value={'items':[row]}),self.assertRaises(CalendarError):
            client.check_destination_conflicts('one',START,START+timedelta(minutes=15),SESSION.pk)

    def test_postwrite_other_busy_event_blocks(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'api',return_value={'items':[event(),{'id':'other','transparency':'opaque'}]}),self.assertRaises(CalendarError) as out:
            client.check_destination_conflicts('one',START,START+timedelta(minutes=15),SESSION.pk)
        self.assertEqual(out.exception.code,'calendar_conflict')

    def test_delete_uses_etag_and_deterministic_id(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',return_value=event()),patch.object(client,'api',return_value={}) as api:
            result=client.cancel_booking('one',SESSION.pk)
        self.assertEqual(result,{'outcome':'deleted'})
        self.assertEqual(api.call_args.args[0],'DELETE')
        self.assertTrue(api.call_args.args[1].endswith(google.event_id(SESSION.pk)))
        self.assertEqual(api.call_args.kwargs['headers'],{'If-Match':'"v1"'})

    def test_cancel_absent_is_distinct_from_acknowledged_deletion(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',return_value=None),patch.object(client,'api') as api:
            self.assertEqual(client.cancel_booking('one',SESSION.pk),{'outcome':'absent'})
        api.assert_not_called()

    def test_cancel_delete_404_is_not_positive_deletion_acknowledgement(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',return_value=event()),patch.object(client,'api',side_effect=CalendarError('not_found')):
            self.assertEqual(client.cancel_booking('one',SESSION.pk),{'outcome':'absent'})

    def test_attendee_invitation_uses_conditional_update(self):
        client=google.GoogleCalendarClient('token')
        with patch.object(client,'get_booking',return_value=event()),patch.object(client,'api',return_value={}) as api:
            client.invite_attendees('one',SESSION)
        self.assertEqual(api.call_args.kwargs['headers'],{'If-Match':'"v1"'})
        self.assertEqual(len(api.call_args.kwargs['json']['attendees']),2)


class PersonalCalendarOAuthTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(MENTOR_CALENDAR_GOOGLE_CLIENT_ID='cid',MENTOR_CALENDAR_GOOGLE_REDIRECT_URI='https://api.example/callback')
    def start(self):
        owner=SimpleNamespace(pk=7)
        session=SimpleNamespace(user_id=7,pk=uuid.uuid4(),origin='https://chat.example')
        with patch.object(service,'_require_configured'),patch.object(service,'_profile'):
            result=service.begin_authorization(owner,account_session=session)
        return parse_qs(urlparse(result['authorization_url']).query)

    def test_pkce_s256_and_owner_bound_state(self):
        query=self.start()
        data=service._consume_state(query['state'][0])
        verifier=data['verifier']
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'],[challenge])
        self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertEqual(data['user_id'],7)
        self.assertEqual(data['intent'],'mentoring_calendar')
        self.assertEqual(data['origin'],'https://chat.example')

    def test_oauth_nonce_cannot_be_replayed(self):
        query=self.start()
        service._consume_state(query['state'][0])
        with self.assertRaises(CalendarError):
            service._consume_state(query['state'][0])

    def test_signature_tampering_rejected(self):
        state=self.start()['state'][0]
        with self.assertRaises(CalendarError):
            service._consume_state(state+'tampered')

    def test_expired_state_rejected(self):
        state=self.start()['state'][0]
        with patch.object(service.signing,'loads',side_effect=signing.SignatureExpired),self.assertRaises(CalendarError):
            service._consume_state(state)

    def test_arbitrary_return_path_rejected(self):
        owner=SimpleNamespace(pk=7)
        with patch.object(service,'_require_configured'),patch.object(service,'_profile'),self.assertRaises(Exception) as out:
            service.begin_authorization(owner,'https://evil.example')
        self.assertEqual(out.exception.code,'invalid_return_path')

    @override_settings(MENTOR_CALENDAR_GOOGLE_ENABLED=False)
    def test_default_feature_is_not_ready(self):
        self.assertFalse(service.configured())

    @override_settings(MENTOR_CALENDAR_GOOGLE_ENABLED=True,MENTOR_CALENDAR_GOOGLE_CLIENT_ID='cid',
        MENTOR_CALENDAR_GOOGLE_CLIENT_SECRET='secret',MENTOR_CALENDAR_GOOGLE_REDIRECT_URI='https://api.example/callback',
        IS_LOCAL_ENV=False,CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
    def test_production_process_local_oauth_state_is_rejected(self):
        self.assertFalse(service.configured())

    def test_token_refresh_does_not_delete_successor_lock(self):
        connection=SimpleNamespace(pk=2,status='connected',access_token='',access_token_expires_at=None,revision=3)
        fresh=SimpleNamespace(pk=2,status='connected',revision=3,access_token='',access_token_expires_at=None,refresh_token='refresh')
        manager=MagicMock()
        manager.get.return_value=fresh
        manager.filter.return_value.update.return_value=1
        with patch.object(service.MentorCalendarConnection,'objects',manager),patch.object(service.google,'send',return_value={'access_token':'new','expires_in':3600}),patch.object(service.cache,'add',return_value=True),patch.object(service.cache,'delete') as delete:
            self.assertEqual(service._access_token(connection),'new')
        delete.assert_not_called()
        self.assertEqual(connection.access_token,'new')


class PersonalCalendarCallbackTests(unittest.TestCase):
    def run_callback(self, *, existing=None, refresh='fresh-refresh', valid_session=True):
        from contextlib import ExitStack, nullcontext
        user=service.get_user_model()(pk=7,is_active=True)
        model=MagicMock()
        model.objects.filter.return_value.first.return_value=user
        session_model=MagicMock()
        session_model.select_related.return_value.filter.return_value.first.return_value=SimpleNamespace(origin='https://chat.example')
        connection_manager=MagicMock()
        connection_manager.select_for_update.return_value.filter.return_value.first.return_value=existing
        token={'access_token':'access','scope':' '.join(google.SCOPES),'expires_in':3600}
        if refresh:
            token['refresh_token']=refresh
        with ExitStack() as stack:
            stack.enter_context(patch.object(service,'_require_configured'))
            stack.enter_context(patch.object(service,'_consume_state',return_value={'user_id':7,'account_session_id':str(uuid.uuid4()),'origin':'https://chat.example','intent':'mentoring_calendar','verifier':'pkce-verifier'}))
            stack.enter_context(patch.object(service,'_profile'))
            stack.enter_context(patch.object(service,'get_user_model',return_value=model))
            stack.enter_context(patch('community_chat.onboarding.has_community_access',return_value=True))
            stack.enter_context(patch('community_chat.models.CommunityChatAccountSession.objects',session_model))
            stack.enter_context(patch('community_chat.account_sessions._valid_session',return_value=valid_session))
            stack.enter_context(patch.object(service.transaction,'atomic',return_value=nullcontext()))
            stack.enter_context(patch.object(service.MentorCalendarConnection,'objects',connection_manager))
            stack.enter_context(patch.object(service.MentorProfile,'objects',MagicMock()))
            network=stack.enter_context(patch.object(google,'send',side_effect=[token,{'sub':'google-subject','email':'mentor@example.com','email_verified':True}]))
            stack.enter_context(patch.object(google.GoogleCalendarClient,'list_calendars',return_value=[]))
            result=service.finish_authorization('signed-state','private-code')
            return result,network.call_args_list

    def test_expired_or_revoked_original_session_blocks_exchange(self):
        with self.assertRaises(CalendarError) as out:
            self.run_callback(valid_session=False)
        self.assertEqual(out.exception.code,'invalid_account_session')

    def test_reconnect_preserves_refresh_token_for_exact_subject_only(self):
        existing=SimpleNamespace(pk=2,refresh_token='original-refresh',revision=2,save=MagicMock())
        connection,calls=self.run_callback(existing=existing,refresh='')
        self.assertEqual(connection.refresh_token,'original-refresh')
        self.assertEqual(connection.email,'mentor@example.com')
        self.assertEqual(calls[0].kwargs['data']['code_verifier'],'pkce-verifier')
        self.assertEqual(connection.revision,3)

    def test_new_identity_cannot_borrow_another_identity_refresh_token(self):
        with self.assertRaises(CalendarError) as out:
            self.run_callback(existing=None,refresh='')
        self.assertEqual(out.exception.code,'offline_access_required')


class PersonalCalendarSelectionTests(unittest.TestCase):
    def test_disconnect_removes_only_future_selection_not_saved_booking_references(self):
        from contextlib import nullcontext
        owner=SimpleNamespace(pk=7)
        connection=SimpleNamespace(pk=3,status='connected',conflict_calendar_ids=['old'],booking_calendar_id='old',
            checked_at=START,refresh_token='refresh',access_token='access',access_token_expires_at=START,
            last_error_code='',revision=1,save=MagicMock())
        manager=MagicMock()
        manager.select_for_update.return_value.get.return_value=connection
        with patch.object(service,'_owned',return_value=connection),patch.object(service,'connections',return_value={'connections':[]}),patch.object(service.transaction,'atomic',return_value=nullcontext()),patch.object(service,'get_user_model',return_value=MagicMock()),patch.object(service.MentorCalendarConnection,'objects',manager),patch.object(service.MentorProfile,'objects',MagicMock()):
            service.disconnect(owner,3)
        self.assertEqual(connection.status,'disconnected')
        self.assertEqual(connection.conflict_calendar_ids,[])
        self.assertEqual(connection.booking_calendar_id,'')
        self.assertEqual(connection.refresh_token,'')
        self.assertEqual(connection.access_token,'')
        self.assertEqual(connection.revision,2)


class PersonalCalendarMaintenanceTests(unittest.TestCase):
    def test_cancel_old_snapshot_after_same_identity_reconnect(self):
        owner=SimpleNamespace(user_id=7)
        session=SimpleNamespace(pk=SESSION.pk,mentor=owner,calendar_snapshot={'connection_id':2,'calendar_id':'original-calendar','revision':1})
        connection=SimpleNamespace(pk=2,user_id=7,provider='google',status='connected',revision=99)
        manager=MagicMock()
        manager.filter.return_value.first.return_value=connection
        client=MagicMock()
        client.cancel_booking.return_value={'outcome':'deleted'}
        with patch.object(service.MentorCalendarConnection,'objects',manager),patch.object(service,'_client',return_value=client) as factory:
            result=service.cancel_booking(session)
        self.assertEqual(result,{'outcome':'deleted'})
        manager.filter.assert_called_once_with(pk=2,user_id=7,provider='google')
        factory.assert_called_once_with(connection,maintenance=True)
        client.cancel_booking.assert_called_once_with('original-calendar',SESSION.pk)
