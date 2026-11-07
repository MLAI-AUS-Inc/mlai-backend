"""Bounded, durable repair of an existing accepted meeting; never moves Roo."""
from datetime import timedelta
from copy import copy
import logging

from django.db import transaction
from django.utils import timezone

from .calendars import service as calendars
from .calendars import google
from .calendars.errors import CalendarError
from .meeting_policy import REQUIRED_POLICY
from .models import MentorSession
from .service import MentoringError, _lock_users

logger = logging.getLogger(__name__)


def automatically_repairable(session):
    """Only adopt a legacy booking whose provider was explicitly never invoked."""
    return (session.status == 'accepted' and session.location_type == 'video'
        and session.starts_at > timezone.now() and session.booking_mode == 'legacy_request'
        and session.mentor.booking_mode == 'instant'
        and session.mentor.meeting_artifact_policy == REQUIRED_POLICY
        and session.meeting_status == 'unavailable' and session.meeting_error == 'google_off'
        and not session.meet_link and not session.calendar_event_id
        and not session.calendar_snapshot and not session.meeting_path
        and not session.meeting_provisioning)


def recover_unlinked(session_id):
    """Admit a safe, previously skipped booking to durable repair without charging."""
    session = MentorSession.objects.select_related('mentor__user').get(pk=session_id)
    if not automatically_repairable(session):
        return
    try:
        target = calendars.calendar_target(session.mentor, verify=True)
        prepare(session_id, connection_id=target['connection_id'],
            calendar_id=target['calendar_id'], automatic=True)
    except (CalendarError, MentoringError) as exc:
        # Retry readiness failures after five minutes; never overwrite a repair
        # or cancellation another worker committed while the provider was read.
        MentorSession.objects.filter(pk=session_id, status='accepted',
            meeting_status='unavailable', meeting_error='google_off',
            calendar_event_id='', calendar_snapshot={}, meeting_provisioning={}).update(
                meeting_claimed_at=timezone.now(), updated_at=timezone.now())
        logger.warning('Mentor legacy recovery deferred: session=%s code=%s', session_id, exc.code)


def prepare(session_id, *, connection_id, calendar_id, automatic=False):
    """Freeze an explicitly supplied owner-verified repair target without provider writes."""
    initial = MentorSession.objects.select_related('mentor__user').get(pk=session_id)
    if automatic and not automatically_repairable(initial):
        raise CalendarError('settings_changed')
    if initial.status != 'accepted' or initial.location_type != 'video' or initial.meet_link or initial.starts_at <= timezone.now():
        raise MentoringError('invalid_state', 'Only a confirmed video session missing its link can be repaired.', 409)
    candidate = copy(initial.mentor)
    candidate.meeting_artifact_policy = dict(REQUIRED_POLICY)
    target = calendars.calendar_target(candidate, verify=True)
    if target['connection_id'] != connection_id or target['calendar_id'] != calendar_id:
        raise CalendarError('settings_changed')
    if initial.calendar_event_id and (initial.calendar_event_id != google.event_id(initial.pk) or not initial.calendar_snapshot):
        raise CalendarError('original_organizer_review_required')
    if initial.calendar_snapshot and any(initial.calendar_snapshot.get(k) != target[k]
            for k in ('connection_id', 'calendar_id')):
        raise CalendarError('original_organizer_review_required')
    with transaction.atomic():
        _lock_users(initial.founder_id, initial.mentor.user_id)
        live = MentorSession.objects.select_for_update().select_related('mentor__user').get(pk=session_id)
        if automatic and not automatically_repairable(live):
            raise CalendarError('settings_changed')
        if live.status != 'accepted' or live.meet_link or calendars.calendar_target(live.mentor) != target:
            raise CalendarError('settings_changed')
        if live.calendar_event_id != initial.calendar_event_id or live.calendar_snapshot != initial.calendar_snapshot:
            raise CalendarError('settings_changed')
        if live.meeting_provisioning.get('repair'):
            return live
        live.calendar_snapshot = target
        live.meeting_provisioning = {'repair': True, 'cleanup_pending': True,
            'policy': dict(REQUIRED_POLICY), 'prepared_at': timezone.now().isoformat()}
        live.meeting_status = 'pending'
        live.save(update_fields=['calendar_snapshot', 'meeting_provisioning', 'meeting_status', 'updated_at'])
    return live


