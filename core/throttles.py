"""Rate throttles for unauthenticated auth entry points.

These endpoints (check-user, send-magic-link, create-user, verify-magic-link)
are AllowAny and take an attacker-supplied email. Without throttling they permit
unbounded credential-enumeration and magic-link email spam. Rates are configured
in settings.DEFAULT_THROTTLE_RATES and overridable via env.

Note on client identity behind a proxy: AnonRateThrottle keys on the client IP
derived from REMOTE_ADDR / X-Forwarded-For per settings.NUM_PROXIES. NUM_PROXIES
is unset, so DRF keys on the whole X-Forwarded-For value. That is one entry in
production, because nginx overwrites the header with $remote_addr, but it is
not the same rule as core.client_ip.trusted_client_ip (TRUSTED_PROXY_HOPS),
which the hand-rolled auth limits in core.auth_throttles use. Keep the two in
mind together if the proxy chain changes. The throttle is still a meaningful
backstop regardless.
"""

from rest_framework.throttling import AnonRateThrottle


class AuthEndpointRateThrottle(AnonRateThrottle):
    """General throttle for auth lookups (check-user, create-user, verify)."""

    scope = "auth_endpoint"


class MagicLinkSendRateThrottle(AnonRateThrottle):
    """Tighter throttle for endpoints that trigger an outbound email."""

    scope = "auth_magic_link"
