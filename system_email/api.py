"""Public entry points: ``routes_to_resend`` and ``send``.

Callers keep their own outboxes and retries. ``send`` renders, checks
suppression, records a ``SystemEmail`` row and calls Resend once. It raises
``SystemEmailRetryable`` or ``SystemEmailRejected``; the caller's outbox
decides what a retry means.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import transport_resend
from .registry import AUTH, get_spec
from .render import RenderedEmail, render
from .transport_resend import SystemEmailRejected

logger = logging.getLogger(__name__)
PROVIDER = "resend"


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    log_id: str
    duplicate: bool = False


def resend_message_keys() -> frozenset[str]:
    raw = getattr(settings, "SYSTEM_EMAIL_RESEND_MESSAGES", ()) or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    return frozenset(str(value).strip() for value in raw if str(value).strip())


def routes_to_resend(message_key: str) -> bool:
    """True when ``message_key`` is switched over to our Resend sender.

    Unlisted keys stay on the caller's legacy provider. ``all`` moves every key.
    """
    get_spec(message_key)
    keys = resend_message_keys()
    return "all" in keys or message_key in keys


def _normalise(address: str) -> str:
    return str(address or "").strip().lower()


def _suppressed(address: str) -> bool:
    from .models import SystemEmailSuppression

    return SystemEmailSuppression.objects.filter(email=address, cleared_at__isnull=True).exists()


def send(
    message_key: str,
    *,
    to: str,
    idempotency_key: str,
    context: Optional[Mapping[str, Any]] = None,
    prerendered: Optional[RenderedEmail] = None,
    user_id: Optional[int] = None,
    unsubscribe_url: str = "",
) -> SendResult:
    """Send one registered message through Resend and return its id.

    The same ``idempotency_key`` always maps to the same log row and the same
    Resend idempotency key, so a caller retrying after a timeout can't send twice.
    """
    from .models import SystemEmail, SystemEmailStatus

    spec = get_spec(message_key)
    address = _normalise(to)
    if "@" not in address:
        raise SystemEmailRejected("invalid_recipient")
    rendered = render(spec, context, prerendered)

    with transaction.atomic():
        row, _created = SystemEmail.objects.select_for_update().get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "message_key": spec.key,
                "category": spec.category,
                "provider": PROVIDER,
                "to_email": address,
                "user_id": user_id,
            },
        )
        if row.message_key != spec.key or row.to_email != address:
            raise SystemEmailRejected("idempotency_key_reused")
        if row.provider_message_id and row.status != SystemEmailStatus.FAILED:
            return SendResult(row.provider_message_id, str(row.id), duplicate=True)
        row.attempts += 1
        row.status = SystemEmailStatus.SENDING
        row.save(update_fields=("attempts", "status", "updated_at"))

    if _suppressed(address) and spec.category != AUTH:
        # Auth mail is still attempted: the provider refuses a suppressed
        # address itself, and support then sees a clear failure for that person.
        SystemEmail.objects.filter(pk=row.pk).update(
            status=SystemEmailStatus.SUPPRESSED, error_code="recipient_suppressed", updated_at=timezone.now()
        )
        raise SystemEmailRejected("recipient_suppressed")

    payload = transport_resend.build_payload(
        to=address,
        rendered=rendered,
        message_key=spec.key,
        entity_ref=str(row.id),
        unsubscribe=spec.unsubscribe,
        unsubscribe_url=unsubscribe_url if spec.unsubscribe else "",
    )
    try:
        provider_id = transport_resend.send(payload, idempotency_key=f"system-email/{idempotency_key}"[:256])
    except transport_resend.SystemEmailError as exc:
        SystemEmail.objects.filter(pk=row.pk).update(
            status=SystemEmailStatus.SENDING if exc.retryable else SystemEmailStatus.FAILED,
            error_code=exc.code,
            updated_at=timezone.now(),
        )
        logger.warning("System email not sent message=%s log_id=%s error=%s", spec.key, row.id, exc.code)
        raise
    now = timezone.now()
    SystemEmail.objects.filter(pk=row.pk).update(
        status=SystemEmailStatus.SENT, provider_message_id=provider_id, error_code="", sent_at=now, updated_at=now,
    )
    logger.info("System email sent message=%s log_id=%s", spec.key, row.id)
    return SendResult(provider_id, str(row.id))
