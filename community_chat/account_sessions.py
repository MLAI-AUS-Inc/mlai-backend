import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import CommunityChatAccountSession

ACCESS_TOKEN_PREFIX = "mlai_session_access_"
REFRESH_TOKEN_PREFIX = "mlai_session_refresh_"


class InvalidAccountSession(ValueError):
    pass


@dataclass(frozen=True)
class AccountSessionCredentials:
    session: CommunityChatAccountSession
    access_token: str
    refresh_token: str


def _hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _new_token(prefix):
    return f"{prefix}{secrets.token_urlsafe(48)}"


def issue_account_session(user, challenge):
    now = timezone.now()
    access_token = _new_token(ACCESS_TOKEN_PREFIX)
    refresh_token = _new_token(REFRESH_TOKEN_PREFIX)
    with transaction.atomic():
        # Device DELETE owns this same user-first boundary before revoking all
        # key/installation credentials. A session issuance that began earlier
        # therefore commits before the delete and is revoked by it, or waits
        # and becomes an explicit post-delete authorization.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        CommunityChatAccountSession.objects.filter(
            user=user,
            client_id=challenge.client_id,
            installation_id=challenge.installation_id,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        session = CommunityChatAccountSession.objects.create(
            user=user,
            public_key=challenge.public_key,
            installation_id=challenge.installation_id,
            client_id=challenge.client_id,
            origin=challenge.origin,
            platform=challenge.platform,
            name=challenge.device_name,
            access_token_hash=_hash_token(access_token),
            refresh_token_hash=_hash_token(refresh_token),
            auth_version=user.auth_version,
            access_expires_at=now
            + timedelta(seconds=settings.COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS),
            expires_at=now
            + timedelta(days=settings.COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS),
            last_used_at=now,
        )
    return AccountSessionCredentials(session, access_token, refresh_token)


def _valid_session(session, now):
    return bool(
        session
        and session.revoked_at is None
        and session.expires_at > now
        and session.user.is_active
        and session.auth_version == session.user.auth_version
    )


def authenticate_access_token(raw_token):
    if not str(raw_token).startswith(ACCESS_TOKEN_PREFIX):
        raise InvalidAccountSession("invalid_session")
    now = timezone.now()
    session = (
        CommunityChatAccountSession.objects.select_related("user")
        .filter(access_token_hash=_hash_token(raw_token))
        .first()
    )
    if not _valid_session(session, now) or session.access_expires_at <= now:
        raise InvalidAccountSession("invalid_session")
    CommunityChatAccountSession.objects.filter(id=session.id).update(last_used_at=now)
    session.last_used_at = now
    return session


def rotate_account_session(raw_refresh_token, *, required_origin=None):
    if not str(raw_refresh_token).startswith(REFRESH_TOKEN_PREFIX):
        raise InvalidAccountSession("invalid_session")
    now = timezone.now()
    with transaction.atomic():
        session = (
            # Lock only the credential row. Device authority transitions take
            # the user lock before revoking sessions; locking the joined user
            # here would invert that order (session->user vs user->session).
            CommunityChatAccountSession.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(refresh_token_hash=_hash_token(raw_refresh_token))
            .first()
        )
        if not _valid_session(session, now):
            raise InvalidAccountSession("invalid_session")
        if required_origin is not None and not secrets.compare_digest(
            session.origin,
            required_origin,
        ):
            raise InvalidAccountSession("invalid_session")
        access_token = _new_token(ACCESS_TOKEN_PREFIX)
        refresh_token = _new_token(REFRESH_TOKEN_PREFIX)
        session.access_token_hash = _hash_token(access_token)
        session.refresh_token_hash = _hash_token(refresh_token)
        session.access_expires_at = now + timedelta(
            seconds=settings.COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS
        )
        session.expires_at = now + timedelta(
            days=settings.COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS
        )
        session.last_used_at = now
        session.save(
            update_fields=(
                "access_token_hash",
                "refresh_token_hash",
                "access_expires_at",
                "expires_at",
                "last_used_at",
                "updated_at",
            )
        )
    return AccountSessionCredentials(session, access_token, refresh_token)


def revoke_account_session(raw_refresh_token, *, required_origin=None):
    if not str(raw_refresh_token).startswith(REFRESH_TOKEN_PREFIX):
        raise InvalidAccountSession("invalid_session")
    now = timezone.now()
    with transaction.atomic():
        session = (
            CommunityChatAccountSession.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(refresh_token_hash=_hash_token(raw_refresh_token))
            .first()
        )
        if not _valid_session(session, now):
            raise InvalidAccountSession("invalid_session")
        if required_origin is not None and not secrets.compare_digest(
            session.origin,
            required_origin,
        ):
            raise InvalidAccountSession("invalid_session")
        session.revoked_at = now
        session.save(update_fields=("revoked_at", "updated_at"))
    return session
