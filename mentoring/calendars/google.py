"""Bounded Google Calendar API adapter using a mentor's own OAuth grant."""
import uuid
from datetime import timezone
from urllib.parse import quote, urlparse

import requests
from django.utils.dateparse import parse_datetime

from .errors import CalendarError

API = 'https://www.googleapis.com/calendar/v3'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
SCOPES = (
    'openid', 'email',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
    'https://www.googleapis.com/auth/calendar.events.freebusy',
    'https://www.googleapis.com/auth/calendar.events.owned',
)
CALENDAR_SCOPES = frozenset(SCOPES[2:])
TIMEOUT = (3.05, 10)


def send(method, url, **kwargs):
    """Classify provider failures without retaining credentials or response text."""
    try:
        response = requests.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
    except requests.RequestException as exc:
        raise CalendarError('network', uncertain=method not in ('GET', 'HEAD')) from exc
    status = response.status_code
    if not 200 <= status < 300:
        code = {400:'invalid_request', 401:'reconnect_required', 403:'permission', 404:'not_found',
                409:'conflict', 410:'not_found', 412:'version_conflict', 429:'rate_limited'}.get(status, 'provider_unavailable')
        raise CalendarError(code, uncertain=status >= 500 and method not in ('GET', 'HEAD'))
    if not response.content:
        return {}
    try:
        result = response.json()
    except ValueError as exc:
        raise CalendarError('invalid_response', uncertain=method not in ('GET', 'HEAD')) from exc
    if not isinstance(result, dict):
        raise CalendarError('invalid_response', uncertain=method not in ('GET', 'HEAD'))
    return result


def event_id(booking_id):
    """Google permits UUID hex characters in a deterministic event identifier."""
    return 'mlaimentor' + uuid.UUID(str(booking_id)).hex


def instant(value):
    """Parse a provider instant, rejecting missing or ambiguous timezone data."""
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise CalendarError('invalid_response')
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def meet_link(event):
    """Accept only an actual Google Meet HTTPS join URL from conference data."""
    candidates = [event.get('hangoutLink')]
    candidates += [p.get('uri') for p in event.get('conferenceData', {}).get('entryPoints', []) if p.get('entryPointType') == 'video']
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed = urlparse(candidate)
        if parsed.scheme == 'https' and parsed.hostname == 'meet.google.com' and not parsed.username and not parsed.password:
            return candidate
    return ''


