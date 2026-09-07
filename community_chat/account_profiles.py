"""Versioned updates to the public fields of a member's canonical account."""

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from .models import CommunityChatAccountSession
from .serializers import profile_version_for_user


class ProfileVersionConflict(ValueError):
    """The account changed after the client last read its profile."""


def update_account_profile(*, authenticated_session, values):
    """Update only public profile fields under the account/session lock order.

    Recheck session authority after acquiring the locks so revocation, account
    deactivation and credential rotation cannot race a queued profile write.
    The caller must validate ``values`` with the profile update serializer.
    """
    with transaction.atomic():
        user_model = get_user_model()
        user = user_model.objects.select_for_update().get(
            pk=authenticated_session.user_id,
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
                session.access_token_hash,
                authenticated_session.access_token_hash,
            )
        ):
            raise AuthenticationFailed("MLAI Chat session has expired.")
        if values["profile_version"] != profile_version_for_user(user):
            raise ProfileVersionConflict()

        changes = {}
        if "display_name" in values:
            user.full_name = values["display_name"]
            changes.update(first_name=user.first_name, last_name=user.last_name)
        for field in ("about", "avatar_url"):
            if field in values:
                changes[field] = values[field]

        previous_timestamp = user.updated_at or user.date_joined
        changes["updated_at"] = (
            max(
                now,
                previous_timestamp + timedelta(microseconds=1),
            )
            if previous_timestamp
            else now
        )
        # Updating explicit fields also keeps the version monotonic when the
        # wall clock moves backwards. Model.save(auto_now) would replace it.
        user_model.objects.filter(pk=user.pk).update(**changes)
        for field, value in changes.items():
            setattr(user, field, value)
        return user
