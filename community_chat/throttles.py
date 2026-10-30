import hashlib

from rest_framework.throttling import ScopedRateThrottle

from core.auth_throttles import RateLimit, configured_limit, enforce_rate_limits
from core.client_ip import trusted_client_ip


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


COMMUNITY_CHAT_RATE_NAMESPACE = "community-chat-rate-v2"
COMMUNITY_CHAT_THROTTLE_DETAIL = "Too many community chat requests."

# Device enrolment (challenge, invite, confirm and so on) for a signed-in
# account is already bounded per account and per device key. Its per-IP
# backstop must not refuse a room of founders on one venue network, so an
# authenticated caller's IP ceiling is raised to at least this many per window.
DEFAULT_AUTHENTICATED_BOOTSTRAP_IP_FLOOR = 300

# The desktop sign-in handoff (``auth/device/start`` then
# ``auth/device/exchange``) is anonymous by design, so the authenticated floor
# above never reaches it. Each request is still bounded per device key, and an
# exchange also needs a code the signed-in browser approved, so these actions
# get a room-sized per-IP floor of their own.
DEFAULT_DEVICE_AUTH_IP_FLOOR = 300
DEVICE_AUTH_ACTIONS = frozenset({"auth-start", "auth-exchange"})


def client_ip(request):
    """Return the trusted client IP (see :func:`core.client_ip.trusted_client_ip`)."""
    return trusted_client_ip(request)


def enforce_dimension_limit(*, action, dimension, value, limit, window_seconds):
    """Admit one request against a single rolling-window limit or raise ``Throttled``."""
    enforce_rate_limits(
        [RateLimit(action, dimension, value, limit, window_seconds)],
        namespace=COMMUNITY_CHAT_RATE_NAMESPACE,
        detail=COMMUNITY_CHAT_THROTTLE_DETAIL,
    )


def enforce_bootstrap_limits(request, *, action, public_key, user_limit, key_limit, ip_limit):
    """Bound device bootstrap work per account, per device key and per client IP.

    A signed-in caller is already bounded by ``user_limit``, so its IP ceiling
    is raised to ``AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M`` when that
    is higher. The anonymous desktop sign-in handoff (``DEVICE_AUTH_ACTIONS``)
    is raised to ``AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M`` the same way. Other
    anonymous callers keep ``ip_limit``.

    Every dimension is checked in one call, so a refusal on any of them leaves
    the others uncounted and a retry never spends the founder's own budget.
    """
    window = 600
    user = getattr(request, "user", None)
    user_id = getattr(user, "pk", None)
    authenticated = user_id is not None and bool(getattr(user, "is_authenticated", False))
    limits = []
    if authenticated:
        ip_limit = max(
            ip_limit,
            configured_limit(
                "AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M",
                DEFAULT_AUTHENTICATED_BOOTSTRAP_IP_FLOOR,
            ),
        )
        limits.append(RateLimit(action, "user", user_id, user_limit, window))
    if action in DEVICE_AUTH_ACTIONS:
        ip_limit = max(
            ip_limit,
            configured_limit("AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M", DEFAULT_DEVICE_AUTH_IP_FLOOR),
        )
    limits.append(RateLimit(action, "public-key", public_key, key_limit, window))
    limits.append(RateLimit(action, "ip", client_ip(request), ip_limit, window))
    enforce_rate_limits(
        limits,
        namespace=COMMUNITY_CHAT_RATE_NAMESPACE,
        detail=COMMUNITY_CHAT_THROTTLE_DETAIL,
    )
