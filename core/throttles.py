"""Rate throttles for unauthenticated auth entry points.

These endpoints (check-user, send-magic-link, create-user, verify-magic-link)
are AllowAny and take an attacker-supplied email. Without throttling they permit
unbounded credential-enumeration and magic-link email spam. Rates are configured
in settings.DEFAULT_THROTTLE_RATES and overridable via env.

Note on client identity behind a proxy: AnonRateThrottle keys on the client IP
derived from REMOTE_ADDR / X-Forwarded-For per settings.NUM_PROXIES. In
production this app sits behind Cloudflare + gunicorn, so NUM_PROXIES must be set
correctly for per-client throttling to be accurate; otherwise all traffic can
share the proxy IP. The throttle is still a meaningful backstop regardless.
"""

from rest_framework.throttling import AnonRateThrottle


class AuthEndpointRateThrottle(AnonRateThrottle):
    """General throttle for auth lookups (check-user, create-user, verify)."""

    scope = "auth_endpoint"


class MagicLinkSendRateThrottle(AnonRateThrottle):
    """Tighter throttle for endpoints that trigger an outbound email."""

    scope = "auth_magic_link"
