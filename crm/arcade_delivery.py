"""Deliver reserved Codex codes through the transactional Resend sender and audit in CRM."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from community_chat.arcade_inventory import CODEX_PREFIX, KINDS, ORIGIN, can_email_winner
from community_chat.volunteer.models import VolunteerSourceReceipt as Receipt
from crm.models import CrmAuditEvent
from integrations.fields import CredentialEncryptionError, decrypt_credential_value
from system_email import api, transport_resend
from system_email.models import SystemEmail
from system_email.transport_resend import SystemEmailRejected, SystemEmailRetryable

logger = logging.getLogger(__name__)
MESSAGE = "arcade_codex_prize"
RETRY_WINDOW = timedelta(hours=23)


def _record(stock, status, reason=""):
    stock.status, stock.error = status, reason
    stock.save(update_fields=("status", "error", "updated_at"))
    CrmAuditEvent.objects.create(
        action="send", target_type="arcade_codex_delivery", target_id=stock.source["roundId"],
        detail={"reservation": str(stock.pk), "state": status, "provider": "resend", "reason": reason},
    )


def _pending():
    return Receipt.objects.filter(origin=ORIGIN, kind=KINDS["codex"], status="email_pending")


def _deliver(pk, now):
    # Each attempt gets a persisted retry time before making a network request.
    with transaction.atomic():
        stock = _pending().select_for_update().filter(pk=pk, occurred_at__lte=now).first()
        if not stock:
            return
        key = "arcade-codex/" + str(stock.pk)
        # A worker can die after Resend accepted and logged the email but before
        # recording its CRM receipt. An existing provider ID settles that case.
        if SystemEmail.objects.filter(idempotency_key=key).exclude(provider_message_id="").exists():
            _record(stock, "sent")
            return
        # A scheduler tick can span multiple provider calls and wait on a row
        # lock. Take a fresh time after the lock before dispatching this code.
        expires = parse_datetime(stock.source.get("expiresAt") or "")
        if expires and expires <= max(now, timezone.now()):
            _record(stock, "needs_review", "code_expired_before_dispatch")
            return
        first = parse_datetime(stock.metadata.get("firstAttempt", "")) or now
        if now - first >= RETRY_WINDOW:
            _record(stock, "needs_review", "provider_receipt_window")
            return
        if not (transport_resend.api_key() and transport_resend.from_address()):
            stock.occurred_at = now + timedelta(minutes=10)
            stock.save(update_fields=("occurred_at", "updated_at"))
            return
        try:
            email = decrypt_credential_value(stock.metadata["recipient"])
            code = decrypt_credential_value(stock.metadata["code"])
        except (CredentialEncryptionError, KeyError):
            _record(stock, "needs_review", "stock_unreadable")
            return
        if not stock.target or stock.target.email != email or not can_email_winner(stock.target, check_sender=False):
            _record(stock, "needs_review", "recipient_unavailable")
            return
        attempt = int(stock.metadata.get("attempts", 0)) + 1
        stock.metadata = {**stock.metadata, "firstAttempt": first.isoformat(), "attempts": attempt}
        stock.occurred_at = now + timedelta(minutes=min(60, 2 ** min(attempt, 6)))
        stock.save(update_fields=("metadata", "occurred_at", "updated_at"))
        context = {"activation_code": code, "activation_url": CODEX_PREFIX + code}
        if expires:
            context["expiry_display"] = expires.astimezone(ZoneInfo("Australia/Melbourne")).strftime(
                "%-d %B %Y at %-I:%M %p %Z"
            )

    try:
        api.send(MESSAGE, to=email, user_id=stock.target_id, idempotency_key=key, context=context)
    except SystemEmailRetryable:
        return
    except SystemEmailRejected:
        status, reason = "needs_review", "resend_rejected"
    else:
        status, reason = "sent", ""
    with transaction.atomic():
        current = _pending().select_for_update().filter(pk=pk).first()
        if current and (status == "sent" or current.metadata.get("attempts") == attempt):
            _record(current, status, reason)


def run_arcade_prize_delivery():
    """A bounded scheduler sweep also recovers crashes between awarding and dispatch."""
    now = timezone.now()
    pending = list(_pending().filter(occurred_at__lte=now).order_by("occurred_at", "pk").values_list("pk", flat=True)[:2])
    failed = 0
    for pk in pending:
        try:
            _deliver(pk, now)
        except Exception as exc:
            # Never attach a transport traceback containing bearer codes or the mail body.
            logger.warning("Arcade prize delivery deferred reservation=%s error_type=%s", pk, type(exc).__name__)
            failed += 1
    return {"status": "completed", "processed": len(pending), "failed": failed}
