"""Business logic for Vibe Mentoring.

Patterns are copied from roo.meeting_rooms.MeetingRoomService: errors carry a
code, message and HTTP status; a request id makes creation idempotent; points
move in the same transaction as the session row; a Postgres advisory lock
serialises writers for one mentor.

Points flow (all via PointsService, source MANUAL, reference_type MENTOR_SESSION
so no roo migration is needed):
  request  -> nothing is charged (only an early balance check)
  accept   -> spend  key mentor_charge:{id}   founder pays the price snapshot
  cancel after accept, before the start time -> refund key mentor_release:{id}
  cancel at or after the start time -> refused (session_started), by either side
  decline / expiry / cancel before accept -> no points move
  complete -> award  key mentor_payout:{id}   mentor receives the price
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

from . import google, scheduling
from .models import (
    hash_invite_token,
    DEFAULT_PRICE_ROO,
    LOCATION_MLAI_SPACE,
    LOCATION_VIDEO,
    MAX_SESSION_MINUTES,
    MIN_SESSION_MINUTES,
    MentorAvailabilityRule,
    MentorInvite,
    MentorProfile,
    MentorSession,
)

logger = logging.getLogger(__name__)

LEDGER_SOURCE = 'MANUAL'
LEDGER_REFERENCE_TYPE = 'MENTOR_SESSION'
MAX_PRICE_ROO = 1000
MAX_AVAILABILITY_WINDOWS = 28
MAX_AWAY_RANGES = 20
# A pending meeting claim older than this is stale (the process died) and may be reclaimed.
MEETING_CLAIM_TIMEOUT_SECONDS = 120
_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


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


def parse_mentor_id(value):
    # bool is an int subclass: JSON true must not resolve to mentor 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MentoringError('invalid_mentor_id', 'mentor_id must be a whole number.', 400)
    return value


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


MAX_BRIEF_QUESTIONS = 8
MAX_QUESTION_TEXT = 200
MAX_ANSWER_LENGTH = 1000
_QUESTION_ID_RE = re.compile(r'^[a-z0-9_-]{1,40}$')

# The questions every mentor without a custom list gets. Ids double as the
# legacy brief keys, so old clients that send {stage, struggle, outcome, link}
# keep working.
DEFAULT_BRIEF_QUESTIONS = (
    {'id': 'stage', 'text': 'Where is your startup at?', 'required': True, 'max_length': 200},
    {'id': 'struggle', 'text': 'What is the hardest thing right now?', 'required': True, 'max_length': 1000},
    {'id': 'outcome', 'text': 'What would you like out of this session?', 'required': True, 'max_length': 1000},
    {'id': 'link', 'text': 'A link to share (optional)', 'required': False, 'max_length': 500, 'kind': 'link'},
)
_LEGACY_BRIEF_FIELDS = {
    'stage': 'brief_stage', 'struggle': 'brief_struggle',
    'outcome': 'brief_outcome', 'link': 'brief_link',
}


def effective_brief_questions(stored):
    """The mentor's questions, or the default four when none are set."""
    if not stored:
        return [dict(q) for q in DEFAULT_BRIEF_QUESTIONS]
    return [
        {'id': q['id'], 'text': q['text'], 'required': bool(q.get('required')),
         'max_length': MAX_ANSWER_LENGTH}
        for q in stored
    ]


def validate_brief_questions(value):
    """Validate a mentor's question list for storage. An empty list resets to defaults."""
    if not isinstance(value, list) or len(value) > MAX_BRIEF_QUESTIONS:
        raise MentoringError(
            'invalid_brief_questions',
            f'brief_questions must be a list of at most {MAX_BRIEF_QUESTIONS} questions.', 400,
        )
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise MentoringError('invalid_brief_questions', 'Each question must be an object.', 400)
        text = _clean_text(item.get('text'), 'question', MAX_QUESTION_TEXT, required=True)
        required = item.get('required', True)
        if not isinstance(required, bool):
            raise MentoringError('invalid_brief_questions', 'required must be true or false.', 400)
        qid = item.get('id')
        if not isinstance(qid, str) or not _QUESTION_ID_RE.match(qid) or qid in seen:
            qid = uuid.uuid4().hex[:12]
        seen.add(qid)
        out.append({'id': qid, 'text': text, 'required': required})
    return out


