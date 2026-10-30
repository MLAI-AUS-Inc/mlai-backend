"""Desktop sign-in v2: one short browser trip, within ADR-006 (P1.4).

Everything here is behind ``COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED`` (code
default off) and the existing ``COMMUNITY_CHAT_DEVICE_AUTH_ENABLED``.
``community_chat.views`` only delegates to these helpers:

- ``flow``: 2 on start responses and the anonymous probe while v2 is on, else
  1. After sign-in, a founder who owns any legacy-cohort startup keeps flow 1,
  so their browser page keeps today's Approve click.
- The fresh-code marker: after an email code is verified on the browser page
  that names a pending desktop request, the cache remembers which account and
  which browser session verified it, for 600 s. Only that same account in that
  same browser session reads ``fresh: true``. A browser that was already signed
  in never gets a marker, so it keeps the Approve click.
- Renew: a pending request is extended while the desktop still holds its state,
  up to ``COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS`` after it started.
- Pending and new members may authorize and exchange; the exchange then issues
  the account session only, never a relay bootstrap token.

No migration: markers and renew proofs live in the cache, and renewal moves
the existing ``CommunityChatDeviceAuthRequest.expires_at``.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import CommunityChatDeviceAuthRequest

FRESH_CODE_TTL_SECONDS = 600
FRESH_CACHE_PREFIX = "device-auth-fresh:"
RENEW_CACHE_PREFIX = "device-auth-renew:"
DEFAULT_MAX_LIFETIME_SECONDS = 3600
WEB_CLIENT_ID = "mlai-chat-web"
FLOW_V1 = 1
FLOW_V2 = 2
EXPIRED_DETAIL = "This sign-in link expired. Start again."


def enabled() -> bool:
    """True only when both the v1 handoff and the v2 switch are on."""
    return bool(
        getattr(settings, "COMMUNITY_CHAT_DEVICE_AUTH_ENABLED", False)
        and getattr(settings, "COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED", False)
    )


def max_lifetime_seconds() -> int:
    """The hard cap on a request's life, renewals included."""
    return int(getattr(settings, "COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS", DEFAULT_MAX_LIFETIME_SECONDS))


def anonymous_flow() -> int:
    """The flow a client may assume before anyone signs in."""
    return FLOW_V2 if enabled() else FLOW_V1


def start_fields() -> dict:
    """Additive keys for the ``auth/device/start/`` POST response (none while off)."""
    return {"flow": FLOW_V2} if enabled() else {}


def parse_request_id(value) -> Optional[uuid.UUID]:
    """A request id from untrusted input, or None."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def pending_request(request_id) -> Optional[CommunityChatDeviceAuthRequest]:
    """The request when it exists, is unconsumed and has not expired."""
    parsed = parse_request_id(request_id)
    if parsed is None:
        return None
    auth_request = CommunityChatDeviceAuthRequest.objects.filter(id=parsed).first()
    if auth_request is None or auth_request.consumed_at is not None or auth_request.expires_at <= timezone.now():
        return None
    return auth_request


def probe_payload(request_id=None) -> dict:
    """The anonymous probe: ``flow``, plus ``requestedAt`` for a pending request."""
    payload = {"flow": anonymous_flow()}
    if not enabled() or request_id in (None, ""):
        return payload
    auth_request = pending_request(request_id)
    if auth_request is not None:
        payload["requestedAt"] = auth_request.created_at
    return payload


def user_flow(user) -> int:
    """Flow 1 for a founder with any legacy-cohort startup (G6), else flow 2."""
    if not enabled():
        return FLOW_V1
    from content_factory.founder_journey_cohort import user_has_legacy_org
    return FLOW_V1 if user_has_legacy_org(user) else FLOW_V2


def session_fingerprint(account_session) -> str:
    """First 32 hex of the SHA-256 of the browser session's stable identity.

    The account session id survives access-token refreshes but differs for
    every sign-in, so another browser (or a later sign-in) never matches.
    """
    return hashlib.sha256(f"community-chat-browser-session:{account_session.pk}".encode("utf-8")).hexdigest()[:32]


def record_fresh_code(*, device_request_id, user, account_session, client_id) -> bool:
    """Remember that this browser session just verified a code for this request."""
    if not enabled() or client_id != WEB_CLIENT_ID or device_request_id in (None, ""):
        return False
    auth_request = pending_request(device_request_id)
    if auth_request is None or (auth_request.user_id is not None and auth_request.user_id != user.pk):
        return False
    cache.set(
        f"{FRESH_CACHE_PREFIX}{auth_request.id}",
        {"user": user.pk, "session": session_fingerprint(account_session)},
        FRESH_CODE_TTL_SECONDS,
    )
    return True


def is_fresh(auth_request, *, user, account_session) -> bool:
    """True only when the marker's account and browser session both match."""
    marker = cache.get(f"{FRESH_CACHE_PREFIX}{auth_request.id}")
    if not isinstance(marker, dict):
        return False
    session = marker.get("session")
    return (
        marker.get("user") == user.pk
        and isinstance(session, str)
        and secrets.compare_digest(session, session_fingerprint(account_session))
    )


