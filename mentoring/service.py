"""Business logic for Vibe Mentoring.

Patterns are copied from roo.meeting_rooms.MeetingRoomService: errors carry a
code, message and HTTP status; a request id makes creation idempotent; points
move in the same transaction as the session row; a Postgres advisory lock
serialises writers for one mentor.

Points flow (all via PointsService, source MANUAL, reference_type MENTOR_SESSION
so no roo migration is needed):
  request  -> spend   key mentor_hold:{id}     founder pays the mentor's price
  decline / cancel / expiry -> refund key mentor_release:{id}
  complete -> award   key mentor_payout:{id}   mentor receives the price
Expiry and auto-completion are applied lazily whenever sessions are read.
"""

import hashlib
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from roo.models import PointsAccount
from roo.permissions import InsufficientBalanceError
from roo.services import PointsService

from . import scheduling
from .models import (
    DEFAULT_PRICE_ROO,
    LOCATION_EITHER,
    LOCATION_MLAI_SPACE,
    LOCATION_VIDEO,
    MAX_SESSION_MINUTES,
    MIN_SESSION_MINUTES,
    MentorAvailabilityRule,
    MentorProfile,
    MentorSession,
)

logger = logging.getLogger(__name__)

LEDGER_SOURCE = 'MANUAL'
LEDGER_REFERENCE_TYPE = 'MENTOR_SESSION'
MAX_PRICE_ROO = 1000
MAX_AVAILABILITY_WINDOWS = 28
MAX_AWAY_RANGES = 20
_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


class MentoringError(Exception):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _ttl_hours():
    return int(getattr(settings, 'MENTOR_REQUEST_TTL_HOURS', 48))


def _auto_complete_hours():
    return int(getattr(settings, 'MENTOR_AUTO_COMPLETE_HOURS', 24))


def _advisory_key(text):
    digest = hashlib.blake2b(text.encode('utf-8'), digest_size=8).digest()
    return int.from_bytes(digest, byteorder='big', signed=True)


def _lock_keys(*texts):
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        for key in sorted({_advisory_key(t) for t in texts}):
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])


def _display_name(user):
    return (user.full_name or '').strip() or 'MLAI member'


# ---------------------------------------------------------------- validation

def parse_aware_datetime(value):
    if not isinstance(value, str):
        raise MentoringError('invalid_time', 'starts_at must be an ISO 8601 time with an offset.', 400)
    parsed = parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise MentoringError('invalid_time', 'starts_at must be an ISO 8601 time with an offset.', 400)
    return parsed.astimezone(dt_timezone.utc)


