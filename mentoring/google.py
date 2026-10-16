"""Small Google client for Vibe Mentoring: Meet links and free/busy.

One MLAI-owned free Google account is the organiser. It authenticates with an
OAuth refresh token kept in the environment. Mentor and mentee Google accounts
never sign in. Plain HTTPS through `requests`; no new dependency.

Settings (all from env, see mlai/settings.py): MENTORING_GOOGLE_ENABLED (False),
MENTORING_GOOGLE_CLIENT_ID, MENTORING_GOOGLE_CLIENT_SECRET,
MENTORING_GOOGLE_REFRESH_TOKEN. Nothing here is called unless enabled.

Every failure is a GoogleError with a short code. Callers store the code and
never let it change a booking.
"""

import hashlib
import logging
import time
from datetime import timezone as dt_timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

TOKEN_URL = 'https://oauth2.googleapis.com/token'
MEET_SPACES_URL = 'https://meet.googleapis.com/v2/spaces'
CALENDAR_BASE = 'https://www.googleapis.com/calendar/v3'
TIMEOUT_SECONDS = 10
BUSY_CACHE_SECONDS = 300
TOKEN_CACHE_KEY = 'mentoring:google:access_token'
PATH_MEET_API = 'meet_api'
PATH_CALENDAR = 'calendar'

# Codes for which the Meet REST API path is replaced by the Calendar fallback.
FALLBACK_CODES = {'permission', 'not_found', 'bad_request', 'unsupported'}


class GoogleError(Exception):
    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code


# ----------------------------------------------------------------- settings

def _setting(name):
    return str(getattr(settings, name, '') or '').strip()


def _staging_blocked():
    """True on isolated staging, where outbound providers are unavailable."""
    try:
        from mlai.staging_integrations import StagingIntegrationUnavailable, require_staging_capability
    except ImportError:
        return False
    try:
        require_staging_capability('google')
    except StagingIntegrationUnavailable:
        return True
    return False


def is_enabled():
    """Whether Google calls may be made at all (flag on, configured, not staging)."""
    if not getattr(settings, 'MENTORING_GOOGLE_ENABLED', False):
        return False
    if not all(_setting(n) for n in (
        'MENTORING_GOOGLE_CLIENT_ID', 'MENTORING_GOOGLE_CLIENT_SECRET', 'MENTORING_GOOGLE_REFRESH_TOKEN',
    )):
        return False
    return not _staging_blocked()


# --------------------------------------------------------------------- http

def _classify(status):
    if status == 401:
        return 'auth'
    if status == 403:
        return 'permission'
    if status in (404, 410):
        return 'not_found'
    if status in (400, 412):
        return 'bad_request'
    if status in (501,):
        return 'unsupported'
    if status == 429:
        return 'rate_limited'
    if status >= 500:
        return 'google_unavailable'
    return 'http_error'


def _send(method, url, **kwargs):
    try:
        response = requests.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
    except requests.RequestException as exc:
        raise GoogleError('network', exc.__class__.__name__)
    if response.status_code >= 400:
        raise GoogleError(_classify(response.status_code), f'HTTP {response.status_code}')
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise GoogleError('bad_response')


def _access_token():
    token = cache.get(TOKEN_CACHE_KEY)
    if token:
        return token
    data = _send('POST', TOKEN_URL, data={
        'client_id': _setting('MENTORING_GOOGLE_CLIENT_ID'),
        'client_secret': _setting('MENTORING_GOOGLE_CLIENT_SECRET'),
        'refresh_token': _setting('MENTORING_GOOGLE_REFRESH_TOKEN'),
        'grant_type': 'refresh_token',
    })
    token = data.get('access_token')
    if not token:
        raise GoogleError('auth', 'no access token')
    try:
        ttl = max(int(data.get('expires_in', 3600)) - 120, 60)
    except (TypeError, ValueError):
        ttl = 600
    cache.set(TOKEN_CACHE_KEY, token, ttl)
    return token


def _api(method, url, **kwargs):
    headers = {'Authorization': f'Bearer {_access_token()}'}
    try:
        return _send(method, url, headers=headers, **kwargs)
    except GoogleError as exc:
        if exc.code == 'auth':
            cache.delete(TOKEN_CACHE_KEY)  # a stale token: next call fetches a new one
        raise


# ------------------------------------------------------------------ meetings

def _event_body(session):
    mentor_user = session.mentor.user
    attendees = [
        {'email': u.email} for u in (mentor_user, session.founder)
        if getattr(u, 'email', '')
    ]
    return {
        'summary': 'MLAI mentoring session',
        'start': {'dateTime': session.starts_at.astimezone(dt_timezone.utc).isoformat()},
        'end': {'dateTime': session.ends_at.astimezone(dt_timezone.utc).isoformat()},
        'attendees': attendees,
        'guestsCanModify': False,
        'guestsCanInviteOthers': False,
    }


