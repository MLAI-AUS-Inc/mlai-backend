"""Shared-cache revocation for rotating refresh-token session families.

SimpleJWT changes the JTI whenever a refresh token rotates. Revoking only the
currently presented JTI therefore leaves older rotated copies usable. New MLAI
refresh tokens carry a stable, random family identifier which survives rotation;
logout revokes that family in the production shared Redis/Valkey cache until the
token's natural expiry. Tokens issued before this change use a user-level cutoff
which invalidates every legacy rotation without affecting new family tokens.
"""

import time
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken


REFRESH_SESSION_CLAIM = 'refresh_session_id'
REVOCATION_CACHE_PREFIX = 'auth:revoked-refresh-session:'
LEGACY_REVOCATION_CACHE_PREFIX = 'auth:revoked-legacy-refresh-before:'


class RefreshRevocationUnavailable(RuntimeError):
    """The shared revocation store could not confirm a read or write."""


def add_refresh_session_claim(token):
    """Add a stable rotation-family claim to a newly minted refresh token."""
    if not token.payload.get(REFRESH_SESSION_CLAIM):
        token[REFRESH_SESSION_CLAIM] = uuid4().hex
    return token


def issue_refresh_token(user):
    return add_refresh_session_claim(RefreshToken.for_user(user))


def _validated_identifier(value, *, error_message):
    identifier = str(value or '').strip()
    if not identifier or len(identifier) > 128:
        raise TokenError(error_message)
    return identifier


def _legacy_user_identifier(token):
    identifier = token.payload.get(api_settings.USER_ID_CLAIM)
    return _validated_identifier(
        identifier,
        error_message='Legacy refresh token is missing a valid user identifier',
    )


def _legacy_issued_at(token):
    try:
        return int(token.payload['iat'])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError('Legacy refresh token is missing a valid issued-at time') from exc


def _legacy_revocation_cache_key(token):
    return f'{LEGACY_REVOCATION_CACHE_PREFIX}{_legacy_user_identifier(token)}'


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception as exc:
        raise RefreshRevocationUnavailable(
            'Refresh-token revocation state is unavailable'
        ) from exc


def _cache_set(key, value, *, timeout):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as exc:
        raise RefreshRevocationUnavailable(
            'Refresh-token revocation state could not be persisted'
        ) from exc


def _refresh_family_identifier_or_none(token):
    identifier = token.payload.get(REFRESH_SESSION_CLAIM)
    if identifier is None:
        return None
    identifier = str(identifier or '').strip()
    if not identifier or len(identifier) > 128:
        raise TokenError('Refresh token is missing a valid session identifier')
    return identifier


def _maximum_refresh_lifetime_seconds():
    # A different rotation from the same family may have been minted after the
    # token presented at logout and therefore expire later. Retain revocation
    # for a full refresh lifetime from logout so no sibling can become valid
    # again when the presented token's own expiry passes.
    return int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds())


def ensure_refresh_session_active(token):
    """Fail closed when a refresh family is revoked or cannot be checked."""
    family_identifier = _refresh_family_identifier_or_none(token)
    if family_identifier is not None:
        if _cache_get(f'{REVOCATION_CACHE_PREFIX}{family_identifier}'):
            raise TokenError('Refresh-token session has been revoked')
        return token

    # Tokens minted before the family claim shipped cannot identify one browser
    # session across rotations. Fail safely by applying a user-level cutoff only
    # to those legacy tokens; newly issued family tokens are unaffected.
    revoked_before = _cache_get(_legacy_revocation_cache_key(token))
    if revoked_before is not None and _legacy_issued_at(token) <= int(revoked_before):
        raise TokenError('Legacy refresh-token session has been revoked')
    return token


def revoke_refresh_credential(raw_token):
    """Validate and revoke the presented refresh token's rotation family."""
    token = RefreshToken(raw_token)
    family_identifier = _refresh_family_identifier_or_none(token)
    if family_identifier is not None:
        _cache_set(
            f'{REVOCATION_CACHE_PREFIX}{family_identifier}',
            True,
            timeout=_maximum_refresh_lifetime_seconds(),
        )
    else:
        # Cover every legacy rotated token for this user, whose expiry can be
        # later than the particular token presented at logout.
        _cache_set(
            _legacy_revocation_cache_key(token),
            int(time.time()),
            timeout=_maximum_refresh_lifetime_seconds(),
        )
    return token
