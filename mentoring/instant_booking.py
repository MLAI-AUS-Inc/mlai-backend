"""Explicit instant booking with durable provider operations and exact refunds.

All calendar calls occur outside transactions. A durable attempt fences late
workers and retains reservations until uncertain calendar cleanup is resolved.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone as utc
from zoneinfo import ZoneInfo
from time import monotonic

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from .models import MentorBookingAttempt, MentorProfile, MentorSession, MentorTimeReservation
from . import scheduling
from .service import (
    MentoringError, MentoringService, _away_ranges, _charge_founder, _lock_keys,
    _lock_users, _refund_charge, parse_aware_datetime, parse_mentor_id,
    parse_request_id, serialize_session, validate_brief,
)

logger = logging.getLogger(__name__)
QUOTE_SALT = 'mentoring.instant-booking.quote.v1'
QUOTE_MAX_AGE = 300
LEASE_SECONDS = 120
TERMINAL = ('confirmed', 'failed', 'cancelled')


def enabled():
    """New instant admission is independent from legacy mentoring availability."""
    return bool(getattr(settings, 'MENTOR_INSTANT_BOOKING_ENABLED', False))


def _calendars():
    from .calendars import service
    return service


def _json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _target(profile):
    return _calendars().calendar_target(profile, verify=False)


def _ready(profile):
    if not enabled():
        raise MentoringError('instant_booking_disabled', 'Instant booking is not available.', 404)
    if not profile.is_active or not profile.user.is_active or not profile.accepting_bookings:
        raise MentoringError('mentor_not_accepting_bookings', 'This mentor is not accepting bookings.', 409)
    return _target(profile)


def _principals(session):
    return (session.founder_id, session.mentor.user_id)


def reserve_session(session):
    """Reserve participant time; PostgreSQL exclusion is the final race guard."""
    before = session.buffer_before_minutes
    after = session.buffer_after_minutes
    for user_id in sorted(set(_principals(session))):
        start = session.starts_at - timedelta(minutes=before) if user_id == session.mentor.user_id else session.starts_at
        end = session.ends_at + timedelta(minutes=after) if user_id == session.mentor.user_id else session.ends_at
        MentorTimeReservation.objects.create(
            session=session, principal_id=user_id, starts_at=start, ends_at=end,
        )


def _internal_busy(profile, now):
    return list(MentorTimeReservation.objects.filter(
        principal_id=profile.user_id, active=True, ends_at__gt=now,
    ).values_list('starts_at', 'ends_at'))


def available_slots(profile, first_day, last_day, *, external_busy, now=None):
    """Shared rule engine for previews and final locked reservation checks."""
    now = now or timezone.now()
    return scheduling.generate_slots(
        rules=[(r.weekday, r.start_time, r.end_time) for r in profile.availability_rules.all()],
        tz_name=profile.timezone, session_minutes=profile.session_minutes,
        first_day=first_day, last_day=last_day,
        busy=_internal_busy(profile, now) + list(external_busy), now=now,
        away=_away_ranges(profile), buffer_before_minutes=profile.buffer_before_minutes,
        buffer_after_minutes=profile.buffer_after_minutes,
        minimum_notice_minutes=profile.minimum_notice_minutes,
        booking_horizon_days=profile.booking_horizon_days,
        start_increment_minutes=profile.start_increment_minutes,
    )


def availability(profile, first_day, last_day):
    """Fail closed when any required calendar cannot be checked."""
    _ready(profile)
    problem = scheduling.validate_range(first_day, last_day)
    if problem:
        raise MentoringError('invalid_range', problem, 400)
    now = timezone.now()
    tz = ZoneInfo(profile.timezone)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(utc.utc)
    end = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(utc.utc)
    busy = _calendars().fetch_busy(profile, start - timedelta(minutes=profile.buffer_before_minutes),
        end + timedelta(minutes=profile.buffer_after_minutes))
    return available_slots(profile, first_day, last_day, external_busy=busy, now=now)


def make_quote(*, founder, mentor_id, starts_at, expected_price):
    """Bind the member's reviewed instant-booking terms for five minutes."""
    profile = MentoringService._get_active_mentor(parse_mentor_id(mentor_id))
    target = _ready(profile)
    if not getattr(founder, 'email', '') or '@' not in founder.email:
        raise MentoringError('email_required', 'Add an email address to receive your Calendar invitation.', 409)
    start = parse_aware_datetime(starts_at)
    if profile.user_id == founder.pk:
        raise MentoringError('own_mentor_profile', 'You cannot book yourself.', 409)
    if type(expected_price) is not int or expected_price != profile.price_roo:
        raise MentoringError('price_changed', 'Please review the current price.', 409)
    day = start.astimezone(ZoneInfo(profile.timezone)).date()
    if start not in availability(profile, day, day):
        raise MentoringError('slot_unavailable', 'That time is no longer available.', 409)
    terms = {
        'founder_id': founder.pk, 'mentor_id': profile.pk, 'starts_at': start.isoformat(),
        'price_roo': profile.price_roo, 'session_minutes': profile.session_minutes,
        'location_type': profile.location_type, 'revision': profile.booking_revision,
        'meeting_artifact_policy': dict(profile.meeting_artifact_policy),
        'buffer_before_minutes': profile.buffer_before_minutes,
        'buffer_after_minutes': profile.buffer_after_minutes, 'calendar_fingerprint': _json_digest(target),
    }
    public = {key: terms[key] for key in ('mentor_id', 'starts_at', 'price_roo', 'session_minutes', 'location_type', 'buffer_before_minutes', 'buffer_after_minutes')}
    return {**public, 'capture_required': profile.location_type == 'video' and bool(profile.meeting_artifact_policy), 'quote': signing.dumps(terms, salt=QUOTE_SALT, compress=True),
            'expires_at': (timezone.now() + timedelta(seconds=QUOTE_MAX_AGE)).isoformat()}