def validate_brief(brief, questions=None):
    """Validate answers against the mentor's questions.

    Returns the model fields for a new session: the snapshot in brief_answers
    (question text copied in) and, for the default questions, the legacy fields.
    """
    if not isinstance(brief, dict):
        raise MentoringError('invalid_brief', 'A brief is required.', 400)
    custom = bool(questions)
    effective = effective_brief_questions(questions)
    raw_answers = brief.get('answers')
    if raw_answers is not None:
        if not isinstance(raw_answers, list):
            raise MentoringError('invalid_brief', 'answers must be a list.', 400)
        by_id = {}
        for item in raw_answers:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                raise MentoringError('invalid_brief', 'Each answer needs an id.', 400)
            by_id[item['id']] = item.get('answer')
    else:
        by_id = {q['id']: brief.get(q['id']) for q in effective}
    snapshot = []
    for q in effective:
        label = f"brief.{q['id']}" if not custom else f"answer to '{q['text']}'"
        if q.get('kind') == 'link':
            answer = _clean_url(by_id.get(q['id']), label)
        else:
            answer = _clean_text(by_id.get(q['id']), label, q['max_length'], required=q['required'])
        snapshot.append({'id': q['id'], 'question': q['text'], 'required': q['required'], 'answer': answer})
    fields = {'brief_answers': snapshot}
    for item in snapshot:
        if not custom and item['id'] in _LEGACY_BRIEF_FIELDS:
            fields[_LEGACY_BRIEF_FIELDS[item['id']]] = item['answer']
    return fields


def session_brief_answers(session):
    """Question and answer pairs for a session, old sessions included."""
    if getattr(session, "brief_answers", None):
        return [
            {'id': a['id'], 'question': a['question'], 'required': bool(a.get('required')), 'answer': a['answer']}
            for a in session.brief_answers
        ]
    return [
        {'id': q['id'], 'question': q['text'], 'required': q['required'],
         'answer': getattr(session, _LEGACY_BRIEF_FIELDS[q['id']])}
        for q in DEFAULT_BRIEF_QUESTIONS
    ]


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
        if payload['location_type'] not in (LOCATION_MLAI_SPACE, LOCATION_VIDEO):
            raise MentoringError('invalid_field', 'location_type must be mlai_space or video.', 400)
        clean['location_type'] = payload['location_type']
    if 'busy_calendar_email' in payload:
        email = _clean_text(payload['busy_calendar_email'], 'busy_calendar_email', 254).lower()
        if email and not _EMAIL_RE.match(email):
            raise MentoringError('invalid_field', 'busy_calendar_email must be an email address.', 400)
        clean['busy_calendar_email'] = email
    if 'space_notes' in payload:
        clean['space_notes'] = _clean_text(payload['space_notes'], 'space_notes', 300)
    if 'brief_questions' in payload:
        clean['brief_questions'] = validate_brief_questions(payload['brief_questions'])
    # video_link no longer exists: video sessions always get an automatic Meet link.
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
        'brief_questions': effective_brief_questions(profile.brief_questions),
    }
    if include_private:
        data.update({
            'is_active': profile.is_active,
            'busy_calendar_email': profile.busy_calendar_email,
            'space_notes': profile.space_notes,
        })
    return data


def _is_stale_pending(session, now=None):
    """A pending accepted session whose claim is missing or too old: the attempt died."""
    if session.status != MentorSession.STATUS_ACCEPTED or session.meeting_status != MentorSession.MEETING_PENDING:
        return False
    claimed = session.meeting_claimed_at
    stale_before = (now or timezone.now()) - timedelta(seconds=MEETING_CLAIM_TIMEOUT_SECONDS)
    return claimed is None or claimed <= stale_before


