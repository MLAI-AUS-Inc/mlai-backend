"""Durable account denial shared by sign-in, signup and profile changes."""

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import AccountBan, User


def account_is_banned(user):
    """Check retained identity, including a canonical email collision."""
    return AccountBan.objects.filter(
        Q(user_id=user.pk) | Q(email=User.objects.normalize_email(user.email)),
        revoked_at__isnull=True,
    ).exists()


def guard_account_save(user):
    """Prevent implicit reactivation, replacement accounts and email erasure."""
    ban = AccountBan.objects.filter(
        Q(user_id=user.pk) | Q(email=user.email),
        revoked_at__isnull=True,
    ).first()
    if ban and (user.is_active or user.pk != ban.user_id or user.email != ban.email):
        raise ValidationError(
            "This account is unavailable. Contact an MLAI administrator."
        )
