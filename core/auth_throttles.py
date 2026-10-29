import hashlib
import time

from django.core.cache import cache
from rest_framework.exceptions import Throttled

from .client_ip import trusted_client_ip


def client_ip(request):
    """Return the trusted client IP (see :func:`core.client_ip.trusted_client_ip`)."""
    return trusted_client_ip(request)


def _enforce_limit(*, action, dimension, value, limit, window_seconds):
    bucket = int(time.time()) // window_seconds
    opaque = hashlib.sha256(f'{action}:{dimension}:{value}'.encode('utf-8')).hexdigest()
    key = f'auth-rate:{opaque}:{bucket}'
    if cache.add(key, 1, timeout=window_seconds + 5):
        return
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_seconds + 5)
        count = 1
    if count > limit:
        wait = window_seconds - (int(time.time()) % window_seconds)
        raise Throttled(wait=max(wait, 1), detail='Too many authentication requests.')


def enforce_password_reset_request_limits(request, email):
    _enforce_limit(
        action='password-reset-request',
        dimension='email',
        value=email,
        limit=5,
        window_seconds=3600,
    )
    _enforce_limit(
        action='password-reset-request',
        dimension='ip',
        value=client_ip(request),
        limit=20,
        window_seconds=3600,
    )


def enforce_password_reset_confirm_limits(request, token_selector):
    _enforce_limit(
        action='password-reset-confirm',
        dimension='selector',
        value=token_selector,
        limit=10,
        window_seconds=3600,
    )
    _enforce_limit(
        action='password-reset-confirm',
        dimension='ip',
        value=client_ip(request),
        limit=30,
        window_seconds=3600,
    )


def enforce_password_change_limits(request):
    _enforce_limit(
        action='password-change',
        dimension='user',
        value=request.user.pk,
        limit=10,
        window_seconds=3600,
    )
    _enforce_limit(
        action='password-change',
        dimension='ip',
        value=client_ip(request),
        limit=20,
        window_seconds=3600,
    )


def enforce_chat_password_login_limits(request, email, public_key):
    for dimension, value, limit in (
        ('email', email, 10),
        ('public-key', public_key, 20),
        ('ip', client_ip(request), 50),
    ):
        _enforce_limit(
            action='community-chat-password-login',
            dimension=dimension,
            value=value,
            limit=limit,
            window_seconds=600,
        )


def enforce_chat_email_code_request_limits(request, email, installation_id):
    _enforce_limit(
        action='community-chat-email-code-cooldown',
        dimension='email-installation',
        value=f'{email}:{installation_id}',
        limit=1,
        window_seconds=60,
    )
    for dimension, value, limit in (
        ('email', email, 5),
        ('installation', installation_id, 10),
        ('ip', client_ip(request), 20),
    ):
        _enforce_limit(
            action='community-chat-email-code-request',
            dimension=dimension,
            value=value,
            limit=limit,
            window_seconds=3600,
        )


def enforce_chat_email_code_verify_limits(request, challenge_id, installation_id):
    for dimension, value, limit in (
        ('challenge', challenge_id, 10),
        ('installation', installation_id, 30),
        ('ip', client_ip(request), 30),
    ):
        _enforce_limit(
            action='community-chat-email-code-verify',
            dimension=dimension,
            value=value,
            limit=limit,
            window_seconds=600,
        )