class GoogleCalendarClient:
    """API operations independent from Django models and booking transactions."""

    def __init__(self, token):
        self.token = token

    def api(self, method, path, **kwargs):
        token = self.token() if callable(self.token) else self.token
        if not token:
            raise CalendarError('reconnect_required')
        headers = {'Authorization': 'Bearer ' + token}
        extra = kwargs.pop('headers', {})
        if 'If-Match' in extra:
            headers['If-Match'] = extra['If-Match']
        return send(method, API + path, headers=headers, **kwargs)

    @staticmethod
    def events_path(calendar_id):
        return '/calendars/' + quote(calendar_id, safe='') + '/events'

    def list_calendars(self):
        calendars = []
        page = None
        for _ in range(20):
            params = {'maxResults':250}
            if page:
                params['pageToken'] = page
            data = self.api('GET', '/users/me/calendarList', params=params)
            for item in data.get('items', []):
                if not isinstance(item, dict) or item.get('deleted'):
                    continue
                identifier = item.get('id')
                if not isinstance(identifier, str) or not identifier or len(identifier) > 1024:
                    raise CalendarError('invalid_response')
                role = item.get('accessRole')
                calendars.append({
                    'id':identifier, 'name':str(item.get('summaryOverride') or item.get('summary') or identifier)[:255],
                    'can_check_conflicts':role in ('owner','writer','reader','freeBusyReader'),
                    'can_write':role == 'owner',
                    'can_create_meet':'hangoutsMeet' in item.get('conferenceProperties',{}).get('allowedConferenceSolutionTypes',[]),
                })
            page = data.get('nextPageToken')
            if not page:
                return calendars
        raise CalendarError('calendar_limit')

    def fetch_busy(self, calendar_ids, start, end):
        ids = list(dict.fromkeys(calendar_ids))
        if not ids or len(ids) > 50:
            raise CalendarError('invalid_selection')
        result = self.api('POST', '/freeBusy', json={'timeMin':iso(start),'timeMax':iso(end), 'items':[{'id':identifier} for identifier in ids]})
        rows = result.get('calendars')
        if not isinstance(rows, dict):
            raise CalendarError('incomplete_busy_check')
        intervals = []
        for identifier in ids:
            row = rows.get(identifier)
            if not isinstance(row, dict) or row.get('errors') or not isinstance(row.get('busy'),list):
                raise CalendarError('incomplete_busy_check')
            for busy in row['busy']:
                if not isinstance(busy,dict):
                    raise CalendarError('invalid_response')
                begin, finish = instant(busy.get('start')), instant(busy.get('end'))
                if begin >= finish:
                    raise CalendarError('invalid_response')
                intervals.append((begin,finish))
        return sorted(set(intervals))

    def get_booking(self, calendar_id, booking_id):
        try:
            result = self.api('GET', self.events_path(calendar_id) + '/' + event_id(booking_id))
        except CalendarError as exc:
            if exc.code == 'not_found':
                return None
            raise
        if result.get('status') == 'cancelled':
            return None
        private = result.get('extendedProperties', {}).get('private', {})
        if private.get('mlai_booking_id') != str(booking_id):
            raise CalendarError('event_identity_mismatch')
        return result

    def create_booking(self, calendar_id, session):
        booking_id = str(session.pk)
        existing = self.get_booking(calendar_id,booking_id)
        if existing is not None:
            return self.booking_result(existing, session)
        video = session.location_type == 'video'
        body = {
            'id':event_id(booking_id), 'summary':'MLAI mentoring', 'transparency':'opaque',
            'start':{'dateTime':iso(session.starts_at)}, 'end':{'dateTime':iso(session.ends_at)},
            'guestsCanModify':False, 'guestsCanInviteOthers':False,
            'extendedProperties':{'private':{'mlai_booking_id':booking_id}},
        }
        if video:
            body['conferenceData']={'createRequest':{'requestId':event_id(booking_id),'conferenceSolutionKey':{'type':'hangoutsMeet'}}}
        try:
            event = self.api('POST', self.events_path(calendar_id), params={'conferenceDataVersion':1,'sendUpdates':'none'}, json=body)
        except CalendarError as exc:
            if exc.code != 'conflict':
                raise
            event = self.get_booking(calendar_id,booking_id)
            if event is None:
                raise CalendarError('event_identity_mismatch') from exc
        return self.booking_result(event,session)

    @staticmethod
    def booking_result(event,session):
        if event.get('id') != event_id(session.pk):
            raise CalendarError('event_identity_mismatch', uncertain=True)
        if instant(event.get('start',{}).get('dateTime')) != session.starts_at or instant(event.get('end',{}).get('dateTime')) != session.ends_at:
            raise CalendarError('event_identity_mismatch', uncertain=True)
        link = meet_link(event)
        status = 'created'
        if session.location_type == 'video' and not link:
            conference_status = event.get('conferenceData',{}).get('createRequest',{}).get('status',{}).get('statusCode')
            if conference_status == 'failure':
                raise CalendarError('conference_failed')
            status = 'pending'
        return {'event_id':event['id'],'meet_link':link,'status':status,'etag':event.get('etag','')}

    def invite_attendees(self, calendar_id, session):
        event = self.get_booking(calendar_id,session.pk)
        if event is None:
            raise CalendarError('not_found')
        attendees = [{'email':u.email} for u in (session.mentor.user, session.founder) if getattr(u,'email','')]
        desired = {row['email'].strip().casefold() for row in attendees}
        existing = {str(row.get('email', '')).strip().casefold() for row in event.get('attendees', []) if isinstance(row, dict) and row.get('email')}
        if existing == desired:
            # A prior PATCH may have succeeded despite a lost response. Reusing
            # sendUpdates=all would deliver the same invitations again.
            return event
        return self.api('PATCH', self.events_path(calendar_id)+'/'+event_id(session.pk),
                        params={'sendUpdates':'all'}, json={'attendees':attendees},
                        headers={'If-Match':event['etag']} if event.get('etag') else {})

    def cancel_booking(self, calendar_id, booking_id):
        event = self.get_booking(calendar_id,booking_id)
        if event is None:
            return {'outcome':'absent'}
        try:
            self.api('DELETE', self.events_path(calendar_id)+'/'+event_id(booking_id), params={'sendUpdates':'all'},
                     headers={'If-Match':event['etag']} if event.get('etag') else {})
        except CalendarError as exc:
            if exc.code != 'not_found':
                raise
            return {'outcome':'absent'}
        return {'outcome':'deleted'}

    def check_destination_conflicts(self, calendar_id, start, end, booking_id):
        """Exclude only exact MLAI IDs; FreeBusy cannot distinguish self overlap."""
        page = None
        for _ in range(20):
            params={'timeMin':iso(start),'timeMax':iso(end),'singleEvents':'true','showDeleted':'false','maxResults':250}
            if page:
                params['pageToken']=page
            data = self.api('GET',self.events_path(calendar_id),params=params)
            if not isinstance(data.get('items'),list):
                raise CalendarError('invalid_response')
            for event in data['items']:
                if event.get('id') == event_id(booking_id):
                    private=event.get('extendedProperties',{}).get('private',{})
                    if private.get('mlai_booking_id') != str(booking_id):
                        raise CalendarError('event_identity_mismatch')
                    continue
                if event.get('status') == 'cancelled' or event.get('transparency') == 'transparent':
                    continue
                # A bounded Calendar events query already expands recurrences and
                # selects overlapping occurrences, including all-day events.
                raise CalendarError('calendar_conflict')
            page=data.get('nextPageToken')
            if not page:
                return
        raise CalendarError('calendar_limit')
