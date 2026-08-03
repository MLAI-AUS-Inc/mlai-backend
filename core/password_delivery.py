import base64
import hashlib
import logging
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .email_utils import send_password_reset_email
from .models import PasswordResetEmailDelivery, PasswordResetDeliveryStatus


logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_LEASE_SECONDS = 120


def _cipher():
    secret = str(settings.PASSWORD_RESET_DELIVERY_SECRET).encode('utf-8')
    digest = hashlib.sha256(b'mlai-password-reset-delivery-v1\0' + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_reset_link(reset_link):
    return _cipher().encrypt(str(reset_link).encode('utf-8')).decode('ascii')


def _decrypt_reset_link(encrypted):
    return _cipher().decrypt(str(encrypted).encode('ascii')).decode('utf-8')


def _lock(queryset):
    if connection.features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update()


def claim_password_reset_delivery():
    """Lease one due outbox row for a worker, reclaiming abandoned sends."""

    now = timezone.now()
    stale_claim = now - timedelta(seconds=DELIVERY_LEASE_SECONDS)
    with transaction.atomic():
        delivery = (
            _lock(
                PasswordResetEmailDelivery.objects.filter(
                    Q(
                        status=PasswordResetDeliveryStatus.PENDING,
                        available_at__lte=now,
                    )
                    | Q(
                        status=PasswordResetDeliveryStatus.SENDING,
                        claimed_at__lt=stale_claim,
                    )
                )
            )
            .order_by('available_at', 'created_at')
            .first()
        )
        if delivery is None:
            return None
        delivery.status = PasswordResetDeliveryStatus.SENDING
        delivery.claimed_at = now
        delivery.attempts += 1
        delivery.save(
            update_fields=('status', 'claimed_at', 'attempts', 'updated_at')
        )
        return delivery.id


def deliver_password_reset_email(delivery_id):
    """Deliver one leased row without ever logging its address or reset link."""

    delivery = PasswordResetEmailDelivery.objects.select_related(
        'challenge__user'
    ).get(id=delivery_id)
    now = timezone.now()
    challenge = delivery.challenge
    if challenge.consumed_at is not None or challenge.expires_at <= now:
        PasswordResetEmailDelivery.objects.filter(id=delivery.id).update(
            status=PasswordResetDeliveryStatus.CANCELLED,
            encrypted_reset_link='',
            claimed_at=None,
            last_error_code='',
            updated_at=now,
        )
        return 'cancelled'

    try:
        reset_link = _decrypt_reset_link(delivery.encrypted_reset_link)
        send_password_reset_email(challenge.user, reset_link)
    except InvalidToken:
        logger.error(
            'Password reset delivery payload could not be decrypted delivery_id=%s',
            delivery.id,
        )
        PasswordResetEmailDelivery.objects.filter(id=delivery.id).update(
            status=PasswordResetDeliveryStatus.FAILED,
            encrypted_reset_link='',
            claimed_at=None,
            last_error_code='InvalidToken',
            updated_at=now,
        )
        return 'failed'
    except Exception as exc:
        final_attempt = delivery.attempts >= MAX_DELIVERY_ATTEMPTS
        delay_seconds = min(300, 2 ** max(0, delivery.attempts - 1))
        PasswordResetEmailDelivery.objects.filter(id=delivery.id).update(
            status=(
                PasswordResetDeliveryStatus.FAILED
                if final_attempt
                else PasswordResetDeliveryStatus.PENDING
            ),
            encrypted_reset_link=(
                '' if final_attempt else delivery.encrypted_reset_link
            ),
            available_at=now + timedelta(seconds=delay_seconds),
            claimed_at=None,
            last_error_code=exc.__class__.__name__[:120],
            updated_at=now,
        )
        logger.warning(
            'Password reset delivery failed delivery_id=%s attempt=%s error_type=%s',
            delivery.id,
            delivery.attempts,
            exc.__class__.__name__,
        )
        return 'failed' if final_attempt else 'retry'

    PasswordResetEmailDelivery.objects.filter(id=delivery.id).update(
        status=PasswordResetDeliveryStatus.SENT,
        encrypted_reset_link='',
        claimed_at=None,
        sent_at=now,
        last_error_code='',
        updated_at=now,
    )
    return 'sent'


def process_password_reset_deliveries(*, limit=25):
    counts = {'sent': 0, 'retry': 0, 'failed': 0, 'cancelled': 0}
    for _ in range(max(0, limit)):
        delivery_id = claim_password_reset_delivery()
        if delivery_id is None:
            break
        result = deliver_password_reset_email(delivery_id)
        counts[result] += 1
    return counts
