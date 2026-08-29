import hashlib
import time

from django.core.cache import cache
from rest_framework.exceptions import Throttled
from rest_framework.throttling import ScopedRateThrottle


class CommunityChatScopedThrottle(ScopedRateThrottle):
    scope_attr = "community_chat_throttle_scope"


def client_ip(request):
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",", 1)[0].strip()
    return forwarded or str(request.META.get("REMOTE_ADDR") or "unknown")


def enforce_dimension_limit(*, action, dimension, value, limit, window_seconds):
    bucket = int(time.time()) // window_seconds
    opaque = hashlib.sha256(f"{action}:{dimension}:{value}".encode("utf-8")).hexdigest()
    key = f"community-chat-rate:{opaque}:{bucket}"
    if cache.add(key, 1, timeout=window_seconds + 5):
        return
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_seconds + 5)
        count = 1
    if count > limit:
        wait = window_seconds - (int(time.time()) % window_seconds)
        raise Throttled(wait=max(wait, 1), detail="Too many community chat requests.")


def enforce_bootstrap_limits(request, *, action, public_key, user_limit, key_limit, ip_limit):
    window = 600
    user = getattr(request, "user", None)
    user_id = getattr(user, "pk", None)
    if user_id is not None and bool(getattr(user, "is_authenticated", False)):
        enforce_dimension_limit(
            action=action,
            dimension="user",
            value=user_id,
            limit=user_limit,
            window_seconds=window,
        )
    enforce_dimension_limit(
        action=action,
        dimension="public-key",
        value=public_key,
        limit=key_limit,
        window_seconds=window,
    )
    enforce_dimension_limit(
        action=action,
        dimension="ip",
        value=client_ip(request),
        limit=ip_limit,
        window_seconds=window,
    )
