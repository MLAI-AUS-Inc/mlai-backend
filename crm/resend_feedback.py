"""Signed Resend delivery feedback, durable deduplication and immediate suppression."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta
from email.utils import parseaddr

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BlastDelivery, Channel, Contact, CrmAuditEvent, SenderMailbox

logger = logging.getLogger(__name__)
EVENTS = {"email.sent", "email.delivered", "email.bounced", "email.complained",
          "email.failed", "email.delivery_delayed", "email.suppressed"}
BAD_EVENTS = {"email.bounced", "email.complained", "email.failed", "email.suppressed"}
MAX_BODY = 128 * 1024


def monitored_senders():
    return tuple(getattr(settings, "CRM_RESEND_MONITORED_SENDERS", ()))


def verify_signature(body, headers, secret, *, now=None):
    """Verify Svix's exact raw bytes, constant-time HMAC and five-minute timestamp."""
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


def parse_event(payload):
    """Allow only bounded, single-recipient feedback for the configured CRM senders."""
    if not isinstance(payload, dict) or payload.get("type") not in EVENTS:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("invalid_data")
    sender = parseaddr(str(data.get("from", "")))[1].strip().lower()
    if sender not in monitored_senders():
        return None
    recipients = data.get("to")
    if not isinstance(recipients, list) or len(recipients) != 1 or not isinstance(recipients[0], str):
        raise ValueError("invalid_recipients")
    recipient = parseaddr(recipients[0])[1].strip().lower()
    if not recipient or len(recipient) > 254:
        raise ValueError("invalid_recipient")
    email_id = str(uuid.UUID(str(data.get("email_id", ""))))
    subject = data.get("subject", "")
    if not isinstance(subject, str) or len(subject) > 1000:
        raise ValueError("invalid_subject")
    return {"type": payload["type"], "sender": sender, "recipient": recipient,
            "email_id": email_id, "subject": subject,
            "test": recipient.rsplit("@", 1)[-1] == "resend.dev"}


def next_delivery_state(status, error, event_type):
    """Terminal failures/complaints cannot be erased by late sent/delivered events."""
    S = BlastDelivery.Status
    if event_type == "email.complained":
        return S.UNSUBSCRIBED, "resend_complained"
    if error == "resend_complained":
        return status, error
    if event_type == "email.bounced":
        return S.BOUNCED, "resend_bounced"
    if status in (S.BOUNCED, S.UNSUBSCRIBED, S.FAILED, S.UNDELIVERED):
        return status, error
    if event_type in ("email.failed", "email.suppressed"):
        return S.FAILED, event_type.replace("email.", "resend_")
    if event_type == "email.delivered":
        return S.DELIVERED, ""
    if status in (S.DELIVERED, S.READ):
        return status, error
    if event_type == "email.delivery_delayed":
        return S.SENT, "resend_delivery_delayed"
    return S.SENT, error


def pause_senders(reason, *, now, event_id=""):
    """Stop future starts on both warming senders; never silently re-enable them."""
    enabled = SenderMailbox.objects.filter(email__in=monitored_senders(), enabled=True)
    count = enabled.update(enabled=False, updated_at=now)
    if count:
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.EDIT, target_type="resend_monitor",
            detail={"operation": "paused", "reason": reason, "mailboxes": count, "event_id": event_id},
        )


def record_event(event, message_id, *, now=None):
    """Serialize on sender rows; use the existing append-only audit as the event receipt."""
    from .listmonk_sync import record_email_opt_out
    now = now or timezone.now()
    receipt = hashlib.sha256(message_id.encode()).hexdigest()
    with transaction.atomic():
        # Same lock order in warm-up queueing and campaign start prevents races.
        locked = list(SenderMailbox.objects.select_for_update().filter(
            email__in=monitored_senders()).order_by("pk"))
        if event["sender"] not in {mailbox.email for mailbox in locked}:
            return {"status": "ignored"}
        duplicate = CrmAuditEvent.objects.filter(target_type="resend_event", target_id=receipt).exists()
        if not duplicate:
            rows = BlastDelivery.objects.select_for_update().filter(channel=Channel.EMAIL, provider_id=event["email_id"])
            delivery = rows.first()
            if delivery is None:
                candidates = list(BlastDelivery.objects.select_for_update().filter(
                    channel=Channel.EMAIL, address=event["recipient"], blast__mailbox__email=event["sender"],
                    blast__subject=event["subject"], created_at__gte=now - timedelta(days=7),
                    provider_id="",
                ).exclude(status=BlastDelivery.Status.QUEUED).order_by("-pk")[:2])
                delivery = candidates[0] if len(candidates) == 1 else None
            if delivery and (delivery.address != event["recipient"] or delivery.blast.mailbox.email != event["sender"]):
                delivery = None
            if delivery:
                delivery.provider_id = event["email_id"]
                delivery.status, delivery.error_code = next_delivery_state(
                    delivery.status, delivery.error_code, event["type"])
                if event["type"] == "email.delivered" and delivery.delivered_at is None:
                    delivery.delivered_at = now
                delivery.save(update_fields=["provider_id", "status", "error_code", "delivered_at", "updated_at"])
            kind = {"email.bounced": "bounced", "email.complained": "complained",
                    "email.suppressed": "bounced"}.get(event["type"])
            if kind:
                record_email_opt_out(event["recipient"], kind, source="resend:webhook", now=now,
                                     detail={"email_id": event["email_id"], "event": event["type"]})
            if event["type"] in BAD_EVENTS and not event["test"]:
                pause_senders(event["type"], now=now, event_id=receipt)
            CrmAuditEvent.objects.create(
                action=CrmAuditEvent.Action.SYNC, target_type="resend_event", target_id=receipt,
                detail={"event": event["type"], "email_id": event["email_id"],
                        "sender": event["sender"], "test": event["test"],
                        "delivery_id": delivery.pk if delivery else None},
            )
    # Retry this on duplicate events too, so an API outage cannot lose suppression.
    if event["type"] in ("email.bounced", "email.complained", "email.suppressed"):
        from . import listmonk
        contact = Contact.objects.filter(email=event["recipient"]).first()
        if contact and contact.listmonk_subscriber_id:
            listmonk.blocklist_subscribers([contact.listmonk_subscriber_id])
    return {"status": "duplicate" if duplicate else "recorded"}


class ResendWebhookView(APIView):
    """Resend authenticates via Svix; failures return non-2xx for provider retry."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = []

    def post(self, request):
        secret = getattr(settings, "CRM_RESEND_WEBHOOK_SECRET", "")
        if not secret or not monitored_senders():
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
        except Exception as exc:
            logger.error("CRM Resend feedback deferred: %s", type(exc).__name__)
            return Response({"error": "temporarily_unavailable"}, status=503)
