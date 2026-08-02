import base64
import hashlib
import logging
import math
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from customerio import APIClient
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import CommunityChatEmailCodeDelivery, EmailCodeDeliveryStatus


logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_LEASE_SECONDS = 120


def _cipher():
    secret = str(settings.COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET).encode("utf-8")
    digest = hashlib.sha256(b"mlai-community-chat-email-code-v1\0" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_email_code(code):
    return _cipher().encrypt(str(code).encode("ascii")).decode("ascii")


def _decrypt_email_code(encrypted):
    return _cipher().decrypt(str(encrypted).encode("ascii")).decode("ascii")


def send_community_chat_email_code(user, code):
    """Send one code through the dedicated Customer.io transaction."""

    api_key = str(settings.CUSTOMERIO_API_KEY).strip()
    message_id = str(settings.CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID).strip()
    if not api_key or not message_id:
        raise RuntimeError("community_chat_email_provider_unconfigured")

    display_name = user.full_name or user.first_name or "MLAI member"
    request_body = {
        "transactional_message_id": message_id,
        "message_data": {
            "verification_code": code,
            "expires_minutes": max(
                1,
                math.ceil(settings.COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS / 60),
            ),
            "first_name": user.first_name or display_name,
            "full_name": display_name,
            "product_name": "MLAI Chat",
            "support_url": "https://mlai.au/support",
        },
        "to": user.email,
        "identifiers": {"id": str(user.id)},
    }
    return APIClient(api_key).send_email(request_body)


def _lock(queryset):
    if connection.features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update()


def claim_email_code_delivery():
    """Lease one due outbox row, including an abandoned send lease."""

    now = timezone.now()
    stale_claim = now - timedelta(seconds=DELIVERY_LEASE_SECONDS)
    with transaction.atomic():
        delivery = (
            _lock(
                CommunityChatEmailCodeDelivery.objects.filter(
                    Q(
                        status=EmailCodeDeliveryStatus.PENDING,
                        available_at__lte=now,
                    )
                    | Q(
                        status=EmailCodeDeliveryStatus.SENDING,
                        claimed_at__lt=stale_claim,
                    )
                )
            )
            .order_by("available_at", "created_at")
            .first()
        )
        if delivery is None:
            return None
        delivery.status = EmailCodeDeliveryStatus.SENDING
        delivery.claimed_at = now
        delivery.attempts += 1
        delivery.save(
            update_fields=("status", "claimed_at", "attempts", "updated_at")
        )
        return delivery.id


def _provider_delivery_id(response):
    if isinstance(response, dict):
        for key in ("delivery_id", "id", "message_id"):
            value = response.get(key)
            if value:
                return str(value)[:255]
    return ""


def _cancel_delivery(delivery, now):
    CommunityChatEmailCodeDelivery.objects.filter(id=delivery.id).update(
        status=EmailCodeDeliveryStatus.CANCELLED,
        encrypted_code="",
        claimed_at=None,
        provider_delivery_id="",
        last_error_code="",
        updated_at=now,
    )
    return "cancelled"


def deliver_email_code(delivery_id):
    """Deliver one leased row without logging its address or code."""

    delivery = CommunityChatEmailCodeDelivery.objects.select_related(
        "challenge__user"
    ).get(id=delivery_id)
    challenge = delivery.challenge
    now = timezone.now()
    if (
        challenge.user_id is None
        or challenge.consumed_at is not None
        or challenge.invalidated_at is not None
        or challenge.expires_at <= now
    ):
        return _cancel_delivery(delivery, now)

    try:
        code = _decrypt_email_code(delivery.encrypted_code)
        response = send_community_chat_email_code(challenge.user, code)
    except InvalidToken:
        logger.error(
            "Chat email-code delivery payload could not be decrypted delivery_id=%s",
            delivery.id,
        )
        CommunityChatEmailCodeDelivery.objects.filter(id=delivery.id).update(
            status=EmailCodeDeliveryStatus.FAILED,
            encrypted_code="",
            claimed_at=None,
            last_error_code="InvalidToken",
            updated_at=now,
        )
        return "failed"
    except Exception as exc:
        final_attempt = delivery.attempts >= MAX_DELIVERY_ATTEMPTS
        delay_seconds = min(300, 2 ** max(0, delivery.attempts - 1))
        CommunityChatEmailCodeDelivery.objects.filter(id=delivery.id).update(
            status=(
                EmailCodeDeliveryStatus.FAILED
                if final_attempt
                else EmailCodeDeliveryStatus.PENDING
            ),
            encrypted_code="" if final_attempt else delivery.encrypted_code,
            available_at=now + timedelta(seconds=delay_seconds),
            claimed_at=None,
            last_error_code=exc.__class__.__name__[:120],
            updated_at=now,
        )
        logger.warning(
            "Chat email-code delivery failed delivery_id=%s attempt=%s error_type=%s",
            delivery.id,
            delivery.attempts,
            exc.__class__.__name__,
        )
        return "failed" if final_attempt else "retry"

    CommunityChatEmailCodeDelivery.objects.filter(id=delivery.id).update(
        status=EmailCodeDeliveryStatus.SENT,
        encrypted_code="",
        claimed_at=None,
        sent_at=now,
        provider_delivery_id=_provider_delivery_id(response),
        last_error_code="",
        updated_at=now,
    )
    return "sent"


def process_email_code_deliveries(*, limit=25):
    counts = {"sent": 0, "retry": 0, "failed": 0, "cancelled": 0}
    for _ in range(max(0, limit)):
        delivery_id = claim_email_code_delivery()
        if delivery_id is None:
            break
        result = deliver_email_code(delivery_id)
        counts[result] += 1
    return counts
