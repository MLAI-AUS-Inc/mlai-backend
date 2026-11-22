"""Send a completion receipt while keeping the former member's address short-lived."""

from datetime import timedelta
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from integrations.fields import (
    CredentialEncryptionError,
    decrypt_credential_value,
    encrypt_credential_value,
)
from system_email import transport_resend
from system_email.models import SystemEmailSuppression
from system_email.render import RenderedEmail

from .deletion_tasks import deletion_deadline
from .models import AccountDeletionCompletionNotice, AccountDeletionRequest


Status = AccountDeletionCompletionNotice.Status
MESSAGE_KEY = "chat_deletion_complete"
DELIVERY_LEASE = timedelta(minutes=2)
# Resend deduplicates for 24 hours. Leave room for clock skew and transport.
RETRY_WINDOW = timedelta(hours=20)
MAX_ATTEMPTS = 6


def capture_recipient(request, address):
    """Capture the current sign-in address inside the request transaction."""
    address = address.strip().lower()
    try:
        validate_email(address)
        recipient = encrypt_credential_value(json.dumps([str(request.pk), address]))
    except (ValidationError, ImproperlyConfigured):
        # A missing or unusable email cannot prevent the member's erasure.
        AccountDeletionCompletionNotice.objects.create(
            request=request, encrypted_recipient="", expires_at=deletion_deadline(request),
            status=Status.NEEDS_ATTENTION, failure_code="recipient_unavailable",
        )
        return
    AccountDeletionCompletionNotice.objects.create(
        request=request, encrypted_recipient=recipient, expires_at=deletion_deadline(request),
    )


def _recipient(notice):
    case_id, address = json.loads(decrypt_credential_value(notice.encrypted_recipient))
    if case_id != str(notice.request_id):
        raise ValueError("Completion notice belongs to another request")
    validate_email(address)
    return address


def _message(request_id, scope, *, contact):
    description = (
        "MLAI Chat data" if scope == AccountDeletionRequest.Scope.CHAT else "shared MLAI account"
    )
    return RenderedEmail(
        subject="Your MLAI deletion request is complete",
        html="",
        text=(
            f"We completed your deletion request for your {description}.\n\n"
            f"Receipt reference: {request_id}\n\n"
            f"For questions about what was deleted or retained, contact {contact}.\n"
        ),
    )


def expire_notices(now, *, limit):
    """Forget expired recipient data, even while delivery is paused."""
    expired = AccountDeletionCompletionNotice.objects.filter(status=Status.PENDING).filter(
        Q(expires_at__lte=now)
        | Q(first_attempt_at__lte=now - RETRY_WINDOW, next_attempt_at__lte=now)
        | Q(attempts__gte=MAX_ATTEMPTS, next_attempt_at__lte=now),
    )
    ids = list(expired.order_by("expires_at", "pk").values_list("pk", flat=True)[:limit])
    return expired.filter(pk__in=ids).update(
        status=Status.NEEDS_ATTENTION, encrypted_recipient="",
        failure_code="delivery_window_closed", next_attempt_at=None,
    )


@transaction.atomic
def claim_notice(now):
    """Lease one completed case, using the attempt count to fence stale sends."""
    ready = AccountDeletionCompletionNotice.objects.filter(
        request__status=AccountDeletionRequest.Status.COMPLETED,
        request__completed_at__isnull=False,
        status=Status.PENDING,
        expires_at__gt=now + DELIVERY_LEASE,
        attempts__lt=MAX_ATTEMPTS,
    ).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
    ).filter(
        Q(first_attempt_at__isnull=True) | Q(first_attempt_at__gt=now - RETRY_WINDOW),
    )
    lock_options = {"skip_locked": True} if connection.features.has_select_for_update_skip_locked else {}
    notice = ready.select_for_update(**lock_options).order_by("expires_at", "pk").first()
    if notice is None:
        return None
    notice.first_attempt_at = notice.first_attempt_at or now
    notice.attempts += 1
    notice.next_attempt_at = now + DELIVERY_LEASE
    notice.save(update_fields=("first_attempt_at", "attempts", "next_attempt_at"))
    return notice


def _finish(notice, *, status, failure="", retry_at=None, accepted_at=None):
    return AccountDeletionCompletionNotice.objects.filter(
        pk=notice.pk, status=Status.PENDING, attempts=notice.attempts,
    ).update(
        status=status, failure_code=failure,
        encrypted_recipient=notice.encrypted_recipient if status == Status.PENDING else "",
        next_attempt_at=retry_at, accepted_at=accepted_at,
    ) == 1


def deliver_notice(notice):
    """On terminal outcomes clear the address from the outbox atomically."""
    try:
        address = _recipient(notice)
    except (CredentialEncryptionError, ImproperlyConfigured, ValidationError, ValueError, TypeError):
        return _finish(notice, status=Status.NEEDS_ATTENTION, failure="recipient_unavailable")

    now = timezone.now()
    if not AccountDeletionCompletionNotice.objects.filter(
        pk=notice.pk, status=Status.PENDING, attempts=notice.attempts,
        expires_at__gt=now, next_attempt_at__gt=now,
        first_attempt_at__gt=now - RETRY_WINDOW,
    ).exists():
        return False

    request = notice.request
    current_users = get_user_model().objects.filter(email__iexact=address)
    if request.scope == AccountDeletionRequest.Scope.CHAT:
        recipient_is_valid = current_users.filter(pk=request.user_id).exists()
    else:
        # A new account may have registered with the old address after erasure.
        recipient_is_valid = request.user_id is None and not current_users.exists()
    if not recipient_is_valid:
        return _finish(notice, status=Status.NEEDS_ATTENTION, failure="recipient_changed")
    if SystemEmailSuppression.objects.filter(email=address, cleared_at__isnull=True).exists():
        return _finish(notice, status=Status.NEEDS_ATTENTION, failure="recipient_suppressed")

    contact = settings.COMMUNITY_CHAT_DELETION_CONTACT or "hi@mlai.au"
    rendered = _message(request.pk, request.scope, contact=contact)
    payload = transport_resend.build_payload(
        to=address, rendered=rendered, message_key=MESSAGE_KEY, entity_ref=str(notice.pk),
    )
    try:
        transport_resend.send(payload, idempotency_key=f"{MESSAGE_KEY}/{notice.pk}")
    except transport_resend.SystemEmailRetryable:
        next_attempt = timezone.now() + timedelta(minutes=2 ** notice.attempts)
        if (
            notice.attempts < MAX_ATTEMPTS
            and next_attempt + DELIVERY_LEASE < min(notice.expires_at, notice.first_attempt_at + RETRY_WINDOW)
        ):
            return _finish(notice, status=Status.PENDING, failure="provider_retry", retry_at=next_attempt)
        return _finish(notice, status=Status.NEEDS_ATTENTION, failure="delivery_window_closed")
    except transport_resend.SystemEmailRejected:
        return _finish(notice, status=Status.NEEDS_ATTENTION, failure="provider_rejected")
    return _finish(notice, status=Status.ACCEPTED, accepted_at=timezone.now())
