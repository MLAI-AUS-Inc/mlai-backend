"""The mentor invite email: its context, its preview and the send.

Sent through the system_email app (Resend on the transactional domain), never
through the CRM or listmonk: it is one personal message from a staff member, and
its button carries a single-use token that must not be rewritten for click
tracking. Nothing here logs or stores the token, the link or the note.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings

from .models import invite_link

MESSAGE_KEY = 'mentor_invite'
NOTE_MAX_LENGTH = 600
PREVIEW_TOKEN = 'preview'
MELBOURNE = ZoneInfo('Australia/Melbourne')
_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December')


def enabled():
    return bool(getattr(settings, 'MENTOR_INVITE_EMAIL_ENABLED', False))


def configured():
    """Whether system email can send at all (key and sender set)."""
    from system_email import transport_resend
    return bool(transport_resend.api_key() and transport_resend.from_address())


def available():
    return enabled() and configured()


def reserve_rate_limit(address, inviter, *, now):
    """Throttle invitations independently of account-access email."""
    if not getattr(settings, 'MENTOR_INVITE_RATE_LIMIT_ENABLED', False):
        return
    from django.core.cache import cache
    from django.utils.crypto import salted_hmac
    from .service import MentoringError

    recipient = salted_hmac('mentor-invite-recipient', address.strip().lower(), algorithm='sha256').hexdigest()
    recipient_key = f'mentoring:email-invite:recipient:{recipient}'
    hour = int(now.timestamp()) // 3600
    sender_key = f'mentoring:email-invite:inviter:{inviter.pk}:{hour}'
    try:
        cache.add(sender_key, 0, timeout=7200)
        if cache.incr(sender_key) > 10:
            raise MentoringError('invite_email_rate_limited', 'Wait until the next hour before sending more invitations.', 429)
        if not cache.add(recipient_key, 1, timeout=86400):
            raise MentoringError('invite_email_rate_limited', 'Wait 24 hours before emailing this person another invitation.', 429)
    except MentoringError:
        raise
    except Exception:
        raise MentoringError('invite_email_rate_unavailable', 'The invitation email check is unavailable. Try again shortly.', 503) from None


def explainer_url():
    return str(getattr(settings, 'MENTOR_INVITE_EXPLAINER_URL', '') or 'https://mlai.au/mentors').strip()


def asset_base():
    return str(getattr(settings, 'CRM_EMAIL_ASSET_BASE_URL', '') or '').rstrip('/')


def clean_note(value):
    """Plain text, at most NOTE_MAX_LENGTH characters, line breaks kept. None for a bad value."""
    if value is None:
        return ''
    if not isinstance(value, str):
        return None
    lines = [' '.join(line.split()) for line in value.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    text = '\n'.join(lines).strip()
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    return text if len(text) <= NOTE_MAX_LENGTH else None


def _date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    local = value.astimezone(MELBOURNE)
    return f'{local.day} {_MONTHS[local.month - 1]} {local.year}'


def _first_name(user):
    name = (getattr(user, 'first_name', '') or '').strip() if user is not None else ''
    if name:
        return name.split()[0]
    full = (getattr(user, 'full_name', '') or '').strip() if user is not None else ''
    return full.split()[0] if full else ''


def _name(user):
    if user is None:
        return 'MLAI'
    return (getattr(user, 'full_name', '') or '').strip() or 'MLAI'


def context(*, invited_user, invited_email, inviter, token, expires_at, note=''):
    """Template context for one invite. ``invited_user`` may be None."""
    return {
        'first_name': _first_name(invited_user),
        'inviter_name': _name(inviter),
        'inviter_first_name': _first_name(inviter) or 'MLAI',
        'note': note or '',
        'accept_url': invite_link(token),
        'explainer_url': explainer_url(),
        'expires_on': _date(expires_at),
        'invited_email': invited_email,
        'asset_base': asset_base(),
    }


def reply_to(inviter):
    """Replies go to the staff member who sent the invite (owner decision, 16 Nov 2026)."""
    return str(getattr(inviter, 'email', '') or '').strip()


def preview(*, invited_user, invited_email, inviter, expires_at, note=''):
    """The rendered email with a dummy token. No invite or token is created."""
    from system_email import transport_resend
    from system_email.registry import get_spec
    from system_email.render import render

    rendered = render(get_spec(MESSAGE_KEY), context(
        invited_user=invited_user, invited_email=invited_email, inviter=inviter,
        token=PREVIEW_TOKEN, expires_at=expires_at, note=note,
    ))
    return {
        'subject': rendered.subject,
        'from': transport_resend.from_address(),
        'reply_to': reply_to(inviter) or str(getattr(settings, 'SYSTEM_EMAIL_REPLY_TO', '') or ''),
        'to': invited_email,
        'html': rendered.html,
        'text': rendered.text,
    }


def send(invite, *, token, inviter, note=''):
    """Send the email for ``invite`` and return ``{status, to, code?}``.

    Never raises for a provider problem: the invite stays valid, and the caller
    falls back to showing the link. The idempotency key is per invite and per
    send, so a retried request cannot send the same email twice.
    """
    from system_email import api as system_email
    from system_email.render import SystemEmailContextError
    from system_email.transport_resend import SystemEmailError

    address = invite.email or getattr(invite.user, 'email', '')
    try:
        result = system_email.send(
            MESSAGE_KEY,
            to=address,
            idempotency_key=f'mentor-invite/{invite.pk}/{invite.send_count + 1}',
            context=context(invited_user=invite.user, invited_email=address, inviter=inviter,
                            token=token, expires_at=invite.expires_at, note=note),
            user_id=invite.user_id,
            reply_to=reply_to(inviter),
        )
    except SystemEmailError as exc:
        status = 'suppressed' if exc.code == 'recipient_suppressed' else 'failed'
        return {'status': status, 'to': address, 'code': exc.code}
    except SystemEmailContextError:
        return {'status': 'failed', 'to': address, 'code': 'render_failed'}
    return {'status': 'sent', 'to': address, 'log_id': result.log_id}
