"""Rolling-window limits for authentication entry points.

Limits are keyed on opaque digests of a dimension value (email, installation,
challenge, client IP and so on) and counted in the shared Django cache.

Two algorithms are used:

* ``limit == 1`` is a cooldown: one admitted request starts a timer and the
  next one is admitted exactly ``window_seconds`` later.
* larger limits use a sliding-window counter: two adjacent fixed buckets, with
  the previous bucket weighted by the part of it that still overlaps the
  rolling window. Unlike the old fixed clock buckets this has no burst at a
  bucket boundary, and ``Retry-After`` is the time until the weighted count
  drops far enough to admit one more request.

Rejected requests are not counted, so tapping again while throttled never
extends the wait. A call that checks several dimensions either counts all of
them or none of them.
"""

import hashlib
import math
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import Throttled

from .client_ip import trusted_client_ip


AUTH_RATE_NAMESPACE = 'auth-rate-v2'
AUTH_THROTTLE_DETAIL = 'Too many authentication requests.'

# Per-IP ceilings sit well above the size of a workshop room on one venue
# network. The per-email, per-installation and per-challenge limits are what
# bound abuse of any single account or device.
DEFAULT_IP_CODE_REQUESTS_PER_HOUR = 600
DEFAULT_IP_VERIFY_PER_10M = 300
DEFAULT_IP_PASSWORD_PER_10M = 200

# Floating-point slack so a client that waits exactly Retry-After is admitted.
_WEIGHT_EPSILON = 1e-6


@dataclass(frozen=True)
class RateLimit:
    """One dimension of a rate limit: at most ``limit`` per ``window_seconds``.

    A ``limit`` of 0 or less disables the dimension.
    """

    action: str
    dimension: str
    value: object
    limit: int
    window_seconds: int


def client_ip(request):
    """Return the trusted client IP (see :func:`core.client_ip.trusted_client_ip`)."""
    return trusted_client_ip(request)


def configured_limit(setting_name, default):
    """Read an integer limit from settings, falling back to ``default``."""
    try:
        return int(getattr(settings, setting_name, default))
    except (TypeError, ValueError):
        return default


def _opaque(limit):
    return hashlib.sha256(
        f'{limit.action}:{limit.dimension}:{limit.value}'.encode('utf-8')
    ).hexdigest()


def _cooldown_wait(stored_at, window_seconds, now):
    try:
        elapsed = now - float(stored_at)
    except (TypeError, ValueError):
        return 0.0
    return window_seconds - elapsed


def _sliding_retry_after(*, previous, admitted, limit, window, bucket, now):
    """Seconds until the weighted count admits one more request."""
    if admitted + 1 <= limit:
        if previous <= 0:
            return 1
        # Within this bucket: wait for the previous bucket's weight to fall.
        fraction = 1 - (limit - 1 - admitted) / previous
        ready_at = (bucket + fraction) * window
    else:
        # This bucket alone is full: wait into the next bucket, where it
        # becomes the previous bucket and decays in turn.
        fraction = 1 - (limit - 1) / admitted
        ready_at = (bucket + 1 + fraction) * window
    return max(1, math.ceil(ready_at - now))


class _Admission:
    """Counts taken by one call, so a rejection can undo all of them."""

    def __init__(self):
        self.counted = []
        self.cooldowns = []
        self.waits = []

    def undo(self):
        for key in self.counted:
            try:
                cache.decr(key)
            except ValueError:
                # The bucket already expired, so there is nothing to undo.
                continue
        for key in self.cooldowns:
            cache.delete(key)


def _admit_cooldown(admission, limit, *, namespace, now):
    key = f'{namespace}:cooldown:{_opaque(limit)}'
    window = int(limit.window_seconds)
    if cache.add(key, now, timeout=window + 1):
        admission.cooldowns.append(key)
        return
    wait = _cooldown_wait(cache.get(key), window, now)
    if wait <= 0:
        # The timer ran out but the cache has not evicted it yet.
        cache.set(key, now, timeout=window + 1)
        admission.cooldowns.append(key)
        return
    admission.waits.append(max(1, math.ceil(wait)))


