"""Post-session feedback: ask the founder how a session went, just after it ends.

A scheduler runner finds sessions whose booked end passed a few minutes ago and
sends the founder one email (system_email, Resend) and one Slack DM from Roo,
each with three faces. A face opens a small public page (feedback_views) that
records the rating. Answering anywhere edits the Slack DM to say thanks, and a
"Not great" or "Session didn't happen" answer alerts the MLAI team in Slack.

Delivery never changes the session, and nothing here raises into the runner.
Links carry an HMAC token over the feedback row's id, so no token is stored
and a resend after a timeout always carries the same links.
"""

from __future__ import annotations

import base64
import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from .models import (
    LOCATION_VIDEO, MentorFeedbackOptOut, MentorSession, MentorSessionFeedback, is_placeholder_email,
)

logger = logging.getLogger(__name__)

MESSAGE_KEY = 'mentor_session_feedback'
PATH = '/api/v1/mentoring/feedback/'
COMMENT_MAX_LENGTH = 2000
HISTORY_LIMIT = 10
SCANNER_WINDOW = timedelta(seconds=10)
SCANNER_AFTER_SEND = timedelta(minutes=2)
MELBOURNE = ZoneInfo('Australia/Melbourne')
_TOKEN_SALT = 'mentoring.feedback.token'
_MAC_BYTES = 18

RATINGS = {
    MentorSessionFeedback.RATING_SAD: ('Not great', '\U0001F641'),
    MentorSessionFeedback.RATING_NEUTRAL: ('Okay', '\U0001F610'),
    MentorSessionFeedback.RATING_HAPPY: ('Great', '\U0001F600'),
    MentorSessionFeedback.RATING_NO_SHOW: ('Didn\u2019t happen', '\U0001F6AB'),
}
FACES = (MentorSessionFeedback.RATING_SAD, MentorSessionFeedback.RATING_NEUTRAL, MentorSessionFeedback.RATING_HAPPY)
VIA = {MentorSessionFeedback.VIA_EMAIL, MentorSessionFeedback.VIA_SLACK, MentorSessionFeedback.VIA_PAGE}
_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December')
_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


class FeedbackError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ------------------------------------------------------------------ settings

def enabled():
    return bool(getattr(settings, 'MENTOR_FEEDBACK_ENABLED', False))


def not_before():
    raw = str(getattr(settings, 'MENTOR_FEEDBACK_NOT_BEFORE', '') or '').strip()
    value = parse_datetime(raw) if raw else None
    if value is not None and timezone.is_naive(value):
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def channels():
    raw = str(getattr(settings, 'MENTOR_FEEDBACK_CHANNELS', 'email,slack') or '')
    return {part.strip().lower() for part in raw.split(',') if part.strip()}


def _int_setting(name, default, low, high):
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def delay():
    return timedelta(minutes=_int_setting('MENTOR_FEEDBACK_DELAY_MINUTES', 5, 0, 24 * 60))


def max_age():
    return timedelta(hours=_int_setting('MENTOR_FEEDBACK_MAX_AGE_HOURS', 24, 1, 24 * 14))


def max_per_tick():
    return _int_setting('MENTOR_FEEDBACK_MAX_PER_TICK', 20, 1, 200)


def token_ttl():
    return timedelta(days=_int_setting('MENTOR_FEEDBACK_TOKEN_DAYS', 30, 1, 365))


def alert_channel():
    return str(getattr(settings, 'MENTOR_FEEDBACK_ALERT_CHANNEL', '') or '').strip()


def asset_base():
    return str(getattr(settings, 'CRM_EMAIL_ASSET_BASE_URL', '') or '').rstrip('/')


