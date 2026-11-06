"""Bounded reconciliation of already linked mentoring events, never calendar import.

Google owns organizer edits to a linked event. Chat owns admission, reservations
and payment. Changes are adopted under the same participant locks as booking;
unknown reads and incompatible edits never cancel/refund a session.
"""
from datetime import timedelta
from time import monotonic
import secrets

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .calendars import google, service as calendars
from .calendars.errors import CalendarError
from .meeting_policy import required
from .models import MentorSession, MentorTimeReservation
from .service import MentoringService, _lock_users


def event_change(session, event):
    """Validate a single linked timed event without changing booking/payment state."""
    if event.get('id') != google.event_id(session.pk):
        raise CalendarError('event_identity_mismatch')
    if event.get('status') == 'cancelled':
        return {'cancelled': True, 'etag': event.get('etag', '')}
    if not event.get('etag'):
        raise CalendarError('event_identity_mismatch')
    if (event.get('extendedProperties', {}).get('private', {}).get('mlai_booking_id') != str(session.pk)
            or event.get('recurrence') or event.get('recurringEventId')
            or not event.get('start', {}).get('dateTime') or not event.get('end', {}).get('dateTime')):
        raise CalendarError('unsupported_calendar_change')
    start = google.instant(event.get('start', {}).get('dateTime'))
    end = google.instant(event.get('end', {}).get('dateTime'))
    # Duration and price are frozen booking terms. Moving the session is allowed;
    # changing its purchased duration requires resolution rather than silent repricing.
    if end - start != session.ends_at - session.starts_at:
        raise CalendarError('calendar_duration_changed')
    if (start, end) != (session.starts_at, session.ends_at) and (
            start <= timezone.now() or session.starts_at <= timezone.now()):
        raise CalendarError('calendar_change_after_start')
    link = google.meet_link(event)
    if session.location_type == 'video' and not link:
        raise CalendarError('calendar_meet_removed')
    return {'cancelled': False, 'etag': event['etag'], 'start': start, 'end': end, 'link': link}


def _state(session):
    return getattr(session, 'meeting_provisioning', {}).get('calendar_sync', {})


def _fence(session):
    return (session.status, session.starts_at, session.ends_at, session.calendar_event_id,
            session.calendar_snapshot, session.meet_link)


def _save_state(session, state):
    session.meeting_provisioning = {**session.meeting_provisioning, 'calendar_sync': state}
    session.save(update_fields=['meeting_provisioning', 'updated_at'])