def clear_fresh(auth_request_id) -> None:
    """A marker is single use: drop it once the request is authorized."""
    cache.delete(f"{FRESH_CACHE_PREFIX}{auth_request_id}")


def authorize_payload(auth_request, *, user, account_session) -> dict:
    """The ``GET auth/device/authorize/`` body for the signed-in browser."""
    return {
        "signedInAs": user.email,
        "fresh": is_fresh(auth_request, user=user, account_session=account_session),
        "flow": user_flow(user),
        "expiresAt": auth_request.expires_at,
        "requestedAt": auth_request.created_at,
    }


def admits_unapproved_member(user) -> bool:
    """Pending and new members may finish desktop sign-in (not rejected or suspended)."""
    if not enabled() or user is None or not getattr(user, "is_active", False):
        return False
    from .onboarding_models import CommunityMemberProfile
    profile = CommunityMemberProfile.objects.filter(user=user).only("status").first()
    if profile is None:
        return True
    return profile.status in {CommunityMemberProfile.Status.INCOMPLETE, CommunityMemberProfile.Status.PENDING}


def _renew_digest(state: str) -> str:
    return hashlib.sha256(b"mlai-chat-device-auth-renew-v1\0" + state.encode("utf-8")).hexdigest()


def remember_renew_proof(auth_request, state: str) -> None:
    """Keep a digest of the start state so the desktop can renew with it alone."""
    if not enabled():
        return
    cache.set(f"{RENEW_CACHE_PREFIX}{auth_request.id}", _renew_digest(state), max(1, max_lifetime_seconds()))


def renew_proof_matches(auth_request, state: str) -> bool:
    """True when ``state`` is the one the request was started with."""
    stored = cache.get(f"{RENEW_CACHE_PREFIX}{auth_request.id}")
    return isinstance(stored, str) and secrets.compare_digest(stored, _renew_digest(state))


class RenewExpired(Exception):
    """The request is consumed, already expired, or past its hard lifetime cap."""


def renewed_expiry(auth_request, *, now=None):
    """Extend a locked, live, unauthorized request by one TTL, within the lifetime cap.

    An authorized request keeps its expiry (the desktop should exchange it now).
    Raises ``RenewExpired`` when the request is consumed, past the cap, or
    already expired. An expired request is never revived: device revocation
    fences in-flight handoffs by setting ``expires_at`` to now, and reviving
    one would undo that fence. The desktop renews before expiry, and a 410
    makes it start a new request.
    """
    now = now or timezone.now()
    cap = auth_request.created_at + timedelta(seconds=max_lifetime_seconds())
    if auth_request.consumed_at is not None or now >= cap or auth_request.expires_at <= now:
        raise RenewExpired()
    if auth_request.authorized_at is not None:
        return auth_request.expires_at
    extended = now + timedelta(seconds=int(settings.COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS))
    return min(max(auth_request.expires_at, extended), cap)