def chat_url(path=''):
    base = str(getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', '') or 'https://chat.mlai.au').rstrip('/')
    return base + path


# -------------------------------------------------------------------- tokens

def _mac(feedback_id: uuid.UUID) -> bytes:
    return salted_hmac(_TOKEN_SALT, str(feedback_id), algorithm='sha256').digest()[:_MAC_BYTES]


def make_token(feedback_id) -> str:
    fid = feedback_id if isinstance(feedback_id, uuid.UUID) else uuid.UUID(str(feedback_id))
    return base64.urlsafe_b64encode(fid.bytes + _mac(fid)).decode('ascii').rstrip('=')


def parse_token(token) -> uuid.UUID | None:
    """The feedback id inside a genuine token, or None. Never raises."""
    text = str(token or '').strip()
    if not text or len(text) > 64:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != 16 + _MAC_BYTES:
        return None
    fid = uuid.UUID(bytes=raw[:16])
    if not hmac.compare_digest(raw[16:], _mac(fid)):
        return None
    return fid


def page_url(token, rating='', via=''):
    url = settings.DEFAULT_BACKEND_URL.rstrip('/') + f'{PATH}{token}/'
    query = {k: v for k, v in (('r', rating), ('src', via)) if v}
    return url + ('?' + urlencode(query) if query else '')


def unsubscribe_url(token):
    return settings.DEFAULT_BACKEND_URL.rstrip('/') + f'{PATH}{token}/unsubscribe/'


# ---------------------------------------------------------------- formatting

def _first_name(user):
    name = (getattr(user, 'first_name', '') or '').strip()
    if name:
        return name.split()[0]
    full = (getattr(user, 'full_name', '') or '').strip()
    return full.split()[0] if full else ''


def _full_name(user):
    return (getattr(user, 'full_name', '') or '').strip() or _first_name(user) or 'your mentor'


def _local(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(MELBOURNE)


def _clock(value):
    hour = value.hour % 12 or 12
    return f'{hour}:{value.minute:02d}'


def _meridiem(value):
    return 'am' if value.hour < 12 else 'pm'


def time_range(session):
    start, end = _local(session.starts_at), _local(session.ends_at)
    if _meridiem(start) == _meridiem(end):
        return f'{_clock(start)} to {_clock(end)} {_meridiem(end)}'
    return f'{_clock(start)} {_meridiem(start)} to {_clock(end)} {_meridiem(end)}'


def long_date(session):
    start = _local(session.starts_at)
    return f'{_DAYS[start.weekday()]} {start.day} {_MONTHS[start.month - 1]}'


def short_date(session):
    start = _local(session.starts_at)
    return f'{_DAYS[start.weekday()][:3]} {start.day} {_MONTHS[start.month - 1][:3]}'


def place(session):
    return 'Google Meet' if session.location_type == LOCATION_VIDEO else 'MLAI space'


def brief_line(session, limit=60):
    """The first thing the founder wrote in the brief, cut short; '' if none."""
    text = ''
    for answer in getattr(session, 'brief_answers', None) or []:
        if isinstance(answer, dict) and str(answer.get('answer') or '').strip():
            text = str(answer['answer'])
            break
    if not text:
        text = getattr(session, 'brief_stage', '') or getattr(session, 'brief_struggle', '') or ''
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def describe(row):
    """Names, times and links shared by the email, the page and Slack."""
    session = row.session
    token = make_token(row.pk)
    mentor_user = session.mentor.user
    return {
        'founder_first': _first_name(row.respondent),
        'mentor_name': _full_name(mentor_user),
        'mentor_first': _first_name(mentor_user) or 'your mentor',
        'date_long': long_date(session),
        'date_short': short_date(session),
        'time_range': time_range(session),
        'place': place(session),
        'is_video': session.location_type == LOCATION_VIDEO,
        'brief_line': brief_line(session),
        'token': token,
    }


# ------------------------------------------------------------------- runner

def due_sessions(now):
    """Booked sessions whose end passed the delay and that nobody asked about yet."""
    earliest = now - max_age()
    floor = not_before()
    if floor is not None and floor > earliest:
        earliest = floor
    asked = MentorSessionFeedback.objects.filter(
        session=OuterRef('pk'), role=MentorSessionFeedback.ROLE_FOUNDER,
    )
    return (
        MentorSession.objects
        .filter(
            status__in=(MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED),
            ends_at__lte=now - delay(), ends_at__gte=earliest,
        )
        .filter(~Exists(asked))
        .select_related('mentor__user', 'founder')
        .order_by('ends_at', 'pk')
    )


def _calendar_change_pending(session):
    from .calendar_sync import public_status
    try:
        state = public_status(session) or {}
    except Exception:
        return False
    return bool(state.get('proposed_start') or state.get('proposed_end'))


def start_feedback(session, now):
    """Create the founder's feedback row once. None when it already exists."""
    try:
        with transaction.atomic():
            return MentorSessionFeedback.objects.create(
                session=session, respondent=session.founder,
                role=MentorSessionFeedback.ROLE_FOUNDER,
                token_expires_at=now + token_ttl(),
            )
    except IntegrityError:
        return None


def run_scheduler(now=None):
    """One bounded pass: create rows for newly ended sessions, then deliver pending ones."""
    if not enabled():
        return {'status': 'skipped', 'reason': 'disabled'}
    if not_before() is None:
        return {'status': 'skipped', 'reason': 'not_before_unset'}
    now = now or timezone.now()
    limit = max_per_tick()
    created = 0
    for session in list(due_sessions(now)[:limit]):
        if _calendar_change_pending(session):
            continue
        if start_feedback(session, now) is not None:
            created += 1
    pending = list(
        MentorSessionFeedback.objects
        .filter(created_at__gte=now - max_age())
        .filter(Q(email_status=MentorSessionFeedback.DELIVERY_PENDING)
                | Q(slack_status=MentorSessionFeedback.DELIVERY_PENDING))
        .order_by('created_at', 'pk')
        .values_list('pk', flat=True)[:limit]
    )
    delivered = 0
    for pk in pending:
        try:
            deliver(pk, now=now)
            delivered += 1
        except Exception:
            logger.exception('mentor feedback delivery raised for %s', pk)
    return {'status': 'completed', 'created': created, 'delivered': delivered}


def deliver(feedback_id, now=None):
    """Send whichever of email and Slack is still pending for one row."""
    now = now or timezone.now()
    row = (MentorSessionFeedback.objects
           .select_related('session__mentor__user', 'respondent').get(pk=feedback_id))
    wanted = channels()
    if row.email_status == MentorSessionFeedback.DELIVERY_PENDING:
        if 'email' not in wanted:
            _set(row, email_status=MentorSessionFeedback.DELIVERY_SKIPPED, email_error='channel_off')
        else:
            send_email(row, now=now)
    if row.slack_status == MentorSessionFeedback.DELIVERY_PENDING:
        if 'slack' not in wanted:
            _set(row, slack_status=MentorSessionFeedback.DELIVERY_SKIPPED)
        else:
            send_slack(row, now=now)
    return row


def _set(row, **fields):
    for key, value in fields.items():
        setattr(row, key, value)
    fields['updated_at'] = timezone.now()
    MentorSessionFeedback.objects.filter(pk=row.pk).update(**fields)


def _mark_sent(row, now):
    if row.sent_at is None:
        MentorSessionFeedback.objects.filter(pk=row.pk, sent_at__isnull=True).update(sent_at=now)
        row.sent_at = now


# --------------------------------------------------------------------- email

def email_context(row):
    info = describe(row)
    token = info['token']
    return {
        **info,
        'url_sad': page_url(token, MentorSessionFeedback.RATING_SAD, MentorSessionFeedback.VIA_EMAIL),
        'url_neutral': page_url(token, MentorSessionFeedback.RATING_NEUTRAL, MentorSessionFeedback.VIA_EMAIL),
        'url_happy': page_url(token, MentorSessionFeedback.RATING_HAPPY, MentorSessionFeedback.VIA_EMAIL),
        'url_no_show': page_url(token, MentorSessionFeedback.RATING_NO_SHOW, MentorSessionFeedback.VIA_EMAIL),
        'unsubscribe_url': unsubscribe_url(token),
        'time_range_detail': f"{info['time_range']}, Melbourne time",
        'asset_base': asset_base(),
    }


def send_email(row, now=None):
    from system_email import api as system_email
    from system_email.render import SystemEmailContextError
    from system_email.transport_resend import SystemEmailError

    now = now or timezone.now()
    user = row.respondent
    address = str(getattr(user, 'email', '') or '').strip()
    if not address or '@' not in address or is_placeholder_email(address):
        _set(row, email_status=MentorSessionFeedback.DELIVERY_SKIPPED, email_error='no_address')
        return row.email_status
    if MentorFeedbackOptOut.objects.filter(user_id=user.pk).exists():
        _set(row, email_status=MentorSessionFeedback.DELIVERY_OPTED_OUT)
        return row.email_status
    context = email_context(row)
    try:
        result = system_email.send(
            MESSAGE_KEY, to=address, idempotency_key=f'mentor-feedback/{row.pk}/email',
            context=context, user_id=user.pk, unsubscribe_url=context['unsubscribe_url'],
        )
    except SystemEmailError as exc:
        if getattr(exc, 'retryable', False):
            # Left pending: the next tick retries with the same idempotency key.
            _set(row, email_error=str(exc.code)[:64])
            return row.email_status
        status = (MentorSessionFeedback.DELIVERY_SUPPRESSED if exc.code == 'recipient_suppressed'
                  else MentorSessionFeedback.DELIVERY_FAILED)
        _set(row, email_status=status, email_error=str(exc.code)[:64])
        return row.email_status
    except SystemEmailContextError:
        _set(row, email_status=MentorSessionFeedback.DELIVERY_FAILED, email_error='render_failed')
        return row.email_status
    try:
        log_id = uuid.UUID(str(result.log_id))
    except (TypeError, ValueError):
        log_id = None
    _set(row, email_status=MentorSessionFeedback.DELIVERY_SENT, email_log_id=log_id, email_error='')
    _mark_sent(row, now)
    return row.email_status


# --------------------------------------------------------------------- Slack

def _slack():
    from mlai.staging_integrations import require_staging_capability
    require_staging_capability('slack')
    from integrations.services.slack import SlackService
    return SlackService


def _context_block(text):
    return {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': text}]}


def _escape(text):
    return str(text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def ask_blocks(row):
    info = describe(row)
    token = info['token']
    detail = ' · '.join(part for part in (
        f"{info['date_short']}, {info['time_range']}", info['place'], _escape(info['brief_line'])) if part)
    hello = f"Hi {_escape(info['founder_first'])} \U0001F44B " if info['founder_first'] else ''
    buttons = [{
        'type': 'button',
        'action_id': f'mentor_feedback_link_{rating}',
        'text': {'type': 'plain_text', 'text': f'{RATINGS[rating][1]} {RATINGS[rating][0]}', 'emoji': True},
        'url': page_url(token, rating, MentorSessionFeedback.VIA_SLACK),
        'value': rating,
    } for rating in FACES]
    no_show = page_url(token, MentorSessionFeedback.RATING_NO_SHOW, MentorSessionFeedback.VIA_SLACK)
    text = f"How was your session with {info['mentor_name']}? Tap a face to tell us."
    blocks = [
        {'type': 'section', 'text': {'type': 'mrkdwn',
            'text': f"{hello}How was your session with *{_escape(info['mentor_name'])}*?"}},
        _context_block(detail),
        {'type': 'actions', 'block_id': 'mentor_feedback_faces', 'elements': buttons},
        _context_block(f'Only the MLAI team sees your answer · <{no_show}|Session didn’t happen?>'),
    ]
    return text, blocks


def thanks_blocks(row):
    info = describe(row)
    token = info['token']
    label, emoji = RATINGS.get(row.rating, ('', ''))
    who = f", {_escape(info['founder_first'])}" if info['founder_first'] else ''
    if row.rating == MentorSessionFeedback.RATING_NO_SHOW:
        line = (f"Thanks{who}. We’ve told the MLAI team your session with "
                f"*{_escape(info['mentor_name'])}* didn’t happen.")
    else:
        line = f"Thanks{who}! You rated your session with *{_escape(info['mentor_name'])}*: {emoji} *{label}*"
    links = f"<{page_url(token, via=MentorSessionFeedback.VIA_SLACK)}|{'Edit your comment' if row.comment else 'Add a comment'}>"
    text = f"Thanks! You rated your session with {info['mentor_name']}: {label}"
    blocks = [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': line}},
        _context_block(f"{info['date_short']}, {info['time_range']} · {links} · "
                       f"<{page_url(token, via=MentorSessionFeedback.VIA_SLACK)}|Change your answer>"),
    ]
    return text, blocks


def send_slack(row, now=None):
    now = now or timezone.now()
    slack_id = str(getattr(row.respondent, 'slack_id', '') or '').strip()
    if not slack_id:
        _set(row, slack_status=MentorSessionFeedback.DELIVERY_SKIPPED)
        return row.slack_status
    # Claim first, so a crash after posting can never post a second DM.
    claimed = MentorSessionFeedback.objects.filter(
        pk=row.pk, slack_status=MentorSessionFeedback.DELIVERY_PENDING,
    ).update(slack_status=MentorSessionFeedback.DELIVERY_SENDING, updated_at=timezone.now())
    if not claimed:
        return row.slack_status
    row.slack_status = MentorSessionFeedback.DELIVERY_SENDING
    outcome, channel, ts = MentorSessionFeedback.DELIVERY_FAILED, '', ''
    try:
        slack = _slack()
        channel = slack.open_dm_channel(slack_id) or ''
        if channel:
            if row.rating:
                text, blocks = thanks_blocks(row)
            else:
                text, blocks = ask_blocks(row)
            ok, ts = slack.send_message(channel, text, blocks=blocks)
            outcome = MentorSessionFeedback.DELIVERY_SENT if ok else MentorSessionFeedback.DELIVERY_FAILED
    except Exception:
        logger.exception('mentor feedback Slack DM failed for %s', row.pk)
    _set(row, slack_status=outcome, slack_channel=channel[:32] if outcome == 'sent' else '',
         slack_ts=str(ts or '')[:32] if outcome == 'sent' else '')
    if outcome == MentorSessionFeedback.DELIVERY_SENT:
        _mark_sent(row, now)
    return outcome


def refresh_slack(feedback_id):
    """Edit the founder's DM to the thank-you version. Best effort."""
    try:
        row = (MentorSessionFeedback.objects
               .select_related('session__mentor__user', 'respondent').get(pk=feedback_id))
        if not (row.rating and row.slack_channel and row.slack_ts):
            return False
        text, blocks = thanks_blocks(row)
        return bool(_slack().update_message(row.slack_channel, row.slack_ts, text, blocks))
    except Exception:
        logger.exception('mentor feedback Slack edit failed for %s', feedback_id)
        return False


# --------------------------------------------------------------- staff alert

def _alert_text(row, *, change=False):
    session = row.session
    founder = _full_name(row.respondent) if row.respondent else 'A founder'
    mentor = _full_name(session.mentor.user)
    when = f'{short_date(session)}, {time_range(session)}'
    label, emoji = RATINGS.get(row.rating, ('', ''))
    admin = chat_url(f'/mentoring/admin/sessions?session={session.pk}')
    if row.rating == MentorSessionFeedback.RATING_NO_SHOW:
        head = f"{emoji} Mentoring feedback: {_escape(founder)} says their session with {_escape(mentor)} ({when}) *didn’t happen*."
    else:
        head = f"{emoji} Mentoring feedback: {_escape(founder)} rated their session with {_escape(mentor)} ({when}) *{label}*."
    if change:
        head = f"Changed answer: *{label}*."
    comment = _escape(row.comment[:300]) if row.comment else 'No comment yet'
    return f'{head} “{comment}” · <{admin}|Open in admin>'


def alert(feedback_id, *, previous_rating='', comment_added=False):
    """Tell staff about a negative answer once; later changes reply in its thread."""
    channel = alert_channel()
    if not channel:
        return False
    try:
        row = (MentorSessionFeedback.objects
               .select_related('session__mentor__user', 'respondent').get(pk=feedback_id))
        if row.suspect_scanner:
            return False
        slack = _slack()
        if row.alert_ts:
            if comment_added and row.comment:
                text = f'Comment: “{_escape(row.comment[:1500])}”'
            elif previous_rating and previous_rating != row.rating:
                text = _alert_text(row, change=True)
            else:
                return False
            ok, _ts = slack.send_message(channel, text, thread_ts=row.alert_ts)
            return bool(ok)
        if row.rating not in MentorSessionFeedback.NEGATIVE_RATINGS:
            return False
        ok, ts = slack.send_message(channel, _alert_text(row))
        if ok and ts:
            MentorSessionFeedback.objects.filter(pk=row.pk, alert_ts='').update(alert_ts=str(ts)[:32])
        return bool(ok)
    except Exception:
        logger.exception('mentor feedback alert failed for %s', feedback_id)
        return False


# ------------------------------------------------------------------ answers

def load(token, *, now=None):
    """The row for a token, or FeedbackError. Expired links still read, never write."""
    fid = parse_token(token)
    if fid is None:
        raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
    row = (MentorSessionFeedback.objects
           .select_related('session__mentor__user', 'respondent').filter(pk=fid).first())
    if row is None:
        raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
    return row


def is_expired(row, now=None):
    return (now or timezone.now()) >= row.token_expires_at


def _looks_like_scanner(row, now):
    """Three different faces within seconds, right after sending: a link scanner."""
    if row.sent_at is None or now - row.sent_at > SCANNER_AFTER_SEND + SCANNER_WINDOW:
        return False
    recent = []
    for entry in row.rating_history:
        at = parse_datetime(str(entry.get('at') or ''))
        if at is not None and now - at <= SCANNER_WINDOW:
            recent.append(entry.get('rating'))
    return len(set(recent) & set(FACES)) >= 3


def _apply_rating(row, rating, via, now):
    previous = row.rating
    row.rating_history = (list(row.rating_history or []) + [
        {'rating': rating, 'at': now.isoformat(), 'via': via},
    ])[-HISTORY_LIMIT:]
    row.rating = rating
    row.rated_at = now
    row.rated_via = via
    if not row.suspect_scanner and _looks_like_scanner(row, now):
        row.suspect_scanner = True
    row.save(update_fields=['rating', 'rated_at', 'rated_via', 'rating_history', 'suspect_scanner', 'updated_at'])
    pk = row.pk
    transaction.on_commit(lambda: _after_rating(pk, previous))
    return row


def _after_rating(pk, previous):
    refresh_slack(pk)
    alert(pk, previous_rating=previous)


def record_rating(token, rating, via=MentorSessionFeedback.VIA_PAGE, now=None):
    now = now or timezone.now()
    if rating not in RATINGS:
        raise FeedbackError('invalid_rating', 'Choose one of the faces.', 400)
    via = via if via in VIA else MentorSessionFeedback.VIA_PAGE
    fid = parse_token(token)
    if fid is None:
        raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
    with transaction.atomic():
        row = (MentorSessionFeedback.objects.select_for_update()
               .select_related('session__mentor__user', 'respondent').filter(pk=fid).first())
        if row is None:
            raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
        if is_expired(row, now):
            raise FeedbackError('expired', 'This feedback link has expired.', 410)
        if row.rating == rating and row.rated_at and now - row.rated_at < SCANNER_WINDOW:
            return row  # A double tap or a reload: nothing new.
        return _apply_rating(row, rating, via, now)


def clean_comment(value):
    if not isinstance(value, str):
        raise FeedbackError('invalid_comment', 'Comment must be text.', 400)
    lines = [' '.join(line.split()) for line in value.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    text = '\n'.join(lines).strip()
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    if len(text) > COMMENT_MAX_LENGTH:
        raise FeedbackError('comment_too_long', f'Keep it under {COMMENT_MAX_LENGTH} characters.', 400)
    return text


def _apply_comment(row, text, now):
    changed = text != row.comment
    row.comment = text
    row.commented_at = now if text else None
    row.save(update_fields=['comment', 'commented_at', 'updated_at'])
    if changed and text:
        pk = row.pk
        transaction.on_commit(lambda: (refresh_slack(pk), alert(pk, comment_added=True)))
    return row


def record_comment(token, value, now=None):
    now = now or timezone.now()
    text = clean_comment(value)
    fid = parse_token(token)
    if fid is None:
        raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
    with transaction.atomic():
        row = (MentorSessionFeedback.objects.select_for_update()
               .select_related('session__mentor__user', 'respondent').filter(pk=fid).first())
        if row is None:
            raise FeedbackError('invalid_token', 'This feedback link is not valid.', 404)
        if is_expired(row, now):
            raise FeedbackError('expired', 'This feedback link has expired.', 410)
        return _apply_comment(row, text, now)


def opt_out(token):
    row = load(token)
    MentorFeedbackOptOut.objects.get_or_create(user_id=row.respondent_id)
    return row


def rate_in_app(session_id, user, rating, comment=None, now=None):
    """The founder rates from MLAI Chat. Creates the row if the runner hasn't yet."""
    now = now or timezone.now()
    if rating not in RATINGS:
        raise FeedbackError('invalid_rating', 'Choose one of the faces.', 400)
    text = clean_comment(comment) if comment is not None else None
    with transaction.atomic():
        session = (MentorSession.objects.select_related('mentor__user', 'founder')
                   .filter(pk=session_id, founder=user).first())
        if session is None:
            raise FeedbackError('not_found', 'Session not found.', 404)
        if session.status not in (MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED) \
                or now < session.ends_at:
            raise FeedbackError('not_ended', 'You can rate a session once it has ended.', 409)
        row, created = MentorSessionFeedback.objects.select_for_update().get_or_create(
            session=session, respondent=user,
            defaults={
                'role': MentorSessionFeedback.ROLE_FOUNDER,
                'token_expires_at': now + token_ttl(),
                # Answered in the app before we asked: don't ask by email or Slack.
                'email_status': MentorSessionFeedback.DELIVERY_SKIPPED,
                'slack_status': MentorSessionFeedback.DELIVERY_SKIPPED,
            },
        )
        if row.rating != rating:
            _apply_rating(row, rating, MentorSessionFeedback.VIA_APP, now)
        if text is not None:
            _apply_comment(row, text, now)
        return row


# ------------------------------------------------------------ serialization

def serialize_mine(row):
    if row is None or not row.rating:
        return None
    return {
        'rating': row.rating,
        'rated_at': row.rated_at.isoformat() if row.rated_at else None,
        'comment': row.comment,
    }


def serialize_admin(row):
    if row is None:
        return None
    return {
        'rating': row.rating or None,
        'rated_at': row.rated_at.isoformat() if row.rated_at else None,
        'rated_via': row.rated_via or None,
        'comment': row.comment,
        'suspect_scanner': row.suspect_scanner,
        'email_status': row.email_status,
        'slack_status': row.slack_status,
    }
