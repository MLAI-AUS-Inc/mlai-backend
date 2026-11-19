"""Versioned own-account outfit reads and writes with fresh rank enforcement."""

import secrets
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from community_chat.models import CommunityChatAccountSession
from community_chat.onboarding import require_community_access
from community_chat.volunteer.access import community_id
from community_chat.volunteer.policy import progress
from community_chat.volunteer.services import contribution_total, state_for

from .models import CommunityRooAvatar
from .policy import (
    CATALOG_VERSION,
    SLOTS,
    RooAvatarError,
    catalog_for,
    effective_outfit,
    empty_outfit,
    require_unlocked,
    validate_update,
)


def _locked_account(authenticated_session):
    """Keep the existing user-before-session lock and revocation contracts."""
    user = (
        get_user_model()
        .objects.select_for_update()
        .get(pk=authenticated_session.user_id)
    )
    session = (
        CommunityChatAccountSession.objects.select_for_update()
        .filter(pk=authenticated_session.pk, user=user)
        .first()
    )
    now = timezone.now()
    if (
        session is None
        or not user.is_active
        or session.revoked_at is not None
        or session.auth_version != user.auth_version
        or session.access_expires_at <= now
        or session.expires_at <= now
        or not secrets.compare_digest(
            session.access_token_hash, authenticated_session.access_token_hash
        )
    ):
        raise AuthenticationFailed("MLAI Chat session has expired.")
    require_community_access(user)
    return user


def _scope():
    scope = community_id()
    relay_url = settings.COMMUNITY_CHAT_RELAY_URL
    if not scope or len(scope) > 255 or not urlparse(relay_url).hostname:
        raise RooAvatarError("roo_avatar_scope_unavailable", 503)
    return scope, relay_url


def _rank(user):
    total = contribution_total(user, state_for(user))
    return progress(total)["current_level"] if total is not None else None


def _snapshot(user, scope, relay_url, row, rank):
    outfit = {slot: getattr(row, slot) for slot in SLOTS} if row else empty_outfit()
    level = rank["level"] if rank else None
    return {
        "account_id": str(user.community_chat_profile_id),
        "community_id": scope,
        "relay_url": relay_url,
        "catalog_version": CATALOG_VERSION,
        "version": row.version if row else 0,
        "current_level": level,
        "rank_name": rank["name"] if rank else None,
        "history_reconciled": rank is not None,
        "outfit": outfit,
        "effective_outfit": effective_outfit(outfit, level),
        "catalog": catalog_for(level),
        "updated_at": row.updated_at.isoformat() if row else None,
    }


@transaction.atomic
def read_avatar(*, authenticated_session):
    """Read only this account's saved outfit and current verified eligibility."""
    user = _locked_account(authenticated_session)
    scope, relay_url = _scope()
    rank = _rank(user)
    row = CommunityRooAvatar.objects.filter(user=user, community=scope).first()
    return _snapshot(user, scope, relay_url, row, rank)


def update_avatar(*, authenticated_session, values):
    """Validate and replace an eligible outfit without modifying rank or Roo."""
    version, outfit = validate_update(values)
    with transaction.atomic():
        user = _locked_account(authenticated_session)
        scope, relay_url = _scope()
        row = (
            CommunityRooAvatar.objects.select_for_update()
            .filter(user=user, community=scope)
            .first()
        )
        if version != (row.version if row else 0):
            raise RooAvatarError("roo_avatar_version_conflict", 409)
        # An outfit from a client without the fur slot keeps the saved fur.
        outfit.setdefault("fur", row.fur if row else None)
        rank = _rank(user)
        require_unlocked(outfit, rank["level"] if rank else None)
        if row is None:
            row = CommunityRooAvatar.objects.create(
                user=user, community=scope, **outfit
            )
        else:
            changes = {
                **outfit,
                "version": row.version + 1,
                "updated_at": max(
                    timezone.now(), row.updated_at + timedelta(microseconds=1)
                ),
            }
            CommunityRooAvatar.objects.filter(pk=row.pk).update(**changes)
            for key, value in changes.items():
                setattr(row, key, value)
        return _snapshot(user, scope, relay_url, row, rank)
