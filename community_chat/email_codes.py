import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone

from .email_delivery import decrypt_signup_email, encrypt_email_code, encrypt_signup_email
from .models import (
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    EmailCodeDeliveryStatus,
    CommunityMemberProfile,
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
    onboarding_version=0,
):
    """Create a uniform challenge and queue delivery only for eligible users."""

    canonical_email = normalize_email(email)
    digest = email_digest(canonical_email)
    user = User.objects.filter(email__iexact=canonical_email).first()
    eligible_user = user if is_email_code_eligible(user) else None
    signup = bool(
        user is None and settings.COMMUNITY_CHAT_SIGNUP_ENABLED and onboarding_version == 1
        and not canonical_email.endswith("@slack.placeholder.com")
    )
    challenge_id = uuid.uuid4()
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = timezone.now()
    with transaction.atomic():
        if eligible_user is not None:
            # Serialize creation of a new sign-in capability with server-side
            # device deletion. A challenge committed before DELETE is
            # invalidated there; one committed afterward is a fresh explicit
            # account proof rather than a surviving pre-delete credential.
            User.objects.select_for_update().get(pk=eligible_user.pk)
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
        active.update(invalidated_at=now, encrypted_signup_email="")
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
            encrypted_signup_email=encrypt_signup_email(canonical_email) if signup else "",
            onboarding_version=onboarding_version,
        )
        if eligible_user is not None or signup:
            CommunityChatEmailCodeDelivery.objects.create(
                challenge=challenge,
                encrypted_code=encrypt_email_code(code),
            )
    return challenge


def _locked_email_code_challenges():
    """Lock only challenge rows while loading the optional user relation.

    ``user`` is nullable so ``select_related`` uses a left outer join. PostgreSQL
    rejects an unscoped ``FOR UPDATE`` for that query because the nullable side
    of an outer join cannot be locked. Callers acquire the immutable eligible
    user's row first; this queryset then takes only the challenge lock so device
    deletion and code consumption use the same user->challenge order.
    """

    return CommunityChatEmailCodeChallenge.objects.select_for_update(
        of=("self",)
    ).select_related("user")


def consume_email_code(*, challenge_id, code, client_id, installation_id):
    """Consume one valid code and return its eligible user and device context."""

    now = timezone.now()
    invalid = False
    identity = (
        CommunityChatEmailCodeChallenge.objects.filter(id=challenge_id)
        .values("user_id")
        .first()
    )
    with transaction.atomic():
        locked_user = None
        if identity is not None and identity["user_id"] is not None:
            locked_user = User.objects.select_for_update().get(
                pk=identity["user_id"]
            )
        try:
            challenge = _locked_email_code_challenges().get(id=challenge_id)
        except (CommunityChatEmailCodeChallenge.DoesNotExist, ValueError):
            challenge = None

        signup = bool(challenge and challenge.user_id is None and challenge.encrypted_signup_email
                      and challenge.onboarding_version == 1 and settings.COMMUNITY_CHAT_SIGNUP_ENABLED)
        signup_email = ""
        if signup:
            try:
                signup_email = normalize_email(decrypt_signup_email(challenge.encrypted_signup_email))
                signup = bool(signup_email and secrets.compare_digest(email_digest(signup_email), challenge.email_digest))
            except (InvalidToken, UnicodeError, ValueError):
                signup = False
        valid_state = bool(challenge) and (
            (signup or (locked_user is not None and challenge.user_id == locked_user.pk
                        and is_email_code_eligible(locked_user)))
            and challenge.client_id == client_id
            and challenge.installation_id == installation_id
            and challenge.consumed_at is None
            and challenge.invalidated_at is None
            and challenge.expires_at > now
            and challenge.attempt_count < challenge.max_attempts
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
                    challenge.encrypted_signup_email = ""
                    update_fields.extend(("invalidated_at", "encrypted_signup_email"))
                challenge.save(update_fields=update_fields)
            invalid = True
        else:
            if signup:
                # Case-insensitive uniqueness in core.User resolves concurrent
                # verifications and a concurrent signup in another MLAI product.
                user, created = User.objects.get_or_create(email__iexact=signup_email, defaults={
                    "email": signup_email, "password": make_password(None), "email_verified_at": now,
                })
                locked_user = User.objects.select_for_update().get(pk=user.pk)
                if not is_email_code_eligible(locked_user):
                    raise InvalidEmailCode("invalid_or_expired_code")
                if created:
                    CommunityMemberProfile.objects.create(user=locked_user)
                challenge.user = locked_user
            challenge.encrypted_signup_email = ""
            challenge.consumed_at = now
            challenge.save(update_fields=("consumed_at", "user", "encrypted_signup_email"))
            user = locked_user
            if user.email_verified_at is None:
                user.email_verified_at = now
                user.save(update_fields=("email_verified_at",))
            CommunityChatEmailCodeChallenge.objects.filter(
                user=user,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
            ).exclude(id=challenge.id).update(invalidated_at=now, encrypted_signup_email="")
            return user, challenge
    if invalid:
        raise InvalidEmailCode("invalid_or_expired_code")