def _admit_sliding(admission, limit, *, namespace, now):
    window = int(limit.window_seconds)
    bucket = int(now // window)
    elapsed = now - bucket * window
    opaque = _opaque(limit)
    current_key = f'{namespace}:{opaque}:{bucket}'
    previous_key = f'{namespace}:{opaque}:{bucket - 1}'
    timeout = 2 * window + 5
    cache.add(current_key, 0, timeout=timeout)
    try:
        current = cache.incr(current_key)
    except ValueError:
        cache.set(current_key, 1, timeout=timeout)
        current = 1
    admission.counted.append(current_key)
    previous = int(cache.get(previous_key) or 0)
    weighted = previous * (window - elapsed) / window + current
    if weighted <= limit.limit + _WEIGHT_EPSILON:
        return
    admission.waits.append(
        _sliding_retry_after(
            previous=previous,
            admitted=current - 1,
            limit=limit.limit,
            window=window,
            bucket=bucket,
            now=now,
        )
    )


def enforce_rate_limits(limits, *, namespace=AUTH_RATE_NAMESPACE, detail=AUTH_THROTTLE_DETAIL, now=None):
    """Admit one request against every limit, or raise ``Throttled``.

    Either every dimension counts the request or none does. ``Throttled.wait``
    (sent as ``Retry-After``) is the longest wait any rejecting dimension needs.
    """
    now = time.time() if now is None else float(now)
    admission = _Admission()
    for limit in limits:
        if int(limit.limit) <= 0:
            continue
        if int(limit.limit) == 1:
            _admit_cooldown(admission, limit, namespace=namespace, now=now)
        else:
            _admit_sliding(admission, limit, namespace=namespace, now=now)
    if admission.waits:
        admission.undo()
        raise Throttled(wait=max(admission.waits), detail=detail)


def _enforce_limit(*, action, dimension, value, limit, window_seconds):
    enforce_rate_limits(
        [RateLimit(action, dimension, value, limit, window_seconds)],
    )


def enforce_password_reset_request_limits(request, email):
    enforce_rate_limits([
        RateLimit('password-reset-request', 'email', email, 5, 3600),
        RateLimit('password-reset-request', 'ip', client_ip(request), 20, 3600),
    ])


def enforce_password_reset_confirm_limits(request, token_selector):
    enforce_rate_limits([
        RateLimit('password-reset-confirm', 'selector', token_selector, 10, 3600),
        RateLimit('password-reset-confirm', 'ip', client_ip(request), 30, 3600),
    ])


def enforce_password_change_limits(request):
    enforce_rate_limits([
        RateLimit('password-change', 'user', request.user.pk, 10, 3600),
        RateLimit('password-change', 'ip', client_ip(request), 20, 3600),
    ])


def enforce_chat_password_login_limits(request, email, public_key):
    ip_limit = configured_limit('AUTH_THROTTLE_IP_PASSWORD_PER_10M', DEFAULT_IP_PASSWORD_PER_10M)
    enforce_rate_limits([
        RateLimit('community-chat-password-login', 'email', email, 10, 600),
        RateLimit('community-chat-password-login', 'public-key', public_key, 20, 600),
        RateLimit('community-chat-password-login', 'ip', client_ip(request), ip_limit, 600),
    ])


def enforce_chat_email_code_request_limits(request, email, installation_id):
    ip_limit = configured_limit(
        'AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR',
        DEFAULT_IP_CODE_REQUESTS_PER_HOUR,
    )
    enforce_rate_limits([
        RateLimit(
            'community-chat-email-code-cooldown',
            'email-installation',
            f'{email}:{installation_id}',
            1,
            60,
        ),
        RateLimit('community-chat-email-code-request', 'email', email, 5, 3600),
        RateLimit('community-chat-email-code-request', 'installation', installation_id, 10, 3600),
        RateLimit('community-chat-email-code-request', 'ip', client_ip(request), ip_limit, 3600),
    ])


def enforce_chat_email_code_verify_limits(request, challenge_id, installation_id):
    ip_limit = configured_limit('AUTH_THROTTLE_IP_VERIFY_PER_10M', DEFAULT_IP_VERIFY_PER_10M)
    enforce_rate_limits([
        RateLimit('community-chat-email-code-verify', 'challenge', challenge_id, 10, 600),
        RateLimit('community-chat-email-code-verify', 'installation', installation_id, 30, 600),
        RateLimit('community-chat-email-code-verify', 'ip', client_ip(request), ip_limit, 600),
    ])