def _create_via_meet_api(session):
    space = _api('POST', MEET_SPACES_URL, json={'config': {'accessType': 'OPEN'}})
    link = space.get('meetingUri')
    if not link:
        raise GoogleError('bad_response', 'no meetingUri')
    body = _event_body(session)
    body['location'] = link
    body['description'] = f'Join the session: {link}'
    event_id = ''
    try:
        event = _api('POST', f'{CALENDAR_BASE}/calendars/primary/events',
                     params={'sendUpdates': 'all'}, json=body)
        event_id = event.get('id', '')
    except GoogleError as exc:
        # The link works without the invite; keep it and record the reason.
        logger.warning('mentor session %s: calendar event failed (%s)', session.pk, exc.code)
        return {'meet_link': link, 'calendar_event_id': '', 'path': PATH_MEET_API,
                'error': 'event_' + exc.code}
    return {'meet_link': link, 'calendar_event_id': event_id, 'path': PATH_MEET_API, 'error': ''}


def _create_via_calendar(session):
    body = _event_body(session)
    body['description'] = 'The Google Meet link is attached to this event.'
    body['conferenceData'] = {'createRequest': {
        'requestId': str(session.pk),
        'conferenceSolutionKey': {'type': 'hangoutsMeet'},
    }}
    url = f'{CALENDAR_BASE}/calendars/primary/events'
    event = _api('POST', url, params={'conferenceDataVersion': 1, 'sendUpdates': 'all'}, json=body)
    event_id = event.get('id', '')
    link = event.get('hangoutLink')
    status = ((event.get('conferenceData') or {}).get('createRequest') or {}).get('status', {})
    if not link and status.get('statusCode') == 'pending' and event_id:
        time.sleep(1)  # poll once
        event = _api('GET', f'{url}/{event_id}', params={'conferenceDataVersion': 1})
        link = event.get('hangoutLink')
    if not link:
        raise GoogleError('meet_pending' if status.get('statusCode') == 'pending' else 'no_link')
    return {'meet_link': link, 'calendar_event_id': event_id, 'path': PATH_CALENDAR, 'error': ''}


def create_meeting(session):
    """Create a fresh Meet link and Calendar invite for a session.

    Returns {meet_link, calendar_event_id, path, error}. Raises GoogleError.
    """
    try:
        return _create_via_meet_api(session)
    except GoogleError as exc:
        if exc.code not in FALLBACK_CODES:
            raise
        logger.info('mentor session %s: Meet API refused (%s), using Calendar fallback', session.pk, exc.code)
    return _create_via_calendar(session)


def delete_event(event_id):
    """Cancel the Calendar event (guests are told). A missing event is fine."""
    try:
        _api('DELETE', f'{CALENDAR_BASE}/calendars/primary/events/{event_id}',
             params={'sendUpdates': 'all'})
    except GoogleError as exc:
        if exc.code != 'not_found':
            raise


# ----------------------------------------------------------------- free/busy

def _utc_iso(value):
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(value):
    parsed = parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise GoogleError('bad_response', 'bad time')
    return parsed.astimezone(dt_timezone.utc)


def busy_times(calendar_email, start, end):
    """Busy intervals [(start, end)] (aware UTC) of a calendar shared with the MLAI account.

    Cached per calendar and window for 5 minutes. Raises GoogleError.
    """
    email = (calendar_email or '').strip().lower()
    key = 'mentoring:busy:' + hashlib.sha256(
        f'{email}|{_utc_iso(start)}|{_utc_iso(end)}'.encode()
    ).hexdigest()[:40]
    cached = cache.get(key)
    if cached is not None:
        return [(_parse(a), _parse(b)) for a, b in cached]
    data = _api('POST', f'{CALENDAR_BASE}/freeBusy', json={
        'timeMin': _utc_iso(start), 'timeMax': _utc_iso(end),
        'items': [{'id': email}],
    })
    entry = (data.get('calendars') or {}).get(email)
    if entry is None:
        # Google keys the answer by the requested id; accept any single entry.
        calendars = data.get('calendars') or {}
        entry = next(iter(calendars.values()), None)
    if entry is None:
        raise GoogleError('bad_response', 'no calendar in answer')
    if entry.get('errors'):
        raise GoogleError('calendar_not_shared')
    pairs = [(item['start'], item['end']) for item in entry.get('busy', [])]
    result = [(_parse(a), _parse(b)) for a, b in pairs]
    cache.set(key, pairs, BUSY_CACHE_SECONDS)
    return result