def serialize_session(session, viewer):
    is_mentor = session.mentor.user_id == viewer.pk
    after_acceptance = session.status in (
        MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED,
    )
    show_location = is_mentor or after_acceptance
    is_video = session.location_type == LOCATION_VIDEO
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
        # Only the two participants ever load a session, and the link is shown
        # only once the session is accepted (or completed).
        'meet_link': (session.meet_link or '') if (is_video and after_acceptance) else '',
        # A stale pending claim is shown as failed so the mentor is offered Retry (read only, no write).
        'meeting_status': (
            ('failed' if _is_stale_pending(session) else session.meeting_status)
            if (is_video and after_acceptance) else None
        ),
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
        'brief_answers': session_brief_answers(session),
        'requested_at': session.requested_at.isoformat(),
        'responded_at': session.responded_at.isoformat() if session.responded_at else None,
    }


# ------------------------------------------------------------------- points

def _refund_charge(session, *, actor_slack_id):
    """Refund the founder if (and only if) they were charged. Idempotent by key."""
    if session.charge_ledger_entry_id is None:
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


def _charge_founder(session):
    """Spend the price snapshot from the founder when the mentor accepts."""
    account = PointsAccount.objects.select_for_update().filter(user=session.founder).first()
    if account is not None:
        PointsService._ensure_microroo_account(account)
    price_microroo = PointsService.roo_to_microroo(session.price_roo)
    purchased = min(account.purchased_topup_balance_microroo if account else 0, price_microroo)
    try:
        ledger, _ = PointsService.spend(
            user=session.founder, delta=session.price_roo, source=LEDGER_SOURCE,
            description=f'Mentor session {session.pk} with mentor {session.mentor_id}',
            created_by_slack_id=session.founder.slack_id or '',
            idempotency_key=f'mentor_charge:{session.pk}',
            reference_type=LEDGER_REFERENCE_TYPE, reference_id=str(session.pk),
            purchased_delta_microroo=purchased,
        )
    except InsufficientBalanceError:
        raise MentoringError(
            'founder_insufficient_points',
            'The founder no longer has enough Roo Points. The request stays open.', 409,
        )
    session.charge_ledger_entry = ledger
    session.purchased_points_cost_microroo = purchased


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
        """Free slots from weekly rules and bookings only (no Google call)."""
        return cls._free_slots(mentor, first_day, last_day, now, settle_first, with_google=False)[0]

    @classmethod
    def _google_busy(cls, mentor, first_day, last_day):
        """Return (busy intervals, busy_check) where busy_check is ok, failed or off."""
        email = getattr(mentor, 'busy_calendar_email', '') or ''
        if not email or not google.is_enabled():
            return [], 'off'
        window_start = datetime.combine(first_day - timedelta(days=1), time.min, tzinfo=dt_timezone.utc)
        window_end = datetime.combine(last_day + timedelta(days=2), time.min, tzinfo=dt_timezone.utc)
        try:
            return google.busy_times(email, window_start, window_end), 'ok'
        except Exception as exc:
            logger.warning('free/busy failed for mentor %s: %s', mentor.pk, getattr(exc, 'code', exc.__class__.__name__))
            return [], 'failed'

    @classmethod
    def _free_slots(cls, mentor, first_day, last_day, now=None, settle_first=True, with_google=False):
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
        busy = list(busy)
        busy_check = 'off'
        if with_google:
            google_busy, busy_check = cls._google_busy(mentor, first_day, last_day)
            busy += google_busy
        slots = scheduling.generate_slots(
            rules=rules, tz_name=mentor.timezone, session_minutes=mentor.session_minutes,
            first_day=first_day, last_day=last_day, busy=busy, now=now,
            away=_away_ranges(mentor),
        )
        return slots, busy_check

    @classmethod
    def availability(cls, mentor_id, first_day=None, last_day=None):
        mentor = cls._get_active_mentor(mentor_id)
        now = timezone.now()
        tz = ZoneInfo(mentor.timezone)
        first_day = first_day or now.astimezone(tz).date()
        last_day = last_day or first_day + timedelta(days=13)
        slots, busy_check = cls._free_slots(mentor, first_day, last_day, now=now, with_google=True)
        return {
            'mentor_id': mentor.pk,
            'timezone': mentor.timezone,
            'session_minutes': mentor.session_minutes,
            'price_roo': mentor.price_roo,
            'busy_check': busy_check,
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
            current = {'location_type': profile.location_type}
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

    # ---- invite-only mentor application

    @staticmethod
    def _load_invite(token, user, *, lock=False):
        """The invite for this token, or a MentoringError. Never consumes it."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise MentoringError('invite_invalid', 'This invite link is not valid.', 404)
        query = MentorInvite.objects.select_for_update() if lock else MentorInvite.objects
        try:
            invite = query.get(token_hash=hash_invite_token(token))
        except MentorInvite.DoesNotExist:
            raise MentoringError('invite_invalid', 'This invite link is not valid.', 404)
        if invite.user_id != user.pk:
            raise MentoringError(
                'invite_wrong_user',
                'This invite was sent to a different account. Sign in as the invited person.', 403,
            )
        if invite.revoked_at is not None:
            raise MentoringError('invite_revoked', 'This invite is no longer valid.', 410)
        return invite

    @classmethod
    def check_invite(cls, user, token):
        """State of an invite for its own user: ready, submitted or approved."""
        invite = cls._load_invite(token, user)
        profile = MentorProfile.objects.filter(user=user).first()
        if invite.used_at is not None:
            state = 'approved' if profile is not None and profile.is_active else 'submitted'
            return {'state': state, 'expires_at': invite.expires_at.isoformat()}
        if invite.expires_at <= timezone.now():
            raise MentoringError('invite_expired', 'This invite has expired. Ask MLAI for a new one.', 410)
        if profile is not None and profile.is_active:
            return {'state': 'approved', 'expires_at': invite.expires_at.isoformat()}
        return {
            'state': 'ready',
            'expires_at': invite.expires_at.isoformat(),
            'default_price_roo': DEFAULT_PRICE_ROO,
        }

    @classmethod
    def submit_application(cls, user, payload):
        """Create or update the user's profile as pending (is_active False)."""
        token = payload.get('token') if isinstance(payload, dict) else None
        with transaction.atomic():
            invite = cls._load_invite(token, user, lock=True)
            if invite.used_at is not None:
                raise MentoringError('invite_used', 'This invite has already been used.', 409)
            now = timezone.now()
            if invite.expires_at <= now:
                raise MentoringError('invite_expired', 'This invite has expired. Ask MLAI for a new one.', 410)
            profile = MentorProfile.objects.select_for_update().filter(user=user).first()
            if profile is not None and profile.is_active:
                raise MentoringError('already_a_mentor', 'You are already an active mentor.', 409)
            fields = {k: v for k, v in payload.items() if k not in ('token', 'away_dates', 'is_active')}
            fields.setdefault('price_roo', DEFAULT_PRICE_ROO)
            clean = validate_profile_update(fields, {})
            windows = validate_availability(fields.get('availability'))
            for name in ('headline', 'bio', 'topics', 'session_minutes', 'location_type'):
                if not clean.get(name):
                    raise MentoringError('invalid_field', f'{name} is required.', 400)
            if not windows:
                raise MentoringError('invalid_field', 'Add at least one weekly availability window.', 400)
            if profile is None:
                profile = MentorProfile(user=user)
            for field, value in clean.items():
                setattr(profile, field, value)
            profile.is_active = False  # staff approve by ticking is_active in admin
            profile.save()
            profile.availability_rules.all().delete()
            MentorAvailabilityRule.objects.bulk_create([
                MentorAvailabilityRule(mentor=profile, weekday=w, start_time=st, end_time=en)
                for w, st, en in windows
            ])
            invite.used_at = now
            invite.save(update_fields=['used_at'])
        return {'state': 'submitted'}

    # ---- request

    @staticmethod
    def _location(mentor):
        """The mentor sets one location; the founder does not choose."""
        # Video sessions get an automatic Meet link on accept, so no detail here.
        detail = '' if mentor.location_type == LOCATION_VIDEO else mentor.space_notes
        return mentor.location_type, detail

    @classmethod
    def request_session(cls, *, founder, mentor_id, starts_at, brief,
                        client_request_id, expected_price=None):
        mentor_id = parse_mentor_id(mentor_id)
        starts_at = parse_aware_datetime(starts_at)
        request_id = parse_request_id(client_request_id)
        if isinstance(expected_price, bool) or not isinstance(expected_price, int):
            raise MentoringError(
                'expected_price_required',
                'expected_price (the whole-number price you were shown) is required.', 400,
            )
        if not isinstance(brief, dict):
            raise MentoringError('invalid_brief', 'A brief is required.', 400)
        # A retry of an already created request is answered from the stored
        # session, even if the mentor was deactivated or the price changed since.
        previous = (
            MentorSession.objects.select_related('mentor__user', 'founder')
            .filter(founder=founder, client_request_id=request_id).first()
        )
        if previous:
            return cls._replay(previous, mentor_id, starts_at), False
        mentor = cls._get_active_mentor(mentor_id)
        if mentor.user_id == founder.pk:
            raise MentoringError('own_mentor_profile', 'You cannot book yourself.', 409)
        cls.sweep(mentor=mentor)
        now = timezone.now()
        try:
            with transaction.atomic():
                _lock_keys(
                    f'mentoring:mentor:{mentor.pk}', f'mentoring:founder:{founder.pk}',
                    f'mentoring:request:{request_id}',
                )
                existing = (
                    MentorSession.objects.select_for_update()
                    .select_related('mentor__user', 'founder')
                    .filter(founder=founder, client_request_id=request_id).first()
                )
                if existing:
                    return cls._replay(existing, mentor.pk, starts_at), False
                mentor = MentorProfile.objects.select_for_update().select_related('user').get(pk=mentor.pk)
                if not mentor.is_active:
                    raise MentoringError('mentor_not_found', 'Mentor not found.', 404)
                if expected_price != mentor.price_roo:
                    raise MentoringError('price_changed', 'The price has changed. Please review it.', 400)
                brief_fields = validate_brief(brief, mentor.brief_questions)
                chosen, detail = cls._location(mentor)
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

                # Nothing is charged now; this only fails early for a founder who
                # clearly cannot pay. The charge happens when the mentor accepts.
                if PointsService.get_available_microroo(founder) < PointsService.roo_to_microroo(mentor.price_roo):
                    raise InsufficientBalanceError('not enough points')
                session = MentorSession(
                    mentor=mentor, founder=founder, starts_at=starts_at, ends_at=ends_at,
                    price_roo=mentor.price_roo,
                    location_type=chosen, location_detail=detail,
                    client_request_id=request_id, requested_at=now,
                    expires_at=scheduling.request_expiry(now=now, starts_at=starts_at, ttl_hours=_ttl_hours()),
                    **brief_fields,
                )
                session.save()
        except InsufficientBalanceError:
            raise MentoringError('insufficient_points', "You don't have enough Roo Points for this session.", 409)
        except IntegrityError:
            existing = MentorSession.objects.select_related('mentor__user', 'founder').filter(
                founder=founder, client_request_id=request_id).first()
            if existing is None:
                raise
            return cls._replay(existing, mentor_id, starts_at), False
        # After commit, in this request thread, best effort. Under
        # ATOMIC_REQUESTS (staging) Django runs it when the request commits.
        transaction.on_commit(lambda: cls._notify_safely(session.pk))
        return session, True

    @classmethod
    def _notify_safely(cls, session_id):
        try:
            cls.notify_mentor(session_id)
        except Exception:
            # Notification trouble must never fail or alter a committed booking.
            logger.exception('mentor notification raised for %s', session_id)

    @staticmethod
    def _replay(existing, mentor_id, starts_at):
        if existing.mentor_id != mentor_id or existing.starts_at != starts_at:
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
                from mlai.staging_integrations import require_staging_capability
                require_staging_capability('slack')
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

    # ---- meeting (best effort, never touches status)

    @classmethod
    def _create_meeting_safely(cls, session_id):
        try:
            cls.create_meeting(session_id)
        except Exception:
            logger.exception('mentor meeting creation raised for %s', session_id)

    @classmethod
    def create_meeting(cls, session_id):
        """Create the Meet link for an accepted video session. Returns the meeting status."""
        outcome, error, fields = MentorSession.MEETING_FAILED, 'unexpected', {}
        event_id = ''
        try:
            session = MentorSession.objects.select_related('mentor__user', 'founder').get(pk=session_id)
            if (
                session.status != MentorSession.STATUS_ACCEPTED
                or session.location_type != LOCATION_VIDEO
                or session.meet_link
            ):
                return session.meeting_status
            if not google.is_enabled():
                outcome, error = MentorSession.MEETING_UNAVAILABLE, 'google_off'
            else:
                def remember_event(known_id):
                    # Persist the event id before Google may create it, so a cancel
                    # can delete it even if this attempt dies or the reply is lost.
                    nonlocal event_id
                    event_id = known_id
                    MentorSession.objects.filter(
                        pk=session_id, status=MentorSession.STATUS_ACCEPTED,
                    ).update(calendar_event_id=known_id)

                try:
                    result = google.create_meeting(session, on_event=remember_event)
                    event_id = result['calendar_event_id'] or event_id
                    outcome, error = MentorSession.MEETING_CREATED, result['error']
                    fields = {
                        'meet_link': result['meet_link'], 'calendar_event_id': event_id,
                        'meeting_path': result['path'],
                    }
                except google.GoogleError as exc:
                    error = exc.code
        except Exception:
            logger.exception('mentor meeting creation failed for %s', session_id)
        try:
            saved = MentorSession.objects.filter(
                pk=session_id, status=MentorSession.STATUS_ACCEPTED,
            ).update(meeting_status=outcome, meeting_error=error, **fields)
            if not saved and event_id:
                # Cancelled while the link was being made: remove the invite.
                cls._delete_event_safely(session_id, event_id)
        except Exception:
            logger.exception('could not record meeting outcome for %s', session_id)
        return outcome

    @classmethod
    def _delete_event_safely(cls, session_id, event_id):
        try:
            if google.is_enabled():
                google.delete_event(event_id)
        except Exception:
            logger.exception('could not remove calendar event for %s', session_id)

    @classmethod
    def retry_meeting(cls, session_id, actor):
        """Mentor asks for the Meet link again after a failure."""
        session = cls._load_session(session_id, actor)
        if session.mentor.user_id != actor.pk:
            raise MentoringError('not_session_mentor', 'Only the mentor can do this.', 403)
        if session.status != MentorSession.STATUS_ACCEPTED or session.location_type != LOCATION_VIDEO:
            raise MentoringError('invalid_state', 'Only an accepted video session has a meeting.', 409)
        if session.meeting_status == MentorSession.MEETING_CREATED and session.meet_link:
            return session  # a retried call is a success
        now = timezone.now()
        stale_before = now - timedelta(seconds=MEETING_CLAIM_TIMEOUT_SECONDS)
        if (
            session.meeting_status == MentorSession.MEETING_PENDING
            and session.meeting_claimed_at and session.meeting_claimed_at > stale_before
        ):
            raise MentoringError('meeting_pending', 'The meeting link is still being created.', 409)
        # A pending claim with no recent timestamp is stale (the process died after
        # the commit): reclaim it. Google creation is idempotent and nothing is charged here.
        claimed = MentorSession.objects.filter(
            Q(meeting_status__in=(MentorSession.MEETING_FAILED, MentorSession.MEETING_UNAVAILABLE))
            | Q(meeting_status=MentorSession.MEETING_PENDING)
            & (Q(meeting_claimed_at__isnull=True) | Q(meeting_claimed_at__lte=stale_before)),
            pk=session.pk, status=MentorSession.STATUS_ACCEPTED,
        ).update(meeting_status=MentorSession.MEETING_PENDING, meeting_error='', meeting_claimed_at=now)
        if claimed:
            cls.create_meeting(session.pk)
        return MentorSession.objects.select_related('mentor__user', 'founder').get(pk=session.pk)

    # ---- transitions

    @classmethod
    def _load_for_update(cls, session_id, actor):
        return cls._load_session(session_id, actor, lock=True)

    @classmethod
    def _load_session(cls, session_id, actor, lock=False):
        try:
            query = MentorSession.objects.select_for_update() if lock else MentorSession.objects
            session = query.select_related('mentor__user', 'founder').get(pk=session_id)
        except (MentorSession.DoesNotExist, ValueError, TypeError):
            raise MentoringError('session_not_found', 'Session not found.', 404)
        if actor.pk not in (session.founder_id, session.mentor.user_id):
            raise MentoringError('session_not_found', 'Session not found.', 404)
        return session

    @classmethod
    def _transition(cls, session_id, actor, *, require_mentor, target):
        expired = False
        with transaction.atomic():
            session = cls._load_for_update(session_id, actor)
            _lock_keys(f'mentoring:mentor:{session.mentor_id}')
            if require_mentor and session.mentor.user_id != actor.pk:
                raise MentoringError('not_session_mentor', 'Only the mentor can do this.', 403)
            if session.status == target:
                return session  # a retried call is a success
            session = cls._apply_settlement(session)
            if session.status == MentorSession.STATUS_EXPIRED:
                # Leave the atomic block normally so the refund commits, then report.
                expired = True
            else:
                handler = getattr(cls, f'_do_{target}')
                handler(session, actor, timezone.now())
        if expired:
            raise MentoringError('request_expired', 'This request has expired.', 409)
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
        _charge_founder(session)
        session.status = MentorSession.STATUS_ACCEPTED
        session.responded_at = now
        if session.location_type == LOCATION_VIDEO:
            if google.is_enabled():
                session.meeting_status = MentorSession.MEETING_PENDING
                session.meeting_claimed_at = now
                # After the charge commits, best effort, like the Slack DM.
                pk = session.pk
                transaction.on_commit(lambda: cls._create_meeting_safely(pk))
            else:
                session.meeting_status = MentorSession.MEETING_UNAVAILABLE
                session.meeting_error = 'google_off'
        session.save()

    @classmethod
    def _do_declined(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_REQUESTED)
        # Nothing was charged for a request, so there is nothing to refund.
        session.status = MentorSession.STATUS_DECLINED
        session.responded_at = now
        session.save()

    @classmethod
    def _do_cancelled(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_REQUESTED, MentorSession.STATUS_ACCEPTED)
        # Not allowed once the session has started (operator decision 20-10-26),
        # by either side. Before the start: refund only if the founder was
        # charged (the mentor had accepted).
        if now >= session.starts_at:
            raise MentoringError('session_started', 'This session has already started and can no longer be cancelled.', 409)
        _refund_charge(session, actor_slack_id=actor.slack_id)
        if session.calendar_event_id:
            event_id = session.calendar_event_id
            transaction.on_commit(lambda: cls._delete_event_safely(session.pk, event_id))
        session.status = MentorSession.STATUS_CANCELLED
        session.cancelled_at = now
        session.cancelled_by = actor
        session.save()

    @classmethod
    def _do_completed(cls, session, actor, now):
        cls._need(session, MentorSession.STATUS_ACCEPTED)
        if now < session.ends_at:
            raise MentoringError('session_not_ended', 'The session has not ended yet.', 409)
        _pay_mentor(session)
        session.status = MentorSession.STATUS_COMPLETED
        session.completed_at = now
        session.save()

