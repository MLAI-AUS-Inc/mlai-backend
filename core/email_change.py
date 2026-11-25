"""Shared account mutation used after current and replacement mailbox proof."""

from django.db.models import Q
from datetime import timedelta

from .email_aliases import lock_email_namespace
from .models import (
    AccountBan, PasswordResetChallenge, PasswordResetDeliveryStatus,
    PasswordResetEmailDelivery, User,
)


# Reserved until legacy email-derived privilege is moved to a stable identity.
RESERVED_ADDRESSES = frozenset({"hi@mlai.au"})


def reserved_email(email):
    """Return whether an address is outside the self-service namespace."""
    return email in RESERVED_ADDRESSES or email.endswith("@slack.placeholder.com")


def commit_verified_email_locked(user, new_email, *, now):
    """Change the canonical identifier under the caller's user-row lock."""
    for address in sorted({user.email, new_email}):
        lock_email_namespace(address)

    if (
        not user.is_active
        or user.is_staff
        or user.is_superuser
        or reserved_email(user.email)
        or reserved_email(new_email)
        or user.email == new_email
        or User.objects.for_email(new_email).exists()
        or AccountBan.objects.filter(
            Q(user=user) | Q(email__in=(user.email, new_email)), revoked_at__isnull=True,
        ).exists()
    ):
        return False

    now_for_profile = max(now, (user.updated_at or now) + timedelta(microseconds=1))
    User.objects.filter(pk=user.pk).update(
        email=new_email,
        email_verified_at=now,
        auth_version=user.auth_version + 1,
        updated_at=now_for_profile,
    )
    cancel_password_resets_locked(user, now=now)
    return True


def cancel_password_resets_locked(user, *, now):
    """Invalidate and scrub outstanding password delivery under the user lock."""
    open_challenges = PasswordResetChallenge.objects.filter(user=user, consumed_at__isnull=True)
    PasswordResetEmailDelivery.objects.filter(
        challenge__in=open_challenges,
        status__in=(PasswordResetDeliveryStatus.PENDING, PasswordResetDeliveryStatus.SENDING),
    ).update(
        status=PasswordResetDeliveryStatus.CANCELLED, encrypted_reset_link="",
        claimed_at=None, updated_at=now,
    )
    open_challenges.update(consumed_at=now)
