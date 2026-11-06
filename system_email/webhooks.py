"""Resend delivery feedback for transactional email only.

Mounted at ``/api/v1/system-email/webhooks/resend/``. It only reads events for
messages sent from the transactional From domain, and it never touches CRM
senders. The CRM webhook, in turn, ignores this domain because it is not in
``CRM_RESEND_MONITORED_SENDERS``. Feedback here alerts; it never pauses sending.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import time
from datetime import datetime
from email.utils import parseaddr

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)
MAX_BODY = 128 * 1024
STATUS_FOR_EVENT = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.delivery_delayed": "delayed",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.failed": "failed",
    "email.suppressed": "suppressed",
}
# Later states win over earlier ones; a late "sent" never hides a bounce.
RANK = {"sending": 0, "sent": 1, "delayed": 2, "delivered": 3, "failed": 4,
        "suppressed": 5, "bounced": 6, "complained": 7}


def verify_signature(body: bytes, headers, secret: str, *, now: float | None = None) -> bool:
    """Svix signature over the exact raw body, constant-time, five-minute window."""
    try:
        message_id = headers.get("svix-id", "")
        stamp = headers.get("svix-timestamp", "")
        signatures = headers.get("svix-signature", "")
        if (not secret.startswith("whsec_") or not message_id or len(message_id) > 255
                or not stamp.isdigit() or len(stamp) > 12 or len(signatures) > 2048
                or len(body) > MAX_BODY):
            return False
        if abs((time.time() if now is None else now) - int(stamp)) > 300:
            return False
        key = base64.b64decode(secret[6:], validate=True)
        if len(key) < 16:
            return False
        signed = message_id.encode("ascii") + b"." + stamp.encode("ascii") + b"." + body
        expected = base64.b64encode(hmac.digest(key, signed, "sha256")).decode("ascii")
        return any(hmac.compare_digest(signature[3:], expected)
                   for signature in signatures.split() if signature.startswith("v1,"))
    except (ValueError, UnicodeError, binascii.Error, AttributeError, TypeError):
        return False


def system_from_domain() -> str:
    _, address = parseaddr(str(getattr(settings, "MLAI_SYSTEM_FROM_EMAIL", "") or ""))
    return address.rpartition("@")[2].lower()


def parse_event(payload) -> dict | None:
    """Return a small event dict, or None for anything not ours or not understood."""
    if not isinstance(payload, dict) or payload.get("type") not in STATUS_FOR_EVENT:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    _, sender = parseaddr(str(data.get("from") or ""))
    domain = system_from_domain()
    if not domain or sender.rpartition("@")[2].lower() != domain:
        return None
    email_id = str(data.get("email_id") or "").strip()
    recipients = data.get("to")
    if not email_id or not isinstance(recipients, list) or len(recipients) != 1:
        return None
    bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
    created = parse_datetime(str(payload.get("created_at") or "")) or None
    return {
        "type": payload["type"],
        "email_id": email_id[:255],
        "recipient": str(recipients[0]).strip().lower()[:320],
        "bounce_type": str(bounce.get("type") or "").lower(),
        "at": created if isinstance(created, datetime) else None,
    }


def _hard_bounce(event) -> bool:
    # Resend reports "Permanent" for hard bounces and "Transient"/"Undetermined" otherwise.
    return event["type"] == "email.bounced" and event["bounce_type"] in {"permanent", "hard"}


def record_event(event: dict, svix_id: str) -> dict:
    """Apply one event idempotently. Returns {"status": "recorded"|"duplicate"|"unknown_message"}."""
    from .models import SystemEmail, SystemEmailSuppression, SystemEmailWebhookEvent

    now = timezone.now()
    at = event["at"] or now
    try:
        with transaction.atomic():
            SystemEmailWebhookEvent.objects.create(
                svix_id=svix_id[:255], event_type=event["type"], provider_message_id=event["email_id"],
            )
            row = SystemEmail.objects.select_for_update().filter(provider_message_id=event["email_id"]).first()
            if row is not None:
                new_status = STATUS_FOR_EVENT[event["type"]]
                fields = ["last_event_at", "updated_at"]
                row.last_event_at = at
                if RANK.get(new_status, 0) >= RANK.get(row.status, 0):
                    row.status = new_status
                    fields.append("status")
                if new_status == "delivered" and row.delivered_at is None:
                    row.delivered_at = at
                    fields.append("delivered_at")
                row.save(update_fields=fields)
            if _hard_bounce(event) or event["type"] == "email.complained":
                SystemEmailSuppression.objects.update_or_create(
                    email=event["recipient"],
                    defaults={
                        "reason": "complaint" if event["type"] == "email.complained" else "hard_bounce",
                        "source_event_id": svix_id[:255],
                        "cleared_at": None,
                        "cleared_by": "",
                    },
                )
    except IntegrityError:
        return {"status": "duplicate"}
    if row is None:
        return {"status": "unknown_message"}
    if event["type"] in {"email.bounced", "email.complained", "email.failed", "email.suppressed"}:
        logger.warning("System email feedback message=%s log_id=%s event=%s", row.message_key, row.id, event["type"])
    return {"status": "recorded"}


class SystemEmailResendWebhookView(APIView):
    """Resend authenticates with Svix; non-2xx responses make Resend retry."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = []

    def post(self, request):
        secret = str(getattr(settings, "SYSTEM_EMAIL_RESEND_WEBHOOK_SECRET", "") or "").strip()
        if not secret or not system_from_domain():
            return Response({"error": "not_configured"}, status=503)
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY:
            return Response({"error": "payload_too_large"}, status=413)
        body = request.body
        if not verify_signature(body, request.headers, secret):
            return Response({"error": "invalid_signature"}, status=403)
        try:
            event = parse_event(json.loads(body))
        except (ValueError, TypeError):
            return Response({"error": "invalid_event"}, status=400)
        if event is None:
            return Response({"status": "ignored"})
        try:
            return Response(record_event(event, request.headers["svix-id"]))
        except Exception as exc:  # retried by Resend
            logger.error("System email feedback deferred: %s", type(exc).__name__)
            return Response({"error": "temporarily_unavailable"}, status=503)
