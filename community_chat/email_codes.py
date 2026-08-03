import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .email_delivery import encrypt_email_code
from .models import (
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    EmailCodeDeliveryStatus,
)


User = get_user_model()


class InvalidEmailCode(ValueError):
    pass


def normalize_email(email):
    return User.objects.normalize_email(email)


def _hmac_hex(domain, value):
    key = str(settings.COMMUNITY_CHAT_EMAIL_CODE_PEPPER).encode("utf-8")
    return hmac.new(
        key,
        f"{domain}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def email_digest(email):
    return _hmac_hex("email", normalize_email(email))


def code_digest(challenge_id, code):
    return _hmac_hex("code", f"{challenge_id.hex}:{code}")


def is_email_code_eligible(user):
    return bool(
        user
        and user.is_active
        and not str(user.email).lower().endswith("@slack.placeholder.com")
        and user.community_chat_profile_id
    )


def issue_email_code_challenge(
    *,
    email,
    client_id,
    installation_id,
    origin,
    platform,
    device_name,
    public_key,
    requested_ip_digest="",
):
    """Create a uniform challenge and queue delivery only for eligible users."""

    canonical_email = normalize_email(email)
    digest = email_digest(canonical_email)
    user = User.objects.filter(email=canonical_email).first()
    eligible_user = user if is_email_code_eligible(user) else None
    challenge_id = uuid.uuid4()
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = timezone.now()
    with transaction.atomic():
        active = CommunityChatEmailCodeChallenge.objects.filter(
            email_digest=digest,
            client_id=client_id,
            installation_id=installation_id,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        )
        CommunityChatEmailCodeDelivery.objects.filter(
            challenge__in=active,
            status__in=(
                EmailCodeDeliveryStatus.PENDING,
                EmailCodeDeliveryStatus.SENDING,
            ),
        ).update(
            status=EmailCodeDeliveryStatus.CANCELLED,
            encrypted_code="",
            claimed_at=None,
            updated_at=now,
        )
        active.update(invalidated_at=now)
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            id=challenge_id,
            user=eligible_user,
            email_digest=digest,
            code_digest=code_digest(challenge_id, code),
            client_id=client_id,
            installation_id=installation_id,
            origin=origin,
            platform=platform,
            device_name=device_name,
            public_key=public_key,
            expires_at=now
            + timedelta(seconds=settings.COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS),
            max_attempts=settings.COMMUNITY_CHAT_EMAIL_CODE_MAX_ATTEMPTS,
            requested_ip_digest=requested_ip_digest,
        )
        if eligible_user is not None:
            CommunityChatEmailCodeDelivery.objects.create(
                challenge=challenge,
                encrypted_code=encrypt_email_code(code),
            )
    return challenge


def consume_email_code(*, challenge_id, code, client_id, installation_id):
    """Consume one valid code and return its eligible user and device context."""

    now = timezone.now()
    invalid = False
    with transaction.atomic():
        try:
            challenge = (
                CommunityChatEmailCodeChallenge.objects.select_for_update()
                .select_related("user")
                .get(id=challenge_id)
            )
        except (CommunityChatEmailCodeChallenge.DoesNotExist, ValueError):
            challenge = None

        valid_state = bool(challenge) and (
            challenge.client_id == client_id
            and challenge.installation_id == installation_id
            and challenge.consumed_at is None
            and challenge.invalidated_at is None
            and challenge.expires_at > now
            and challenge.attempt_count < challenge.max_attempts
            and is_email_code_eligible(challenge.user)
        )
        expected_digest = code_digest(challenge.id, code) if challenge else ""
        if not valid_state or not secrets.compare_digest(
            challenge.code_digest if challenge else "",
            expected_digest,
        ):
            if (
                challenge is not None
                and challenge.consumed_at is None
                and challenge.invalidated_at is None
                and challenge.expires_at > now
            ):
                challenge.attempt_count += 1
                update_fields = ["attempt_count"]
                if challenge.attempt_count >= challenge.max_attempts:
                    challenge.invalidated_at = now
                    update_fields.append("invalidated_at")
                challenge.save(update_fields=update_fields)
            invalid = True
        else:
            challenge.consumed_at = now
            challenge.save(update_fields=("consumed_at",))
            user = challenge.user
            if user.email_verified_at is None:
                user.email_verified_at = now
                user.save(update_fields=("email_verified_at",))
            CommunityChatEmailCodeChallenge.objects.filter(
                user=user,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
            ).exclude(id=challenge.id).update(invalidated_at=now)
            return user, challenge
    if invalid:
        raise InvalidEmailCode("invalid_or_expired_code")