def process(session_id):
    """Recover the same event on every retry, preserving status, price and ledger."""
    with transaction.atomic():
        initial = MentorSession.objects.select_related('mentor__user').get(pk=session_id)
        _lock_users(initial.founder_id, initial.mentor.user_id)
        live = MentorSession.objects.select_for_update().select_related('mentor__user', 'founder').get(pk=session_id)
        if not live.meeting_provisioning.get('repair'):
            return
        now = timezone.now()
        if live.meeting_claimed_at and live.meeting_claimed_at > now - timedelta(minutes=2):
            return
        if live.meet_link and live.status == 'accepted' and live.notification_status == 'sent':
            return
        live.meeting_claimed_at = now
        live.save(update_fields=['meeting_claimed_at', 'updated_at'])
    # All Google calls occur outside row locks.
    try:
        if live.status != 'accepted':
            # Cleanup only when cancelled. A completed appointment retains its event.
            if live.status == 'cancelled':
                deleted = calendars.cancel_booking(live)
                if deleted.get('outcome') not in ('deleted', 'absent'):
                    raise CalendarError('invalid_response')
                # Older successful repairs retained unknown_create. A stored,
                # verified deterministic event proves that insert completed;
                # absence is then sufficient cleanup, never cancellation evidence.
                observed = (live.calendar_event_id == google.event_id(live.pk)
                    and bool(live.meet_link) and live.meeting_provisioning.get('verified') is True)
                if (deleted['outcome'] == 'absent' and live.meeting_provisioning.get('unknown_create')
                        and not observed):
                    return
            outcome = {**live.meeting_provisioning, 'cleanup_pending': False, 'unknown_create': False}
            fields = {'meeting_provisioning': outcome}
        else:
            result = calendars.lookup_booking(live)
            if result is None:
                start = live.starts_at - timedelta(minutes=live.buffer_before_minutes)
                end = live.ends_at + timedelta(minutes=live.buffer_after_minutes)
                if calendars.fetch_busy(live.mentor, start, end):
                    raise CalendarError('calendar_conflict')
                live.meeting_provisioning = {**live.meeting_provisioning, 'unknown_create': True}
                if not MentorSession.objects.filter(pk=live.pk, status='accepted', meeting_claimed_at=now).update(
                        meeting_provisioning=live.meeting_provisioning):
                    return
                result = calendars.create_booking(live)
            if result.get('status') != 'created' or not result.get('meet_link'):
                raise CalendarError('calendar_pending')
            c, identifier = calendars._target(live)
            client = calendars._client(c)
            calendars._access_token(c)
            if google.MEET_SETTINGS_SCOPE not in c.scopes:
                raise CalendarError('meet_reconnect_required')
            capture = client.configure_capture(result['meet_link'])
            calendars.verify_booking(live)
            # Check cancellation/lease before inviting. In-flight Google writes are
            # reconciled by the next cancelled-session cleanup pass.
            current = MentorSession.objects.get(pk=live.pk)
            if current.status != 'accepted' or current.meeting_claimed_at != now:
                return
            calendars.invite_attendees(live)
            fields = {'calendar_event_id': result['event_id'], 'meet_link': result['meet_link'],
                'meeting_status': 'created', 'meeting_error': '', 'notification_status': 'sent',
                'meeting_provisioning': {**live.meeting_provisioning, **capture,
                    'verified_at': timezone.now().isoformat(), 'cleanup_pending': True,
                    'unknown_create': False}}
        saved = MentorSession.objects.filter(pk=live.pk, status=live.status,
            meeting_claimed_at=now).update(**fields)
        if saved and live.status == 'cancelled' and not fields['meeting_provisioning']['cleanup_pending']:
            live.time_reservations.update(active=False)
    except CalendarError as exc:
        MentorSession.objects.filter(pk=live.pk, meeting_claimed_at=now).update(
            meeting_status='failed', meeting_error=exc.code)
