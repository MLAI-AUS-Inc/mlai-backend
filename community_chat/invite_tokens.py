"""Opaque, signed and short-lived MLAI Chat invite tokens (P1 one-screen join).

A workshop or individual invite link carries ``intent=startup`` for routing and
``invite=inv1.<token>``. The token is a Fernet token, so it is both encrypted
and authenticated: nothing in the URL reveals the cohort label, an email
address or a name, and a tampered token never resolves. The key is derived
from ``SECRET_KEY`` with HKDF-SHA256 under a purpose-specific ``info`` string,
so it is never shared with any other Fernet use in the project.

Tokens are stateless: there is no invite table. Expiry is the ``exp`` claim,
bounded by ``COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS``.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


TOKEN_PREFIX = "inv1."
TOKEN_VERSION = 1
HKDF_INFO = b"community-chat-invite-token-v1"
INVITE_INTENTS = ("startup",)
COHORT_MAX_LENGTH = 40
# Labels end up in logs; keep them to a plain, log-safe alphabet.
COHORT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z")
EMAIL_MAX_LENGTH = 254
NAME_MAX_LENGTH = 120
TOKEN_MAX_LENGTH = 2048
DEFAULT_TTL_HOURS = 72
DEFAULT_MAX_TTL_HOURS = 336
# Workshop links always point at production Chat; the token works on any host.
INVITE_LINK_BASE_URL = "https://chat.mlai.au"


class InvalidInviteToken(ValueError):
    """The token is malformed, tampered, expired or of an unknown version."""


@dataclass(frozen=True)
class InviteClaims:
    """The resolved, validated contents of one invite token."""

    intent: Optional[str]
    cohort: Optional[str]
    email: Optional[str]
    name: Optional[str]
    expires_at: int

    def public_payload(self) -> dict:
        """The resolve response: email and name only for individually sent invites."""
        return {"intent": self.intent, "cohort": self.cohort, "email": self.email, "name": self.name}


def invite_tokens_enabled() -> bool:
    """Whether invite tokens resolve (and count at join). Code default off."""
    return bool(getattr(settings, "COMMUNITY_CHAT_INVITE_TOKENS_ENABLED", False))


def default_ttl_hours() -> int:
    """The TTL a new token gets when none is requested."""
    return int(getattr(settings, "COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS", DEFAULT_TTL_HOURS))


def max_ttl_hours() -> int:
    """The longest TTL any token may carry; longer requests are clamped."""
    return int(getattr(settings, "COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS", DEFAULT_MAX_TTL_HOURS))


def clamp_ttl_hours(ttl_hours: Optional[int]) -> int:
    """Return a TTL between one hour and the configured maximum."""
    requested = default_ttl_hours() if ttl_hours is None else int(ttl_hours)
    return max(1, min(requested, max(1, max_ttl_hours())))


def _fernet() -> Fernet:
    secret = str(settings.SECRET_KEY).encode("utf-8")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(secret)
    return Fernet(base64.urlsafe_b64encode(key))


def _optional_text(value, *, max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInviteToken("invalid_field")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length or any(ord(char) < 32 for char in text):
        raise InvalidInviteToken("invalid_field")
    return text


def _validated(intent, cohort, email, name) -> tuple:
    if intent is not None and intent not in INVITE_INTENTS:
        raise InvalidInviteToken("invalid_intent")
    cohort = _optional_text(cohort, max_length=COHORT_MAX_LENGTH)
    if cohort is not None and not COHORT_RE.match(cohort):
        raise InvalidInviteToken("invalid_cohort")
    email = _optional_text(email, max_length=EMAIL_MAX_LENGTH)
    if email is not None and "@" not in email:
        raise InvalidInviteToken("invalid_email")
    name = _optional_text(name, max_length=NAME_MAX_LENGTH)
    return intent, cohort, email, name


def mint_invite_token(*, intent: Optional[str] = "startup", cohort: Optional[str] = None,
                      email: Optional[str] = None, name: Optional[str] = None,
                      ttl_hours: Optional[int] = None, now: Optional[float] = None) -> str:
    """Mint one ``inv1.`` token. Raises ``InvalidInviteToken`` for bad claims.

    ``ttl_hours`` defaults to ``COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS``
    and is clamped to ``COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS``.
    """
    intent, cohort, email, name = _validated(intent, cohort, email, name)
    issued = int(time.time() if now is None else now)
    payload = {
        "v": TOKEN_VERSION,
        "intent": intent,
        "cohort": cohort,
        "email": email,
        "name": name,
        "exp": issued + clamp_ttl_hours(ttl_hours) * 3600,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return TOKEN_PREFIX + _fernet().encrypt_at_time(body, issued).decode("ascii")


def resolve_invite_token(token, *, now: Optional[float] = None) -> InviteClaims:
    """Decrypt and validate one token, or raise ``InvalidInviteToken``."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > TOKEN_MAX_LENGTH:
        raise InvalidInviteToken("invalid_prefix")
    try:
        body = _fernet().decrypt(token[len(TOKEN_PREFIX):].encode("ascii"))
        payload = json.loads(body.decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise InvalidInviteToken("invalid_token") from exc
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        raise InvalidInviteToken("invalid_version")
    expires_at = payload.get("exp")
    if type(expires_at) is not int:
        raise InvalidInviteToken("invalid_expiry")
    current = time.time() if now is None else now
    if expires_at <= current:
        raise InvalidInviteToken("expired")
    intent, cohort, email, name = _validated(
        payload.get("intent"), payload.get("cohort"), payload.get("email"), payload.get("name"),
    )
    return InviteClaims(intent=intent, cohort=cohort, email=email, name=name, expires_at=expires_at)


def invite_link(token: str, *, intent: Optional[str] = "startup") -> str:
    """The shareable Chat link for a token. ``intent`` is routing only, never security."""
    query = f"intent={intent}&invite={token}" if intent else f"invite={token}"
    return f"{INVITE_LINK_BASE_URL}/?{query}"
