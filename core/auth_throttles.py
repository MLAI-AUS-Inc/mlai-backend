import hashlib
import time

from django.core.cache import cache
from rest_framework.exceptions import Throttled


def client_ip(request):
    forwarded = str(request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',', 1)[0].strip()
    return forwarded or str(request.META.get('REMOTE_ADDR') or 'unknown')


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