def _load_quote(quote, founder):
    try:
        terms = signing.loads(quote, salt=QUOTE_SALT, max_age=QUOTE_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        raise MentoringError('quote_expired', 'Please review this time again.', 409)
    if terms.get('founder_id') != founder.pk:
        raise MentoringError('invalid_quote', 'This quote belongs to another account.', 403)
    return terms


def _assert_quote_current(profile, terms):
    target = _ready(profile)
    fields = ('session_minutes', 'price_roo', 'location_type', 'buffer_before_minutes', 'buffer_after_minutes')
    if terms.get('meeting_artifact_policy', {}) != profile.meeting_artifact_policy:
        raise MentoringError('settings_changed', 'Meeting settings changed. Please review again.', 409)
    if profile.booking_revision != terms['revision'] or any(getattr(profile, f) != terms[f] for f in fields):
        raise MentoringError('settings_changed', 'The mentor changed their settings. Please review again.', 409)
    if _json_digest(target) != terms['calendar_fingerprint']:
        raise MentoringError('settings_changed', 'The calendar settings changed. Please review again.', 409)
    return target


def _previous(founder, request_id, digest):
    previous = MentorBookingAttempt.objects.select_related('session__mentor__user', 'session__founder').filter(
        session__founder=founder, session__client_request_id=request_id,
    ).first()
    if previous and previous.payload_digest != digest:
        raise MentoringError('idempotency_conflict', 'This request ID was used for different booking details.', 409)
    if not previous and MentorSession.objects.filter(founder=founder, client_request_id=request_id).exists():
        raise MentoringError('idempotency_conflict', 'This request ID already belongs to another booking.', 409)
    return previous


def book(*, founder, quote, brief, client_request_id):
    """Reserve, debit and enqueue once; matching retries recover before quote expiry checks."""
    request_id = parse_request_id(client_request_id)
    digest = _json_digest({'quote': quote, 'brief': brief})
    previous = _previous(founder, request_id, digest)
    if previous:
        return previous
    if connection.vendor != 'postgresql':
        raise MentoringError('instant_booking_unavailable', 'Instant booking requires the production booking store.', 503)
    terms = _load_quote(quote, founder)
    profile = MentoringService._get_active_mentor(terms['mentor_id'])
    _assert_quote_current(profile, terms)
    start = parse_aware_datetime(terms['starts_at'])
    end = start + timedelta(minutes=terms['session_minutes'])
    before, after = scheduling.protected_interval(start, terms['session_minutes'], terms['buffer_before_minutes'], terms['buffer_after_minutes'])
    from . import conflicts
    checked_at = timezone.now()
    calendar_config = conflicts.fingerprint(profile)
    busy = _calendars().fetch_busy(profile, before, after)
    try:
        with transaction.atomic():
            _lock_users(founder.pk, profile.user_id)
            _lock_keys(f'mentoring:request:{request_id}', *(f'mentoring:principal:{uid}' for uid in (founder.pk, profile.user_id)))
            previous = _previous(founder, request_id, digest)
            if previous:
                return previous
            profile = MentorProfile.objects.select_for_update().select_related('user').get(pk=profile.pk)
            trusted_target = _assert_quote_current(profile, terms)
            conflicts.validate(profile, {'config': calendar_config, 'checked_at': checked_at})
            now = timezone.now()
            if (now - checked_at).total_seconds() > 10 or now >= start:
                raise MentoringError('slot_unavailable', 'Please check this time again.', 409)
            day = start.astimezone(ZoneInfo(profile.timezone)).date()
            if start not in available_slots(profile, day, day, external_busy=busy, now=now):
                raise MentoringError('slot_unavailable', 'That time is no longer available.', 409)
            if MentorTimeReservation.objects.filter(principal_id=founder.pk, active=True,
                    starts_at__lt=end, ends_at__gt=start).exists():
                raise MentoringError('founder_busy', 'You already have a session at that time.', 409)
            fields = validate_brief(brief, profile.brief_questions)
            session = MentorSession.objects.create(
                mentor=profile, founder=founder, starts_at=start, ends_at=end,
                status=MentorSession.STATUS_PROVISIONING, booking_mode='instant',
                price_roo=profile.price_roo, location_type=profile.location_type,
                location_detail=profile.space_notes if profile.location_type == 'mlai_space' else '',
                buffer_before_minutes=profile.buffer_before_minutes, buffer_after_minutes=profile.buffer_after_minutes,
                calendar_snapshot=trusted_target, booking_snapshot=terms,
                client_request_id=request_id, requested_at=now, expires_at=start, **fields,
            )
            reserve_session(session)
            _charge_founder(session)
            session.save()
            attempt = MentorBookingAttempt.objects.create(
                session=session, payload_digest=digest, deadline=min(now + timedelta(minutes=15), start),
            )
    except IntegrityError as exc:
        previous = _previous(founder, request_id, digest)
        if previous:
            return previous
        raise MentoringError('slot_unavailable', 'That time was just booked. Please choose another.', 409) from exc
    return attempt


def serialize_attempt(attempt, viewer):
    """No calendar identities or provider error bodies escape this response."""
    session = attempt.session
    payment = 'refunded' if session.release_ledger_entry_id else 'processing' if attempt.status not in TERMINAL else 'paid'
    return {
        'id': str(attempt.pk), 'status': attempt.status, 'error_code': attempt.error_code,
        'role': 'founder' if viewer.pk == session.founder_id else 'mentor',
        'mentor_id': session.mentor_id, 'client_request_id': str(session.client_request_id),
        'starts_at': session.starts_at.isoformat(), 'mentor_name': session.mentor.user.full_name or 'MLAI mentor',
        'payment_status': payment, 'detail': {
            'provisioning': 'Confirming your booking.', 'reconciling': 'Still confirming your booking.',
            'confirmed': 'Your session is confirmed.', 'failed': 'Booking could not be confirmed. Your Roo has been refunded.',
            'cancelled': 'Booking cancelled. Your Roo has been refunded.',
        }.get(attempt.status, 'Checking booking status.'),
        'session': serialize_session(session, viewer) if attempt.status == 'confirmed' else None,
    }


def get_attempt(attempt_id, actor):
    """Return only a participant's attempt, including after its quote expired."""
    try:
        attempt = MentorBookingAttempt.objects.select_related('session__mentor__user', 'session__founder').get(pk=attempt_id)
    except (MentorBookingAttempt.DoesNotExist, ValueError):
        raise MentoringError('booking_not_found', 'Booking not found.', 404)
    if actor.pk not in _principals(attempt.session):
        raise MentoringError('booking_not_found', 'Booking not found.', 404)
    return attempt


def list_attempts(actor):
    """Recover pending and recent failed operations without provider writes."""
    query = MentorBookingAttempt.objects.select_related('session__mentor__user', 'session__founder').filter(
        Q(session__founder=actor) | Q(session__mentor__user=actor),
    ).exclude(status='confirmed').filter(
        Q(status__in=('provisioning', 'reconciling')) | Q(updated_at__gte=timezone.now()-timedelta(days=7)),
    ).order_by('-created_at')[:50]
    return [serialize_attempt(attempt, actor) for attempt in query]


def _cancel_local(attempt, *, status='failed', code='booking_failed', actor=None):
    session = attempt.session
    _refund_charge(session, actor_slack_id=getattr(actor, 'slack_id', '') or '')
    session.status = MentorSession.STATUS_CANCELLED
    session.failure_code = code
    session.cancelled_at = timezone.now()
    session.cancelled_by = actor
    session.save()
    attempt.status = status
    attempt.error_code = code
    attempt.generation += 1
    attempt.cleanup_pending = attempt.provider_started
    attempt.next_attempt_at = timezone.now()
    attempt.save()
    if not attempt.cleanup_pending:
        session.time_reservations.update(active=False)


def cancel_attempt(attempt_id, actor):
    """Unconfirmed attempts can always be cancelled; money resolves before remote cleanup."""
    original = get_attempt(attempt_id, actor)
    with transaction.atomic():
        _lock_users(*_principals(original.session))
        attempt = MentorBookingAttempt.objects.select_for_update().select_related('session__mentor__user', 'session__founder').get(pk=original.pk)
        if attempt.status == 'confirmed':
            raise MentoringError('booking_confirmed', 'Cancel the confirmed session from My sessions.', 409)
        if attempt.status not in ('failed', 'cancelled'):
            _cancel_local(attempt, status='cancelled', code='cancelled_by_participant', actor=actor)
    return attempt


def mark_cancelled_cleanup(session):
    """Keep paid-session cancellation and calendar cleanup in the same commit."""
    attempt = MentorBookingAttempt.objects.select_for_update().get(session=session)
    attempt.status = 'cancelled'
    attempt.generation += 1
    attempt.cleanup_pending = attempt.provider_started
    attempt.next_attempt_at = timezone.now()
    attempt.save()
    if not attempt.cleanup_pending:
        session.time_reservations.update(active=False)


def _requeue_late_cleanup(attempt):
    """A finished stale call can reveal a late remote event after cancellation."""
    if attempt.status in ('failed', 'cancelled'):
        attempt.cleanup_pending = True
        attempt.next_attempt_at = timezone.now()
        attempt.save(update_fields=['cleanup_pending', 'next_attempt_at', 'updated_at'])


def _claim(attempt_id):
    initial = MentorBookingAttempt.objects.select_related('session__mentor__user', 'session__founder').get(pk=attempt_id)
    with transaction.atomic():
        _lock_users(*_principals(initial.session))
        attempt = MentorBookingAttempt.objects.select_for_update().select_related('session__mentor__user', 'session__founder').get(pk=attempt_id)
        now = timezone.now()
        if (attempt.status == 'confirmed' and attempt.session.notification_status in ('sent', 'skipped')) or (attempt.status in ('failed', 'cancelled') and not attempt.cleanup_pending):
            return None
        if attempt.status not in TERMINAL and now >= attempt.deadline:
            _cancel_local(attempt, code='confirmation_deadline')
        if attempt.lease_until and attempt.lease_until > now:
            return None
        token = uuid.uuid4()
        attempt.lease_token = token
        attempt.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        attempt.generation += 1
        attempt.provider_started = True
        attempt.save()
        return attempt, token, attempt.generation


def _record_create_started(attempt_id, token, generation):
    """Persist uncertainty before issuing a potentially successful remote write."""
    original = MentorBookingAttempt.objects.select_related('session__mentor').get(pk=attempt_id)
    with transaction.atomic():
        _lock_users(original.session.founder_id, original.session.mentor.user_id)
        live = MentorBookingAttempt.objects.select_for_update(of=('self',)).select_related('session').get(pk=attempt_id)
        if live.status in TERMINAL or live.lease_token != token or live.generation != generation:
            return False
        snapshot = dict(live.session.booking_snapshot)
        snapshot['_calendar_create_state'] = 'unknown'
        live.session.booking_snapshot = snapshot
        live.session.save(update_fields=['booking_snapshot', 'updated_at'])
        return True


def _record_first_create_rejected(attempt_id, token, generation):
    """A definitive first-write rejection has no unresolved earlier insert."""
    original = MentorBookingAttempt.objects.select_related('session__mentor').get(pk=attempt_id)
    with transaction.atomic():
        _lock_users(original.session.founder_id, original.session.mentor.user_id)
        live = MentorBookingAttempt.objects.select_for_update(of=('self',)).select_related('session').get(pk=attempt_id)
        if live.lease_token != token or live.generation != generation:
            return
        snapshot = dict(live.session.booking_snapshot)
        if snapshot.get('_calendar_create_state') == 'unknown':
            snapshot['_calendar_create_state'] = 'rejected'
            live.session.booking_snapshot = snapshot
            live.session.save(update_fields=['booking_snapshot', 'updated_at'])


def _record_event_observed(attempt_id):
    """A positive result remains useful evidence even after local cancellation."""
    original = MentorBookingAttempt.objects.select_related('session__mentor').get(pk=attempt_id)
    with transaction.atomic():
        _lock_users(original.session.founder_id, original.session.mentor.user_id)
        live = MentorBookingAttempt.objects.select_for_update(of=('self',)).select_related('session').get(pk=attempt_id)
        snapshot = dict(live.session.booking_snapshot)
        snapshot['_calendar_create_state'] = 'observed'
        live.session.booking_snapshot = snapshot
        live.session.save(update_fields=['booking_snapshot', 'updated_at'])
        if live.status in ('failed', 'cancelled'):
            _requeue_late_cleanup(live)


def _retry_wait_seconds(attempt):
    """Quarantined tombstones back off without starving live paid attempts."""
    if attempt.status in ('failed', 'cancelled') and attempt.cleanup_pending:
        # Initial claim and cancellation consume the first three generations.
        exponent = max(0, min(attempt.generation - 4, 7))
        return min(3600, 30 * (2 ** exponent))
    return 30


def process_attempt(attempt_id):
    """One bounded outbox attempt. Unknown remote writes never free a reservation."""
    claim = _claim(attempt_id)
    if not claim:
        return
    attempt, token, generation = claim
    session = attempt.session
    create_called = False
    previous_unknown_create = session.booking_snapshot.get('_calendar_create_state') == 'unknown'
    try:
        provider = _calendars()
        if attempt.status == 'confirmed':
            if not MentorBookingAttempt.objects.filter(pk=attempt.pk, status='confirmed',
                    lease_token=token, generation=generation, session__status='accepted').exists():
                return
            provider.invite_attendees(session, target=session.calendar_snapshot)
            result = None
        elif attempt.status in ('failed', 'cancelled'):
            result = provider.cancel_booking(session, target=session.calendar_snapshot)
        else:
            result = provider.lookup_booking(session, target=session.calendar_snapshot)
            if result is None:
                # Recheck external availability immediately before the first write.
                profile = session.mentor
                busy = provider.fetch_busy(profile, session.starts_at - timedelta(minutes=session.buffer_before_minutes),
                                           session.ends_at + timedelta(minutes=session.buffer_after_minutes))
                p_start, p_end = scheduling.protected_interval(session.starts_at,
                    int((session.ends_at-session.starts_at).total_seconds()/60), session.buffer_before_minutes, session.buffer_after_minutes)
                if any(scheduling.overlaps(p_start,p_end,s,e) for s,e in busy):
                    raise MentoringError('slot_unavailable', 'The calendar is no longer free.', 409)
                if not _record_create_started(attempt.pk, token, generation):
                    return
                create_called = True
                result = provider.create_booking(session, target=session.calendar_snapshot)
            if result:
                _record_event_observed(attempt.pk)
            if result and result.get('status') == 'created':
                from .meeting_policy import required
                capture = provider.configure_capture(session, result, target=session.calendar_snapshot) if required(session) else {}
                if required(session) and not (isinstance(capture, dict) and capture.get('verified') is True):
                    from .calendars.errors import CalendarError
                    raise CalendarError('capture_not_verified')
                result['capture'] = capture
                provider.verify_booking(session, target=session.calendar_snapshot)
        with transaction.atomic():
            _lock_users(*_principals(session))
            live = MentorBookingAttempt.objects.select_for_update().select_related('session__mentor__user', 'session__founder').get(pk=attempt.pk)
            if live.lease_token != token or live.generation != generation:
                _requeue_late_cleanup(live)
                return
            session = live.session
            if live.status == 'confirmed':
                session.notification_status = MentorSession.NOTIFY_SENT
                session.save(update_fields=['notification_status', 'updated_at'])
            elif live.status in ('failed', 'cancelled'):
                outcome = result.get('outcome') if isinstance(result, dict) else None
                state = session.booking_snapshot.get('_calendar_create_state', 'not_started')
                # One GET404 after an unknown insert cannot prove the server
                # will never finish that insert. Keep the tombstone and slot
                # quarantined until an event is positively observed/deleted.
                resolved = outcome == 'deleted' or (outcome == 'absent' and state != 'unknown')
                live.cleanup_pending = not resolved
                if resolved:
                    session.time_reservations.update(active=False)
            elif result and result.get('status') == 'created' and (session.location_type != 'video' or result.get('meet_link')):
                profile = MentorProfile.objects.select_for_update().select_related('user').get(pk=session.mentor_id)
                if timezone.now() >= live.deadline or not profile.accepting_bookings or not profile.is_active or profile.booking_revision != session.booking_snapshot['revision'] or _target(profile) != session.calendar_snapshot:
                    _cancel_local(live, code='booking_changed')
                    return
                session.status = MentorSession.STATUS_ACCEPTED
                session.calendar_event_id = result['event_id']
                session.meet_link = result.get('meet_link', '')
                session.meeting_status = MentorSession.MEETING_CREATED
                session.meeting_provisioning = result.get('capture', {})
                session.responded_at = timezone.now()
                session.save()
                live.status = 'confirmed'
                live.error_code = ''
            else:
                live.status = 'reconciling'
            live.lease_until = None
            live.next_attempt_at = timezone.now() + timedelta(seconds=_retry_wait_seconds(live))
            live.save()
    except Exception as exc:
        if create_called and not previous_unknown_create and not getattr(exc, 'uncertain', True):
            _record_first_create_rejected(attempt.pk, token, generation)
        # Only pre-write business conflicts are definitive. Provider errors can
        # represent a successful remote write with a lost response.
        with transaction.atomic():
            _lock_users(*_principals(session))
            live = MentorBookingAttempt.objects.select_for_update().select_related('session__mentor__user', 'session__founder').get(pk=attempt.pk)
            if live.lease_token != token or live.generation != generation:
                _requeue_late_cleanup(live)
                return
            hard_codes = {'slot_unavailable', 'permission', 'invalid_request', 'conference_unavailable', 'conference_failed', 'calendar_conflict', 'reconnect_required', 'calendar_not_configured', 'meet_reconnect_required', 'invalid_meeting_policy', 'invalid_meeting_identity'}
            if getattr(exc, 'code', '') in hard_codes and not getattr(exc, 'uncertain', False) and live.status not in TERMINAL:
                _cancel_local(live, code=getattr(exc, 'code', 'booking_failed'))
            elif live.status == 'confirmed':
                live.session.notification_status = MentorSession.NOTIFY_FAILED
                live.session.save(update_fields=['notification_status', 'updated_at'])
            elif live.status not in TERMINAL:
                live.status = 'reconciling'
                live.error_code = 'calendar_pending'
            live.lease_until = None
            live.next_attempt_at = timezone.now() + timedelta(seconds=_retry_wait_seconds(live))
            live.save()
        logger.warning('Mentor booking operation pending: attempt=%s code=%s', attempt.pk, getattr(exc, 'code', 'provider_unavailable'))


def process_due_bookings(limit=5, time_budget_seconds=30):
    """Run bounded recovery even when new instant-booking admission is disabled."""
    started = monotonic()
    processed = 0
    limit = max(1, min(limit, 100))
    budget = max(1, min(time_budget_seconds, 300))
    ids = list(MentorBookingAttempt.objects.filter(
        Q(status__in=('provisioning', 'reconciling')) | Q(cleanup_pending=True)
        | Q(status='confirmed', session__notification_status__in=('pending', 'failed')),
        next_attempt_at__lte=timezone.now(),
    ).annotate(work_priority=Case(
        When(status__in=('provisioning', 'reconciling'), then=Value(0)),
        When(status='confirmed', then=Value(1)), default=Value(2), output_field=IntegerField(),
    )).order_by('work_priority', 'next_attempt_at').values_list('pk', flat=True)[:limit])
    for pk in ids:
        if monotonic() - started >= budget:
            break
        process_attempt(pk)
        processed += 1
    # Expired legacy requests must release their backfilled reservations even
    # after the mentor converts and no legacy read triggers the old lazy sweep.
    now = timezone.now()
    due = MentorSession.objects.filter(
        Q(status=MentorSession.STATUS_REQUESTED, expires_at__lte=now)
        | Q(status=MentorSession.STATUS_ACCEPTED, ends_at__lte=now-timedelta(hours=int(getattr(settings, 'MENTOR_AUTO_COMPLETE_HOURS', 24)))),
    ).order_by('expires_at', 'pk').values_list('pk', flat=True)[:max(1,min(limit,100))]
    for pk in due:
        MentoringService.settle(pk)
    from .meeting_repair import process as repair
    repairs = MentorSession.objects.filter(meeting_provisioning__repair=True).filter(
        Q(status='accepted', meet_link='') | Q(status='accepted', notification_status__in=('pending', 'failed'))
        | Q(status='cancelled', meeting_provisioning__cleanup_pending=True)
    ).order_by('updated_at').values_list('pk', flat=True)[:limit]
    for pk in repairs:
        if monotonic() - started >= budget:
            break
        repair(pk)
    return {'processed': processed}