def sync_session(session_id):
    """Reconcile one explicitly linked accepted event; safe to replay after a crash."""
    initial = MentorSession.objects.select_related('mentor__user', 'founder').get(pk=session_id)
    with transaction.atomic():
        _lock_users(initial.founder_id, initial.mentor.user_id)
        live = MentorSession.objects.select_for_update().get(pk=session_id)
        state = _state(live)
        lease = parse_datetime(state.get('lease_until', ''))
        if (live.status != 'accepted' or not live.calendar_snapshot
                or live.calendar_event_id != google.event_id(live.pk)
                or live.notification_status != 'sent'
                or (lease and lease > timezone.now())):
            return
        claim = secrets.token_hex(16)
        _save_state(live, {**state, 'claim': claim,
                          'lease_until': (timezone.now() + timedelta(minutes=2)).isoformat(),
                          'next_check': (timezone.now() + timedelta(minutes=2)).isoformat()})
        initial = live
        fence = _fence(live)
    code = ''
    change = None
    try:
        c, calendar_id = calendars._target(initial)
        revision = c.revision
        client = calendars._client(c, maintenance=True)
        event = client.read_booking_change(calendar_id, initial.pk)
        change = event_change(initial, event)
        capture = None
        if not change['cancelled']:
            if (change['start'], change['end']) != (initial.starts_at, initial.ends_at):
                calendars.check_after_create(initial,
                    change['start'] - timedelta(minutes=initial.buffer_before_minutes),
                    change['end'] + timedelta(minutes=initial.buffer_after_minutes))
            if change['link'] != initial.meet_link and (
                    required(initial) or initial.meeting_provisioning.get('repair')):
                capture = client.configure_capture(change['link'])
        # Fence provider edits made during our conflict/capture checks.
        latest = client.read_booking_change(calendar_id, initial.pk)
        if (latest.get('etag', '') != change['etag']
                or (latest.get('status') == 'cancelled') != change['cancelled']):
            raise CalendarError('calendar_changed_during_sync')
        with transaction.atomic():
            _lock_users(initial.founder_id, initial.mentor.user_id)
            live = MentorSession.objects.select_for_update().select_related('mentor__user', 'founder').get(pk=session_id)
            current_connection, current_calendar = calendars._target(live)
            if (_state(live).get('claim') != claim or _fence(live) != fence
                    or current_connection.revision != revision or current_calendar != calendar_id):
                raise CalendarError('settings_changed')
            if change['cancelled']:
                if timezone.now() >= live.starts_at:
                    raise CalendarError('calendar_change_after_start')
                # Uses the existing exactly-once refund and cleanup path.
                MentoringService._do_cancelled(live, live.mentor.user, timezone.now())
            else:
                before, after = live.buffer_before_minutes, live.buffer_after_minutes
                for principal in sorted({live.founder_id, live.mentor.user_id}):
                    start = change['start'] - timedelta(minutes=before if principal == live.mentor.user_id else 0)
                    end = change['end'] + timedelta(minutes=after if principal == live.mentor.user_id else 0)
                    if MentorTimeReservation.objects.filter(principal_id=principal, active=True,
                            starts_at__lt=end, ends_at__gt=start).exclude(session=live).exists():
                        raise CalendarError('calendar_conflict')
                    MentorTimeReservation.objects.update_or_create(session=live, principal_id=principal,
                        defaults={'starts_at': start, 'ends_at': end, 'active': True})
                live.starts_at, live.ends_at, live.meet_link = change['start'], change['end'], change['link']
                if capture:
                    live.meeting_provisioning = {**live.meeting_provisioning, **capture,
                                                'verified_at': timezone.now().isoformat()}
                live.save(update_fields=['starts_at', 'ends_at', 'meet_link', 'meeting_provisioning', 'updated_at'])
            _save_state(live, {'status': 'synced', 'checked_at': timezone.now().isoformat(),
                              'etag': change['etag'], 'next_check': (timezone.now()+timedelta(minutes=1)).isoformat()})
        return
    except CalendarError as exc:
        code = exc.code
    except IntegrityError:
        code = 'calendar_conflict'
    with transaction.atomic():
        _lock_users(initial.founder_id, initial.mentor.user_id)
        live = MentorSession.objects.select_for_update().get(pk=session_id)
        if _state(live).get('claim') != claim:
            return
        state = {'status': 'attention', 'code': code, 'checked_at': timezone.now().isoformat(),
                 'next_check': (timezone.now()+timedelta(minutes=1)).isoformat()}
        if change and not change['cancelled']:
            state.update(proposed_start=change['start'].isoformat(), proposed_end=change['end'].isoformat())
        _save_state(live, state)


def sync_due_sessions(limit=5, time_budget_seconds=15):
    """Poll only known linked events; bound work and rotate by last scheduled check."""
    started = monotonic()
    now = timezone.now()
    due = MentorSession.objects.filter(status='accepted', notification_status='sent'
    ).exclude(calendar_event_id='').exclude(calendar_snapshot={}).filter(
        Q(meeting_provisioning__calendar_sync__next_check__isnull=True)
        | Q(meeting_provisioning__calendar_sync__next_check__lte=now.isoformat())
    ).order_by(F('meeting_provisioning__calendar_sync__next_check').asc(nulls_first=True), 'pk')
    processed = 0
    for pk in list(due.values_list('pk', flat=True)[:max(1, min(limit, 100))]):
        if monotonic() - started >= time_budget_seconds:
            break
        sync_session(pk)
        processed += 1
    return {'processed': processed}


def settlement_ready(session):
    """Do not automatically pay out a stale local time after an external edit."""
    if not getattr(session, 'calendar_snapshot', {}) or not getattr(session, 'calendar_event_id', ''):
        return True
    state = _state(session)
    checked = parse_datetime(state.get('checked_at', ''))
    return bool(state.get('status') == 'synced' and not state.get('claim') and checked
                and timedelta(0) <= timezone.now() - checked <= timedelta(minutes=5))


def public_status(session):
    """Participant-safe synchronization state without provider IDs or lease tokens."""
    state = _state(session)
    if not getattr(session, 'calendar_snapshot', {}) or not getattr(session, 'calendar_event_id', ''):
        return None
    return {key: state[key] for key in ('status', 'code', 'checked_at', 'proposed_start', 'proposed_end') if key in state}