def parse_request_id(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise MentoringError('invalid_request_id', 'client_request_id must be a UUID.', 400)


def _clean_text(value, field, max_length, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise MentoringError('invalid_field', f'{field} must be text.', 400)
    value = value.strip()
    if required and not value:
        raise MentoringError('invalid_field', f'{field} is required.', 400)
    if len(value) > max_length:
        raise MentoringError('invalid_field', f'{field} must be at most {max_length} characters.', 400)
    return value


def _clean_url(value, field):
    value = _clean_text(value, field, 500)
    if value and not re.match(r'^https://[^\s]+$', value):
        raise MentoringError('invalid_field', f'{field} must be an https link.', 400)
    return value


def validate_brief(brief):
    if not isinstance(brief, dict):
        raise MentoringError('invalid_brief', 'A brief is required.', 400)
    return {
        'brief_stage': _clean_text(brief.get('stage'), 'brief.stage', 200, required=True),
        'brief_struggle': _clean_text(brief.get('struggle'), 'brief.struggle', 1000, required=True),
        'brief_outcome': _clean_text(brief.get('outcome'), 'brief.outcome', 1000, required=True),
        'brief_link': _clean_url(brief.get('link'), 'brief.link'),
    }


def _parse_hhmm(value, field):
    match = _TIME_RE.match(value) if isinstance(value, str) else None
    if not match:
        raise MentoringError('invalid_availability', f'{field} must be HH:MM.', 400)
    return time(int(match.group(1)), int(match.group(2)))


def validate_availability(windows):
    if not isinstance(windows, list) or len(windows) > MAX_AVAILABILITY_WINDOWS:
        raise MentoringError(
            'invalid_availability',
            f'availability must be a list of at most {MAX_AVAILABILITY_WINDOWS} windows.', 400,
        )
    parsed = []
    for window in windows:
        if not isinstance(window, dict):
            raise MentoringError('invalid_availability', 'Each window must be an object.', 400)
        weekday = window.get('weekday')
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise MentoringError('invalid_availability', 'weekday must be 0 (Monday) to 6 (Sunday).', 400)
        start = _parse_hhmm(window.get('start'), 'start')
        end = _parse_hhmm(window.get('end'), 'end')
        if end <= start:
            raise MentoringError('invalid_availability', 'A window must end after it starts.', 400)
        parsed.append((weekday, start, end))
    parsed.sort()
    for earlier, later in zip(parsed, parsed[1:]):
        if earlier[0] == later[0] and later[1] < earlier[2]:
            raise MentoringError('invalid_availability', 'Availability windows must not overlap.', 400)
    return parsed


def validate_away_dates(value):
    """Return a clean, sorted list of {"from", "to"} ISO date strings (inclusive)."""
    if not isinstance(value, list) or len(value) > MAX_AWAY_RANGES:
        raise MentoringError('invalid_away_dates', f'away_dates must be a list of at most {MAX_AWAY_RANGES} ranges.', 400)
    clean = []
    for item in value:
        try:
            first = date.fromisoformat(item['from'])
            last = date.fromisoformat(item['to'])
        except (KeyError, TypeError, ValueError):
            raise MentoringError('invalid_away_dates', 'Each range needs from and to as YYYY-MM-DD.', 400)
        if last < first or (last - first).days > 366:
            raise MentoringError('invalid_away_dates', 'A range must end on or after it starts and last at most a year.', 400)
        clean.append((first, last))
    clean.sort()
    return [{'from': a.isoformat(), 'to': b.isoformat()} for a, b in clean]


def _away_ranges(profile):
    out = []
    for item in profile.away_dates or []:
        try:
            out.append((date.fromisoformat(item['from']), date.fromisoformat(item['to'])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def validate_profile_update(payload, current):
    """Return a dict of validated fields. current is a dict of existing values."""
    if not isinstance(payload, dict):
        raise MentoringError('invalid_payload', 'A JSON object is required.', 400)
    clean = {}
    if 'headline' in payload:
        clean['headline'] = _clean_text(payload['headline'], 'headline', 140)
    if 'bio' in payload:
        clean['bio'] = _clean_text(payload['bio'], 'bio', 2000)
    if 'topics' in payload:
        topics = payload['topics']
        if not isinstance(topics, list) or len(topics) > 10:
            raise MentoringError('invalid_field', 'topics must be a list of at most 10 items.', 400)
        clean['topics'] = [_clean_text(t, 'topic', 40, required=True) for t in topics]
    if 'timezone' in payload:
        name = _clean_text(payload['timezone'], 'timezone', 64, required=True)
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise MentoringError('invalid_field', 'Unknown timezone.', 400)
        clean['timezone'] = name
    if 'away_dates' in payload:
        clean['away_dates'] = validate_away_dates(payload['away_dates'])
    if 'session_minutes' in payload:
        minutes = payload['session_minutes']
        if (
            isinstance(minutes, bool) or not isinstance(minutes, int)
            or not MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES or minutes % 5
        ):
            raise MentoringError(
                'invalid_field',
                f'session_minutes must be a multiple of 5 from {MIN_SESSION_MINUTES} to {MAX_SESSION_MINUTES}.', 400,
            )
        clean['session_minutes'] = minutes
    if 'price_roo' in payload:
        price = payload['price_roo']
        if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= MAX_PRICE_ROO:
            raise MentoringError('invalid_field', f'price_roo must be a whole number from 1 to {MAX_PRICE_ROO}.', 400)
        clean['price_roo'] = price
    if 'location_type' in payload:
        if payload['location_type'] not in (LOCATION_MLAI_SPACE, LOCATION_VIDEO, LOCATION_EITHER):
            raise MentoringError('invalid_field', 'location_type must be mlai_space, video or either.', 400)
        clean['location_type'] = payload['location_type']
    if 'video_link' in payload:
        clean['video_link'] = _clean_url(payload['video_link'], 'video_link')
    if 'space_notes' in payload:
        clean['space_notes'] = _clean_text(payload['space_notes'], 'space_notes', 300)
    final = {**current, **clean}
    if final.get('location_type') in (LOCATION_VIDEO, LOCATION_EITHER) and not final.get('video_link'):
        raise MentoringError('video_link_required', 'Add your own video link for video sessions.', 400)
    return clean


# ------------------------------------------------------------- serialisation

def serialize_mentor(profile, *, include_private=False):
    user = profile.user
    data = {
        'id': profile.pk,
        'name': _display_name(user),
        'avatar_url': getattr(user, 'avatar_url', None) or None,
        'headline': profile.headline,
        'bio': profile.bio or (getattr(user, 'about', '') or ''),
        'topics': list(profile.topics or []),
        'timezone': profile.timezone,
        'away_dates': list(profile.away_dates or []),
        'session_minutes': profile.session_minutes,
        'price_roo': profile.price_roo,
        'location_type': profile.location_type,
    }
    if include_private:
        data.update({
            'is_active': profile.is_active,
            'video_link': profile.video_link,
            'space_notes': profile.space_notes,
        })
    return data


def serialize_session(session, viewer):
    is_mentor = session.mentor.user_id == viewer.pk
    show_location = is_mentor or session.status in (
        MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED,
    )
    return {
        'id': str(session.pk),
        'role': 'mentor' if is_mentor else 'founder',
        'status': session.status,
        'starts_at': session.starts_at.isoformat(),
        'ends_at': session.ends_at.isoformat(),
        'expires_at': session.expires_at.isoformat(),
        'price_roo': session.price_roo,
        'location_type': session.location_type,
        'location_detail': session.location_detail if show_location else '',
        'mentor': {
            'id': session.mentor_id,
            'name': _display_name(session.mentor.user),
            'avatar_url': getattr(session.mentor.user, 'avatar_url', None) or None,
        },
        'founder': {
            'name': _display_name(session.founder),
            'avatar_url': getattr(session.founder, 'avatar_url', None) or None,
        },
        'brief': {
            'stage': session.brief_stage,
            'struggle': session.brief_struggle,
            'outcome': session.brief_outcome,
            'link': session.brief_link,
        },
        'requested_at': session.requested_at.isoformat(),
        'responded_at': session.responded_at.isoformat() if session.responded_at else None,
    }


# ------------------------------------------------------------------- points

def _hold_description(session):
    return f'Mentor session {session.pk} with mentor {session.mentor_id}'


def _release_hold(session, *, actor_slack_id):
    """Refund the founder exactly once. Safe to call again (idempotency key)."""
    if session.hold_ledger_entry_id is None:
        return
    ledger, _ = PointsService.refund(
        user=session.founder,
        delta=session.price_roo,
        source=LEDGER_SOURCE,
        description=f'Refund for mentor session {session.pk}',
        created_by_slack_id=actor_slack_id or '',
        idempotency_key=f'mentor_release:{session.pk}',
        reference_type=LEDGER_REFERENCE_TYPE,
        reference_id=str(session.pk),
        purchased_delta_microroo=session.purchased_points_cost_microroo,
        reverse_lifetime_spent=True,
    )
    session.release_ledger_entry = ledger


def _pay_mentor(session):
    ledger, _ = PointsService.award(
        user=session.mentor.user,
        delta=session.price_roo,
        source=LEDGER_SOURCE,
        description=f'Mentor session {session.pk} completed',
        created_by_slack_id=session.mentor.user.slack_id or '',
        idempotency_key=f'mentor_payout:{session.pk}',
        reference_type=LEDGER_REFERENCE_TYPE,
        reference_id=str(session.pk),
    )
    session.payout_ledger_entry = ledger


def _due_filter(now, auto_complete_hours):
    """Sessions that a lazy read should settle."""
    return (
        Q(status=MentorSession.STATUS_REQUESTED, expires_at__lte=now)
        | Q(status=MentorSession.STATUS_ACCEPTED, ends_at__lte=now - timedelta(hours=auto_complete_hours))
    )


# ------------------------------------------------------------------- service

class MentoringService:
    # ---- reads

    @staticmethod
    def list_mentors(viewer):
        profiles = (
            MentorProfile.objects.filter(is_active=True)
            .select_related('user')
            .order_by('user__first_name', 'user__last_name', 'pk')[:100]
        )
        return [
            {**serialize_mentor(p), 'is_me': p.user_id == viewer.pk}
            for p in profiles
        ]

    @staticmethod
    def _get_active_mentor(mentor_id):
        try:
            return MentorProfile.objects.select_related('user').get(pk=mentor_id, is_active=True)
        except (MentorProfile.DoesNotExist, ValueError, TypeError):
            raise MentoringError('mentor_not_found', 'Mentor not found.', 404)

    @classmethod
    def free_slots(cls, mentor, first_day, last_day, now=None, settle_first=True):
        now = now or timezone.now()
        problem = scheduling.validate_range(first_day, last_day)
        if problem:
            raise MentoringError('invalid_range', problem, 400)
        if settle_first:
            cls.sweep(mentor=mentor)
        rules = [
            (r.weekday, r.start_time, r.end_time)
            for r in mentor.availability_rules.all()
        ]
        busy = MentorSession.objects.filter(
            mentor=mentor, status__in=MentorSession.ACTIVE_STATUSES,
            ends_at__gt=now,
        ).values_list('starts_at', 'ends_at')
        return scheduling.generate_slots(
            rules=rules, tz_name=mentor.timezone, session_minutes=mentor.session_minutes,
            first_day=first_day, last_day=last_day, busy=list(busy), now=now,
            away=_away_ranges(mentor),
        )

    @classmethod
    def availability(cls, mentor_id, first_day=None, last_day=None):
        mentor = cls._get_active_mentor(mentor_id)
        now = timezone.now()
        tz = ZoneInfo(mentor.timezone)
        first_day = first_day or now.astimezone(tz).date()
        last_day = last_day or first_day + timedelta(days=13)
        slots = cls.free_slots(mentor, first_day, last_day, now=now)
        return {
            'mentor_id': mentor.pk,
            'timezone': mentor.timezone,
            'session_minutes': mentor.session_minutes,
            'price_roo': mentor.price_roo,
            'slots': [s.isoformat() for s in slots],
        }

    @classmethod
    def list_sessions(cls, user, role, status=None):
        cls.sweep(user=user)
        query = MentorSession.objects.select_related('mentor__user', 'founder')
        if role == 'mentor':
            query = query.filter(mentor__user=user)
        else:
            query = query.filter(founder=user)
        if status:
            query = query.filter(status=status)
        return [serialize_session(s, user) for s in query.order_by('-starts_at')[:200]]

    # ---- lazy settlement

    @classmethod
    def sweep(cls, *, user=None, mentor=None):
        """Expire or auto-complete due sessions for a user or mentor."""
        now = timezone.now()
        query = MentorSession.objects.all()
        if mentor is not None:
            query = query.filter(mentor=mentor)
        if user is not None:
            query = query.filter(Q(founder=user) | Q(mentor__user=user))
        due = query.filter(
            _due_filter(now, _auto_complete_hours())
        ).values_list('pk', flat=True)
        for pk in list(due[:50]):
            try:
                cls.settle(pk)
            except Exception:
                # A failed settlement must not break a read; it retries next time.
                logger.exception('mentor session settlement failed for %s', pk)

    @classmethod
    def settle(cls, session_id):
        with transaction.atomic():
            session = (
                MentorSession.objects.select_for_update()
                .select_related('mentor__user', 'founder').get(pk=session_id)
            )
            return cls._apply_settlement(session)

    @classmethod
    def _apply_settlement(cls, session):
        now = timezone.now()
        action = scheduling.settlement_action(
            status=session.status, now=now, starts_at=session.starts_at,
            ends_at=session.ends_at, expires_at=session.expires_at,
            auto_complete_hours=_auto_complete_hours(),
        )
        if action == 'expire':
            _release_hold(session, actor_slack_id='')
            session.status = MentorSession.STATUS_EXPIRED
            session.responded_at = now
            session.save()
        elif action == 'complete':
            _pay_mentor(session)
            session.status = MentorSession.STATUS_COMPLETED
            session.completed_at = now
            session.save()
        return session

    # ---- profile

    @staticmethod
    def get_profile(user):
        try:
            profile = MentorProfile.objects.select_related('user').get(user=user)
        except MentorProfile.DoesNotExist:
            raise MentoringError('not_a_mentor', 'Your account is not set up as a mentor.', 404)
        data = serialize_mentor(profile, include_private=True)
        data['availability'] = [
            {'weekday': r.weekday, 'start': r.start_time.strftime('%H:%M'), 'end': r.end_time.strftime('%H:%M')}
            for r in profile.availability_rules.all()
        ]
        return data

    @classmethod
    def update_profile(cls, user, payload):
        with transaction.atomic():
            try:
                profile = MentorProfile.objects.select_for_update().select_related('user').get(user=user)
            except MentorProfile.DoesNotExist:
                raise MentoringError('not_a_mentor', 'Your account is not set up as a mentor.', 404)
            if not profile.is_active:
                raise MentoringError('mentor_inactive', 'An admin must activate your mentor profile first.', 403)
            current = {
                'location_type': profile.location_type, 'video_link': profile.video_link,
            }
            clean = validate_profile_update(payload, current)
            windows = validate_availability(payload['availability']) if 'availability' in payload else None
            for field, value in clean.items():
                setattr(profile, field, value)
            profile.save()
            if windows is not None:
                profile.availability_rules.all().delete()
                MentorAvailabilityRule.objects.bulk_create([
                    MentorAvailabilityRule(mentor=profile, weekday=w, start_time=s, end_time=e)
                    for w, s, e in windows
                ])
        return cls.get_profile(user)

    # ---- request

    @staticmethod
    def _resolve_location(mentor, requested):
        if mentor.location_type == LOCATION_EITHER:
            if requested not in (LOCATION_MLAI_SPACE, LOCATION_VIDEO):
                raise MentoringError('location_required', 'Choose mlai_space or video.', 400)
            chosen = requested
        else:
            if requested not in (None, '', mentor.location_type):
                raise MentoringError('location_unavailable', 'This mentor does not offer that location.', 409)
            chosen = mentor.location_type
        detail = mentor.video_link if chosen == LOCATION_VIDEO else mentor.space_notes
        return chosen, detail

    @classmethod
    def request_session(cls, *, founder, mentor_id, starts_at, location_type, brief,
                        client_request_id, expected_price=None):
        starts_at = parse_aware_datetime(starts_at)
        request_id = parse_request_id(client_request_id)
        brief_fields = validate_brief(brief)
        mentor = cls._get_active_mentor(mentor_id)
        if mentor.user_id == founder.pk:
            raise MentoringError('own_mentor_profile', 'You cannot book yourself.', 409)
        cls.sweep(mentor=mentor)
        now = timezone.now()
        try:
            with transaction.atomic():
                _lock_keys(f'mentoring:mentor:{mentor.pk}', f'mentoring:request:{request_id}')
                existing = (
                    MentorSession.objects.select_for_update()
                    .select_related('mentor__user', 'founder')
                    .filter(founder=founder, client_request_id=request_id).first()
                )
                if existing:
                    return cls._replay(existing, mentor, starts_at), False
                mentor = MentorProfile.objects.select_for_update().select_related('user').get(pk=mentor.pk)
                if not mentor.is_active:
                    raise MentoringError('mentor_not_found', 'Mentor not found.', 404)
                if expected_price is not None and expected_price != mentor.price_roo:
                    raise MentoringError('price_changed', 'The price has changed. Please review it.', 409)
                chosen, detail = cls._resolve_location(mentor, location_type)
                ends_at = starts_at + timedelta(minutes=mentor.session_minutes)
                day = starts_at.astimezone(ZoneInfo(mentor.timezone)).date()
                slots = cls.free_slots(mentor, day - timedelta(days=1), day + timedelta(days=1),
                                       now=now, settle_first=False)
                if starts_at not in slots:
                    raise MentoringError('slot_unavailable', 'That time is no longer available.', 409)
                if MentorSession.objects.filter(
                    founder=founder, status__in=MentorSession.ACTIVE_STATUSES,
                    starts_at__lt=ends_at, ends_at__gt=starts_at,
                ).exists():
                    raise MentoringError('founder_busy', 'You already have a mentor session at that time.', 409)

                account = PointsAccount.objects.select_for_update().filter(user=founder).first()
                if account is not None:
                    PointsService._ensure_microroo_account(account)
                price_microroo = PointsService.roo_to_microroo(mentor.price_roo)
                purchased = min(account.purchased_topup_balance_microroo if account else 0, price_microroo)
                session = MentorSession(
                    mentor=mentor, founder=founder, starts_at=starts_at, ends_at=ends_at,
                    price_roo=mentor.price_roo, purchased_points_cost_microroo=purchased,
                    location_type=chosen, location_detail=detail,
                    client_request_id=request_id, requested_at=now,
                    expires_at=scheduling.request_expiry(now=now, starts_at=starts_at, ttl_hours=_ttl_hours()),
                    **brief_fields,
                )
                session.save()
                ledger, _ = PointsService.spend(
                    user=founder, delta=session.price_roo, source=LEDGER_SOURCE,
                    description=_hold_description(session),
                    created_by_slack_id=founder.slack_id or '',
                    idempotency_key=f'mentor_hold:{session.pk}',
                    reference_type=LEDGER_REFERENCE_TYPE, reference_id=str(session.pk),
                )
                session.hold_ledger_entry = ledger
                session.save(update_fields=['hold_ledger_entry', 'updated_at'])
        except InsufficientBalanceError:
            raise MentoringError('insufficient_points', "You don't have enough Roo Points for this session.", 409)
        except IntegrityError:
            existing = MentorSession.objects.select_related('mentor__user', 'founder').filter(
                founder=founder, client_request_id=request_id).first()
            if existing is None:
                raise
            return cls._replay(existing, mentor, starts_at), False
        try:
            cls.notify_mentor(session.pk)
        except Exception:
            # Notification trouble must never fail or alter a committed booking.
            logger.exception('mentor notification raised for %s', session.pk)
        return session, True

    @staticmethod
    def _replay(existing, mentor, starts_at):
        if existing.mentor_id != mentor.pk or existing.starts_at != starts_at:
            raise MentoringError('idempotency_conflict', 'That request id was used for a different session.', 409)
        return existing

    # ---- notification (best effort, never touches status)

    @classmethod
    def notify_mentor(cls, session_id):
        outcome = MentorSession.NOTIFY_FAILED
        try:
            session = MentorSession.objects.select_related('mentor__user', 'founder').get(pk=session_id)
            slack_id = session.mentor.user.slack_id
            if not slack_id:
                outcome = MentorSession.NOTIFY_SKIPPED
            else:
                from integrations.services.slack import SlackService
                text = (
                    f'New mentor request from {_display_name(session.founder)} for '
                    f'{session.starts_at.astimezone(ZoneInfo(session.mentor.timezone)):%a %d %b %H:%M} '
                    f'({session.mentor.timezone}). Open MLAI Chat to accept or decline. '
                    f'It expires {session.expires_at:%d %b %H:%M} UTC.'
                )
                ok, _ts = SlackService.send_dm(slack_id, text)
                outcome = MentorSession.NOTIFY_SENT if ok else MentorSession.NOTIFY_FAILED
        except Exception:
            logger.exception('mentor request notification failed for %s', session_id)
        try:
            MentorSession.objects.filter(pk=session_id).update(
                notification_status=outcome, notification_attempted_at=timezone.now(),
            )
        except Exception:
            logger.exception('could not record notification outcome for %s', session_id)
        return outcome

    # ---- transitions

    @classmethod
    def _load_for_update(cls, session_id, actor):
        try:
            session = (
                MentorSession.objects.select_for_update()
                .select_related('mentor__user', 'founder').get(pk=session_id)
            )
        except (MentorSession.DoesNotExist, ValueError, TypeError):
            raise MentoringError('session_not_found', 'Session not found.', 404)
        if actor.pk not in (session.founder_id, session.mentor.user_id):
            raise MentoringError('session_not_found', 'Session not found.', 404)
        return session

    @classmethod
    def _transition(cls, session_id, actor, *, require_mentor, target):
        with transaction.atomic():
            session = cls._load_for_update(session_id, actor)
            _lock_keys(f'mentoring:mentor:{session.mentor_id}')
            if require_mentor and session.mentor.user_id != actor.pk:
                raise MentoringError('not_session_mentor', 'Only the mentor can do this.', 403)
            if session.status == target:
                return session  # a retried call is a success
            session = cls._apply_settlement(session)
            if session.status == MentorSession.STATUS_EXPIRED and target != MentorSession.STATUS_EXPIRED:
                raise MentoringError('request_expired', 'This request expired and your points were refunded.', 409)
            now = timezone.now()
            handler = getattr(cls, f'_do_{target}')
            handler(session, actor, now)
            return session

    @classmethod
    def accept(cls, session_id, actor):
        return cls._transition(session_id, actor, require_mentor=True, target=MentorSession.STATUS_ACCEPTED)

    @classmethod
    def decline(cls, session_id, actor):
        return cls._transition(session_id, actor, require_mentor=True, target=MentorSession.STATUS_DECLINED)

    @classmethod
    def cancel(cls, session_id, actor):
        return cls._transition(session_id, actor, require_mentor=False, target=MentorSession.STATUS_CANCELLED)

    @classmethod
    def complete(cls, session_id, actor):
        return cls._transition(session_id, actor, require_mentor=True, target=MentorSession.STATUS_COMPLETED)

    @staticmethod
    def _need(session, *allowed):
        if session.status not in allowed:
            raise MentoringError(
                'invalid_state', f'This session is {session.status} and cannot be changed that way.', 409,
            )

    @classmethod
    def _do_accepted(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_REQUESTED)
        if now >= session.starts_at:
            raise MentoringError('session_started', 'The session start time has passed.', 409)
        if MentorSession.objects.filter(
            mentor=session.mentor, status=MentorSession.STATUS_ACCEPTED,
            starts_at__lt=session.ends_at, ends_at__gt=session.starts_at,
        ).exclude(pk=session.pk).exists():
            raise MentoringError('slot_unavailable', 'You already accepted a session at that time.', 409)
        session.status = MentorSession.STATUS_ACCEPTED
        session.responded_at = now
        session.save()

    @classmethod
    def _do_declined(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_REQUESTED)
        _release_hold(session, actor_slack_id=actor.slack_id)
        session.status = MentorSession.STATUS_DECLINED
        session.responded_at = now
        session.save()

    @classmethod
    def _do_cancelled(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_REQUESTED, MentorSession.STATUS_ACCEPTED)
        if now >= session.starts_at:
            raise MentoringError('session_started', 'A session cannot be cancelled after it starts.', 409)
        _release_hold(session, actor_slack_id=actor.slack_id)
        session.status = MentorSession.STATUS_CANCELLED
        session.cancelled_at = now
        session.cancelled_by = actor
        session.save()

    @classmethod
    def _do_completed(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_ACCEPTED)
        if now < session.starts_at:
            raise MentoringError('session_not_started', 'The session has not started yet.', 409)
        _pay_mentor(session)
        session.status = MentorSession.STATUS_COMPLETED
        session.completed_at = now
        session.save()

