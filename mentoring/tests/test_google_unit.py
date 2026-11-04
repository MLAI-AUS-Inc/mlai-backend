"""Google client with `requests` mocked. No network, no database, no secrets."""
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from django.core.cache import cache
from django.test import override_settings

from mentoring import google

START = datetime(2026, 11, 9, 1, 0, tzinfo=tz.utc)
SESSION = SimpleNamespace(
    pk=uuid.uuid4(), starts_at=START, ends_at=START + timedelta(minutes=30),
    mentor=SimpleNamespace(user=SimpleNamespace(email='mia@example.com')),
    founder=SimpleNamespace(email='fi@example.com'),
)
EVENT_ID = google.event_id_for(SESSION)
CONFIG = dict(
    MENTORING_GOOGLE_ENABLED=True, MENTORING_GOOGLE_CLIENT_ID='cid',
    MENTORING_GOOGLE_CLIENT_SECRET='csecret', MENTORING_GOOGLE_REFRESH_TOKEN='rtoken',
)


def reply(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.content = b'x' if body is not None else b''
    r.json.return_value = body
    return r


class FakeGoogle:
    """Routes mocked HTTP calls by URL and records them."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, fragment), answers in self.routes.items():
            if m == method and fragment in url:
                answer = answers.pop(0) if isinstance(answers, list) and len(answers) > 1 else (
                    answers[0] if isinstance(answers, list) else answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f'unexpected call {method} {url}')

    def to(self, fragment):
        return [c for c in self.calls if fragment in c[1]]


def posts(fake):
    return [c for c in fake.to('/events') if c[0] == 'POST']


class GoogleClientTests(unittest.TestCase):
    def setUp(self):
        override = override_settings(**CONFIG)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        self.addCleanup(cache.clear)
        patch.object(google.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def run_with(self, routes):
        routes.setdefault(('POST', 'oauth2.googleapis.com'), [reply(200, {'access_token': 't', 'expires_in': 3600})])
        routes.setdefault(('GET', '/events/' + EVENT_ID), [reply(404, {})])  # no event from an earlier attempt
        fake = FakeGoogle(routes)
        patch.object(google.requests, 'request', fake).start()
        return fake

    def test_enabled_needs_flag_and_all_three_values(self):
        self.assertTrue(google.is_enabled())
        with override_settings(MENTORING_GOOGLE_ENABLED=False):
            self.assertFalse(google.is_enabled())
        with override_settings(MENTORING_GOOGLE_REFRESH_TOKEN=''):
            self.assertFalse(google.is_enabled())

    def test_disabled_on_isolated_staging(self):
        class Unavailable(Exception):
            pass
        fake = MagicMock(StagingIntegrationUnavailable=Unavailable,
                         require_staging_capability=MagicMock(side_effect=Unavailable('google')))
        with patch.dict('sys.modules', {'mlai.staging_integrations': fake}):
            self.assertFalse(google.is_enabled())
        fake.require_staging_capability.assert_called_once_with('google')

    def test_primary_path_uses_meet_api_then_creates_the_invite(self):
        fake = self.run_with({
            ('POST', 'meet.googleapis.com/v2/spaces'): [reply(200, {'meetingUri': 'https://meet.google.com/abc'})],
            ('POST', '/calendars/primary/events'): [reply(200, {'id': 'ev1'})],
        })
        out = google.create_meeting(SESSION)
        self.assertEqual(out, {'meet_link': 'https://meet.google.com/abc', 'calendar_event_id': 'ev1',
                               'path': 'meet_api', 'error': ''})
        self.assertEqual(fake.to('v2/spaces')[0][2]['json'], {'config': {'accessType': 'OPEN'}})
        kw = posts(fake)[0][2]
        self.assertEqual(kw['params'], {'sendUpdates': 'all'})
        self.assertEqual(kw['json']['location'], 'https://meet.google.com/abc')
        self.assertIn('https://meet.google.com/abc', kw['json']['description'])
        self.assertEqual([a['email'] for a in kw['json']['attendees']], ['mia@example.com', 'fi@example.com'])
        self.assertEqual(kw['json']['start']['dateTime'], '2026-11-09T01:00:00+00:00')
        self.assertEqual(kw['headers'], {'Authorization': 'Bearer t'})

    def test_meet_ok_but_invite_fails_keeps_the_link(self):
        self.run_with({
            ('POST', 'v2/spaces'): [reply(200, {'meetingUri': 'https://meet.google.com/abc'})],
            ('POST', '/events'): [reply(403, {})],
        })
        out = google.create_meeting(SESSION)
        self.assertEqual((out['meet_link'], out['calendar_event_id'], out['error']),
                         ('https://meet.google.com/abc', '', 'event_permission'))

    def test_permission_error_falls_back_to_calendar_conference(self):
        fake = self.run_with({
            ('POST', 'v2/spaces'): [reply(403, {})],
            ('POST', '/events'): [reply(200, {'id': 'ev2', 'hangoutLink': 'https://meet.google.com/zzz'})],
        })
        out = google.create_meeting(SESSION)
        self.assertEqual((out['path'], out['meet_link'], out['calendar_event_id']),
                         ('calendar', 'https://meet.google.com/zzz', 'ev2'))
        kw = posts(fake)[0][2]
        self.assertEqual(kw['params'], {'conferenceDataVersion': 1, 'sendUpdates': 'all'})
        create = kw['json']['conferenceData']['createRequest']
        self.assertEqual(create['requestId'], str(SESSION.pk))
        self.assertEqual(create['conferenceSolutionKey'], {'type': 'hangoutsMeet'})

    def test_fallback_polls_once_when_pending(self):
        fake = self.run_with({
            ('POST', 'v2/spaces'): [reply(400, {})],
            ('POST', '/events'): [reply(200, {'id': 'ev3', 'conferenceData': {'createRequest': {'status': {'statusCode': 'pending'}}}})],
            ('GET', '/events/ev3'): [reply(200, {'id': 'ev3', 'hangoutLink': 'https://meet.google.com/late'})],
        })
        self.assertEqual(google.create_meeting(SESSION)['meet_link'], 'https://meet.google.com/late')
        self.assertEqual(len(fake.to('/events/ev3')), 1)

    def test_fallback_still_pending_is_an_error(self):
        self.run_with({
            ('POST', 'v2/spaces'): [reply(403, {})],
            ('POST', '/events'): [reply(200, {'id': 'ev3', 'conferenceData': {'createRequest': {'status': {'statusCode': 'pending'}}}})],
            ('GET', '/events/ev3'): [reply(200, {'id': 'ev3'})],
        })
        with self.assertRaises(google.GoogleError) as ctx:
            google.create_meeting(SESSION)
        self.assertEqual(ctx.exception.code, 'meet_pending')

    def test_network_and_server_errors_do_not_fall_back(self):
        fake = self.run_with({('POST', 'v2/spaces'): [reply(503, {})]})
        with self.assertRaises(google.GoogleError) as ctx:
            google.create_meeting(SESSION)
        self.assertEqual(ctx.exception.code, 'google_unavailable')
        self.assertEqual(posts(fake), [])

    def test_requests_errors_become_a_network_code(self):
        self.run_with({('POST', 'v2/spaces'): [google.requests.ConnectionError('down')]})
        with self.assertRaises(google.GoogleError) as ctx:
            google.create_meeting(SESSION)
        self.assertEqual(ctx.exception.code, 'network')

    def test_access_token_is_cached_and_dropped_on_401(self):
        fake = self.run_with({('POST', 'v2/spaces'): [reply(401, {}), reply(401, {})]})
        for _ in range(2):
            with self.assertRaises(google.GoogleError):
                google.create_meeting(SESSION)
        self.assertEqual(len(fake.to('oauth2')), 2)  # dropped after each 401, so fetched again
        cache.clear()
        fake = self.run_with({('POST', 'v2/spaces'): [reply(200, {'meetingUri': 'u'})],
                              ('POST', '/events'): [reply(200, {'id': 'e'})]})
        google.create_meeting(SESSION)
        google.create_meeting(SESSION)
        self.assertEqual(len(fake.to('oauth2')), 1)

    def test_event_id_is_deterministic_and_valid_for_google(self):
        eid = google.event_id_for(SESSION)
        self.assertEqual(eid, google.event_id_for(SESSION))
        self.assertRegex(eid, r'^[a-v0-9]{5,1024}$')

    def test_event_is_inserted_under_the_fixed_id_and_announced_first(self):
        fake = self.run_with({
            ('POST', 'v2/spaces'): [reply(200, {'meetingUri': 'https://meet.google.com/abc'})],
            ('POST', '/events'): [reply(200, {'id': EVENT_ID})],
        })
        seen = []
        out = google.create_meeting(SESSION, on_event=seen.append)
        self.assertEqual(seen, [EVENT_ID])
        self.assertEqual(out['calendar_event_id'], EVENT_ID)
        self.assertEqual(posts(fake)[0][2]['json']['id'], EVENT_ID)

    def test_retry_after_a_lost_insert_reuses_the_event_and_inserts_nothing(self):
        existing = {'id': EVENT_ID, 'location': 'https://meet.google.com/first'}
        fake = self.run_with({('GET', '/events/' + EVENT_ID): [reply(200, existing)]})
        out = google.create_meeting(SESSION)
        self.assertEqual((out['meet_link'], out['calendar_event_id'], out['path']),
                         ('https://meet.google.com/first', EVENT_ID, 'meet_api'))
        self.assertEqual(posts(fake), [])
        self.assertEqual(fake.to('v2/spaces'), [])

    def test_retry_reuses_a_calendar_event_whose_link_arrived_later(self):
        existing = {'id': EVENT_ID, 'hangoutLink': 'https://meet.google.com/late'}
        fake = self.run_with({('GET', '/events/' + EVENT_ID): [reply(200, existing)]})
        out = google.create_meeting(SESSION)
        self.assertEqual((out['meet_link'], out['path']), ('https://meet.google.com/late', 'calendar'))
        self.assertEqual(posts(fake), [])

    def test_pending_event_keeps_its_id_and_the_retry_does_not_insert_again(self):
        pending = {'id': EVENT_ID, 'conferenceData': {'createRequest': {'status': {'statusCode': 'pending'}}}}
        fake = self.run_with({
            ('POST', 'v2/spaces'): [reply(403, {})],
            ('GET', '/events/' + EVENT_ID): [reply(404, {}), reply(200, pending), reply(200, pending),
                                             reply(200, {'id': EVENT_ID, 'hangoutLink': 'https://meet.google.com/ok'})],
            ('POST', '/events'): [reply(200, pending)],
        })
        seen = []
        with self.assertRaises(google.GoogleError) as ctx:
            google.create_meeting(SESSION, on_event=seen.append)
        self.assertEqual((ctx.exception.code, seen), ('meet_pending', [EVENT_ID]))
        out = google.create_meeting(SESSION)  # the retry: finds the event, polls, gets the link
        self.assertEqual(out['meet_link'], 'https://meet.google.com/ok')
        self.assertEqual(len(posts(fake)), 1)

    def test_insert_conflict_falls_back_to_the_existing_event(self):
        existing = {'id': EVENT_ID, 'location': 'https://meet.google.com/won'}
        self.run_with({
            ('POST', 'v2/spaces'): [reply(200, {'meetingUri': 'https://meet.google.com/mine'})],
            ('POST', '/events'): [reply(409, {})],
            ('GET', '/events/' + EVENT_ID): [reply(404, {}), reply(200, existing)],
        })
        self.assertEqual(google.create_meeting(SESSION)['meet_link'], 'https://meet.google.com/won')

    def test_delete_event_ignores_missing_events(self):
        fake = self.run_with({('DELETE', '/events/ev1'): [reply(410, None)]})
        google.delete_event('ev1')
        self.assertEqual(fake.to('/events/ev1')[0][2]['params'], {'sendUpdates': 'all'})
        self.run_with({('DELETE', '/events/ev1'): [reply(500, None)]})
        with self.assertRaises(google.GoogleError):
            google.delete_event('ev1')

    def test_busy_times_parse_and_cache_for_five_minutes(self):
        body = {'calendars': {'mia@gmail.com': {'busy': [
            {'start': '2026-11-09T01:00:00Z', 'end': '2026-11-09T02:00:00Z'}]}}}
        fake = self.run_with({('POST', '/freeBusy'): [reply(200, body)]})
        a, b = START, START + timedelta(days=2)
        first = google.busy_times(' Mia@Gmail.com ', a, b)
        second = google.busy_times('mia@gmail.com', a, b)
        self.assertEqual(first, second)
        self.assertEqual(first, [(START, START + timedelta(hours=1))])
        self.assertEqual(len(fake.to('freeBusy')), 1)
        self.assertEqual(fake.to('freeBusy')[0][2]['json']['items'], [{'id': 'mia@gmail.com'}])

    def test_unshared_calendar_is_an_error_and_not_cached(self):
        body = {'calendars': {'mia@gmail.com': {'errors': [{'reason': 'notFound'}], 'busy': []}}}
        fake = self.run_with({('POST', '/freeBusy'): [reply(200, body)]})
        for _ in range(2):
            with self.assertRaises(google.GoogleError) as ctx:
                google.busy_times('mia@gmail.com', START, START + timedelta(days=1))
            self.assertEqual(ctx.exception.code, 'calendar_not_shared')
        self.assertEqual(len(fake.to('freeBusy')), 2)


if __name__ == '__main__':
    unittest.main()
