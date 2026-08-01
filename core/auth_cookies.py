"""Single source of truth for the JWT auth cookies.

Every place that hands a browser an access/refresh token (magic-link verify, the
token-obtain view, the cookie refresh view) and the one place that takes them away
(logout) goes through here, so the cookie attributes and — critically — the
``max_age`` values can never drift apart again.

The historical bug this module exists to prevent: the refresh view set the access
cookie with no ``max_age``, which makes it a *session* cookie that dies when the
browser closes, and it never re-issued the refresh cookie at all. Combined with a
one-day ``REFRESH_TOKEN_LIFETIME`` that meant a login could not outlive 24 hours no
matter how recently the user was active.

Cookie lifetimes are derived from ``SIMPLE_JWT`` lifetimes so the browser drops a
cookie at (or after) the moment the token inside it stops being useful. The refresh
cookie gets a small grace margin so the browser keeps presenting it right up to the
token's own expiry rather than a few seconds early.
"""

from django.conf import settings

ACCESS_COOKIE = 'access_token'
REFRESH_COOKIE = 'refresh_token'

# Production cookies are shared across mlai.au subdomains (mlai.au ↔ api.mlai.au),
# which requires SameSite=None + Secure. Local dev is host-only over plain HTTP.
PRODUCTION_COOKIE_DOMAIN = '.mlai.au'


def _is_production() -> bool:
    return not settings.DEBUG


def cookie_kwargs() -> dict:
    """Attributes shared by every auth cookie we set or delete."""
    is_production = _is_production()
    return {
        'httponly': True,
        'path': '/',
        'domain': PRODUCTION_COOKIE_DOMAIN if is_production else None,
        'secure': is_production,
        'samesite': 'None' if is_production else 'Lax',
    }


def access_cookie_max_age() -> int:
    return int(settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds())


def refresh_cookie_max_age() -> int:
    return int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds())


def set_auth_cookies(response, access_token=None, refresh_token=None):
    """Attach the auth cookies present in the arguments.

    ``refresh_token`` is optional because a refresh only re-issues one when
    ``ROTATE_REFRESH_TOKENS`` is on; when it is absent the browser keeps the
    refresh cookie it already has.
    """
    kwargs = cookie_kwargs()
    if access_token:
        response.set_cookie(
            key=ACCESS_COOKIE,
            value=access_token,
            max_age=access_cookie_max_age(),
            **kwargs,
        )
    if refresh_token:
        response.set_cookie(
            key=REFRESH_COOKIE,
            value=refresh_token,
            max_age=refresh_cookie_max_age(),
            **kwargs,
        )
    return response


def clear_auth_cookie(response, key):
    """Delete one auth cookie using the same attributes it was set with."""
    if key not in (ACCESS_COOKIE, REFRESH_COOKIE):
        raise ValueError(f'Unsupported auth cookie: {key}')
    kwargs = cookie_kwargs()
    response.delete_cookie(
        key,
        path=kwargs['path'],
        domain=kwargs['domain'],
        samesite=kwargs['samesite'],
    )
    return response


def clear_auth_cookies(response):
    """Delete both auth cookies using the same attributes they were set with."""
    for key in (ACCESS_COOKIE, REFRESH_COOKIE):
        clear_auth_cookie(response, key)
    return response


def clear_django_session_cookies(response):
    """Delete Django's session and CSRF cookies with their configured scope.

    Production sessions use ``Domain=.mlai.au``. Deleting only a host-scoped
    cookie leaves that parent-domain session alive, so logout must mirror the
    configured name, path, domain and SameSite attributes for both cookies.
    """
    for prefix in ('SESSION', 'CSRF'):
        response.delete_cookie(
            key=getattr(settings, f'{prefix}_COOKIE_NAME'),
            path=getattr(settings, f'{prefix}_COOKIE_PATH'),
            domain=getattr(settings, f'{prefix}_COOKIE_DOMAIN'),
            samesite=getattr(settings, f'{prefix}_COOKIE_SAMESITE'),
        )
    return response
