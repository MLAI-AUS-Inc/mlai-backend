import hashlib
import logging
import secrets
from datetime import timedelta
from urllib.parse import quote
from uuid import UUID

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from .email_utils import send_password_reset_email
from .models import PasswordResetChallenge


logger = logging.getLogger(__name__)
User = get_user_model()
_DUMMY_PASSWORD_HASH = make_password('mlai-password-reset-timing-padding')


class InvalidPasswordResetToken(ValueError):
    pass


def normalize_account_email(email):
    return User.objects.normalize_email(email)


def authenticate_account(request, email, password):
    """Authenticate without a cheap, account-enumerable failure path."""

    canonical_email = normalize_account_email(email)
    candidate = User.objects.filter(email=canonical_email).first()
    if candidate is None or not candidate.has_usable_password():
        check_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if not candidate.is_active:
        candidate.check_password(password)
        return None
    return authenticate(request=request, email=canonical_email, password=password)


def _hash_secret(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _is_placeholder_email(email):
    return str(email or '').lower().endswith('@slack.placeholder.com')


def _reset_link(token):
    base_url = str(settings.COMMUNITY_CHAT_PASSWORD_RESET_URL).strip()
    separator = '&' if '?' in base_url else '?'
    return f'{base_url}{separator}token={quote(token, safe="")}'


def issue_password_reset(email, *, requested_ip_hash=''):
    """Issue and email one setup/reset token without revealing account state."""

    canonical_email = normalize_account_email(email)
    # Always perform one password-hash verification so missing/ineligible users
    # do not take the conspicuously cheap path.
    check_password('invalid-request', _DUMMY_PASSWORD_HASH)
    user = User.objects.filter(email=canonical_email, is_active=True).first()
    if user is None or _is_placeholder_email(user.email):
        return False

    now = timezone.now()
    secret = secrets.token_urlsafe(32)
    with transaction.atomic():
        PasswordResetChallenge.objects.filter(
            user=user,
            consumed_at__isnull=True,
        ).update(consumed_at=now)
        challenge = PasswordResetChallenge.objects.create(
            user=user,
            secret_hash=_hash_secret(secret),
            requested_ip_hash=requested_ip_hash,
            expires_at=now + timedelta(seconds=settings.PASSWORD_RESET_TTL_SECONDS),
        )
    token = f'{challenge.id.hex}.{secret}'
    try:
        send_password_reset_email(user, _reset_link(token))
    except Exception:
        logger.exception('Password reset email delivery failed for user_id=%s', user.id)
    return True


def _parse_reset_token(token):
    try:
        selector, secret = str(token or '').split('.', 1)
        challenge_id = UUID(selector)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidPasswordResetToken('Invalid or expired password token.') from exc
    if len(secret) < 32 or len(secret) > 128:
        raise InvalidPasswordResetToken('Invalid or expired password token.')
    return challenge_id, secret


def consume_password_reset(token, new_password):
    challenge_id, secret = _parse_reset_token(token)
    now = timezone.now()
    with transaction.atomic():
        try:
            challenge = (
                PasswordResetChallenge.objects.select_for_update()
                .select_related('user')
                .get(id=challenge_id)
            )
        except PasswordResetChallenge.DoesNotExist as exc:
            raise InvalidPasswordResetToken('Invalid or expired password token.') from exc
        if (
            challenge.consumed_at is not None
            or challenge.expires_at <= now
            or not secrets.compare_digest(challenge.secret_hash, _hash_secret(secret))
            or not challenge.user.is_active
        ):
            raise InvalidPasswordResetToken('Invalid or expired password token.')

        user = User.objects.select_for_update().get(pk=challenge.user_id)
        validate_password(new_password, user=user)
        user.set_password(new_password)
        if user.email_verified_at is None:
            user.email_verified_at = now
        user.auth_version += 1
        user.save(
            update_fields=(
                'password',
                'password_set_at',
                'email_verified_at',
                'auth_version',
            )
        )
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        PasswordResetChallenge.objects.filter(
            user=user,
            consumed_at__isnull=True,
        ).exclude(id=challenge.id).update(consumed_at=now)
    return user


def change_password(user, current_password, new_password):
    if not user.check_password(current_password):
        return False
    validate_password(new_password, user=user)
    now = timezone.now()
    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        if not locked_user.check_password(current_password):
            return False
        locked_user.set_password(new_password)
        locked_user.auth_version += 1
        locked_user.save(update_fields=('password', 'password_set_at', 'auth_version'))
        PasswordResetChallenge.objects.filter(
            user=locked_user,
            consumed_at__isnull=True,
        ).update(consumed_at=now)
    return True


def password_validation_messages(error):
    if isinstance(error, DjangoValidationError):
        return list(error.messages)
    return ['Password does not meet MLAI security requirements.']
