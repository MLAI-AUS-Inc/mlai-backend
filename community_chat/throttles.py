import hashlib
import time

from django.core.cache import cache
from rest_framework.exceptions import Throttled
from rest_framework.throttling import ScopedRateThrottle


class CommunityChatScopedThrottle(ScopedRateThrottle):
    scope_attr = "community_chat_throttle_scope"


class StartupScopedThrottle(ScopedRateThrottle):
    """Bound startup work per account without consuming Chat's home budget."""

    scope_attr = "startup_throttle_scope"


class StartupThrottleMixin:
    """Keep page loading, other reads, polling and mutations in bounded buckets.

    ScopedRateThrottle keys authenticated requests by account, so selecting
    another company or device cannot replenish a bucket. Domain throttles are
    additive: an expensive operation keeps its original restrictions.
    """

    startup_read_bucket = "read"

    @property
    def startup_throttle_scope(self):
        """Choose a trusted operation bucket; client parameters cannot alter it."""
        bucket = self.startup_read_bucket if self.request.method in ("GET", "HEAD", "OPTIONS") else "write"
        return f"my_startup_{bucket}"

    def get_throttles(self):
        """Add the account budget while retaining the owning API's throttles."""
        return [*super().get_throttles(), StartupScopedThrottle()]


class SlackSnapshotDeviceThrottle(CommunityChatScopedThrottle):
    """Bound snapshot polling per authenticated device, independently of writes."""

    def get_cache_key(self, request, view):
        key = super().get_cache_key(request, view)
        device = getattr(request, "community_chat_public_key", None)
        if key and device:
            # Authentication supplies this binding; query/body values never do.
            return f"{key}:device:{hashlib.sha256(str(device).encode()).hexdigest()}"
        return key


class SlackSnapshotAccountThrottle(CommunityChatScopedThrottle):
    """Cap aggregate polling even when an account has many verified devices."""

    scope_attr = "slack_snapshot_account_throttle_scope"


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
