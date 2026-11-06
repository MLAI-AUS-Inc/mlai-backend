"""Signed, replay-safe feedback; individual suppression never stops a stream."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import timedelta
from email.utils import parseaddr

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response
from rest_framework.views import APIView

from .deliverability import policy
from .deliverability.streams import monitored_sender
from .models import BlastDelivery, Channel, Contact, CrmAuditEvent, EmailEvent, SenderMailbox, SendingStream

logger = logging.getLogger(__name__)
EVENTS = {"email.sent", "email.delivered", "email.bounced", "email.complained",
          "email.failed", "email.delivery_delayed", "email.suppressed"}
MAX_BODY = 128 * 1024
_EMAIL = re.compile(r"[^\s<>\"'()]+@[^\s<>\"'()]+")


def monitored_senders():
    """Legacy pilot configuration; additional senders no longer restrict ingestion."""
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


def provider_for(address):
    """Known consumer providers; MX-based custom-domain classification follows in Phase 2."""
    domain = address.rsplit("@", 1)[-1]
    for provider, domains in {
        "google": {"gmail.com", "googlemail.com"},
        "microsoft": {"outlook.com", "hotmail.com", "live.com", "msn.com"},
        "yahoo": {"yahoo.com", "yahoo.com.au", "ymail.com", "aol.com"},
        "apple": {"icloud.com", "me.com", "mac.com"},
    }.items():
        if domain in domains:
            return provider
    return "other"


def diagnostic_fields(data):
    """Read documented provider fields only, masking addresses before persistence."""
    bounce, failed, suppressed = (data.get(key) or {} for key in ("bounce", "failed", "suppressed"))
    if not all(isinstance(item, dict) for item in (bounce, failed, suppressed)):
        raise ValueError("invalid_diagnostic")
    diagnostic = str(bounce.get("message") or failed.get("reason") or suppressed.get("message") or "")
    diagnostic = _EMAIL.sub("[address]", diagnostic)[:min(500, policy.value("CRM_FEEDBACK_DIAGNOSTIC_LENGTH"))]
    smtp = re.search(r"\b([245]\d{2})\b", diagnostic)
    enhanced = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", diagnostic)
    return {"bounce_type": str(bounce.get("type") or "")[:24],
            "bounce_subtype": str(bounce.get("subType") or suppressed.get("type") or "")[:64],
            "smtp_code": smtp[1] if smtp else "", "enhanced_code": enhanced[1] if enhanced else "",
            "diagnostic": diagnostic}


def parse_event(payload):
    """Allow bounded single-recipient feedback from stream, protected or legacy senders."""
    if not isinstance(payload, dict) or payload.get("type") not in EVENTS:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("invalid_data")
    sender = parseaddr(str(data.get("from", "")))[1].strip().lower()
    if not monitored_sender(sender):
        return None
    if len(sender) > 254:
        raise ValueError("invalid_sender")
    recipients = data.get("to")
    if not isinstance(recipients, list) or len(recipients) != 1 or not isinstance(recipients[0], str):
        raise ValueError("invalid_recipients")
    recipient = parseaddr(recipients[0])[1].strip().lower()
    if "@" not in recipient or len(recipient) > 254:
        raise ValueError("invalid_recipient")
    email_id = str(uuid.UUID(str(data.get("email_id", ""))))
    subject = data.get("subject", "")
    if not isinstance(subject, str) or len(subject) > 1000:
        raise ValueError("invalid_subject")
    occurred_at = None
    if payload.get("created_at"):
        occurred_at = parse_datetime(str(payload["created_at"]))
        if occurred_at is None or timezone.is_naive(occurred_at):
            raise ValueError("invalid_timestamp")
    return {"type": payload["type"], "sender": sender, "recipient": recipient,
            "email_id": email_id, "subject": subject, "occurred_at": occurred_at,
            "test": recipient.rsplit("@", 1)[-1] == "resend.dev", **diagnostic_fields(data)}


def next_delivery_state(status, error, event_type, *, bounce_type=""):
    """Complaints and permanent failures survive late delivery/sent feedback."""
    S = BlastDelivery.Status
    if event_type == "email.complained":
        return S.UNSUBSCRIBED, "resend_complained"
    if error == "resend_complained":
        return status, error
    if event_type == "email.bounced":
        if bounce_type == "Transient":
            if status in (S.BOUNCED, S.UNSUBSCRIBED, S.DELIVERED, S.READ):
                return status, error
            return S.UNDELIVERED, "soft_bounce"
        return S.BOUNCED, "resend_bounced"
    if status in (S.BOUNCED, S.UNSUBSCRIBED, S.FAILED) or (status == S.UNDELIVERED and error != "soft_bounce"):
        return status, error
    if event_type in ("email.failed", "email.suppressed"):
        return S.FAILED, event_type.replace("email.", "resend_")
    if event_type == "email.delivered":
        return S.DELIVERED, ""
    if status in (S.DELIVERED, S.READ, S.UNDELIVERED):
        return status, error
    if event_type == "email.delivery_delayed":
        return S.SENT, "resend_delivery_delayed"
    return S.SENT, error


def pause_senders(reason, *, now, event_id=""):
    """The finite legacy pilot's explicit stop; never called by feedback ingestion."""
    count = SenderMailbox.objects.filter(email__in=monitored_senders(), enabled=True).update(enabled=False, updated_at=now)
    if count:
        CrmAuditEvent.objects.create(action="edit", target_type="resend_monitor",
            detail={"operation": "paused", "reason": reason, "mailboxes": count, "event_id": event_id})


def _match_delivery(event, now):
    rows = BlastDelivery.objects.select_for_update(of=("self",)).select_related("blast__mailbox")
    delivery = rows.filter(channel=Channel.EMAIL, provider_id=event["email_id"]).first()
    if delivery is None:
        candidates = list(rows.filter(channel=Channel.EMAIL, address=event["recipient"],
            blast__mailbox__email=event["sender"], blast__subject=event["subject"], provider_id="",
        ).filter(Q(wave__started_at__gte=now - timedelta(days=policy.value("CRM_FEEDBACK_MATCH_DAYS")))
            | Q(wave__isnull=True, created_at__gte=now - timedelta(days=policy.value("CRM_FEEDBACK_MATCH_DAYS"))))
            .exclude(status=BlastDelivery.Status.QUEUED).order_by("-pk")[:2])
        delivery = candidates[0] if len(candidates) == 1 else None
    if delivery and (delivery.address != event["recipient"] or not delivery.blast.mailbox_id
                     or delivery.blast.mailbox.email != event["sender"]):
        return None
    return delivery


def _apply_contact_feedback(event, stored, delivery, now):
    from .listmonk_sync import record_email_opt_out
    # All streak changes share the contact lock; separate receipts cannot race increments.
    contact = Contact.objects.select_for_update().filter(email=event["recipient"]).first()
    if contact is None:
        return
    kind = {"email.complained": "complained", "email.suppressed": "bounced"}.get(event["type"])
    if event["type"] == "email.delivered":
        later_bounce = EmailEvent.objects.filter(recipient=event["recipient"], event_type="email.bounced",
            bounce_type="Transient", occurred_at__gt=stored.occurred_at).exists()
        if not later_bounce:
            contact.soft_bounce_streak = 0
            contact.save(update_fields=["soft_bounce_streak", "updated_at"])
    elif event["type"] == "email.bounced":
        if event.get("bounce_type") == "Transient":
            scope = {"blast_id": stored.blast_id} if stored.blast_id else {"provider_message_id": stored.provider_message_id}
            seen = EmailEvent.objects.filter(recipient=event["recipient"], event_type="email.bounced",
                bounce_type="Transient", **scope).exclude(pk=stored.pk).exists()
            later_delivery = EmailEvent.objects.filter(recipient=event["recipient"], event_type="email.delivered",
                occurred_at__gte=stored.occurred_at).exists()
            if not seen and not later_delivery:
                contact.soft_bounce_streak = min(32767, contact.soft_bounce_streak + 1)
                contact.save(update_fields=["soft_bounce_streak", "updated_at"])
            if contact.soft_bounce_streak >= policy.value("CRM_GUARD_SOFT_BOUNCE_LIMIT"):
                kind = "bounced"
                if delivery and delivery.error_code != "resend_complained":
                    delivery.status, delivery.error_code, delivery.bounce_kind = "bounced", "repeated_soft_bounce", "hard"
                    delivery.save(update_fields=["status", "error_code", "bounce_kind", "updated_at"])
        else:
            kind = "bounced"
    if kind:
        record_email_opt_out(contact.email, kind, source="resend:webhook", now=now,
                             detail={"email_id": event["email_id"], "event": event["type"]})


def record_event(event, message_id, *, now=None):
    """Atomically deduplicate the receipt and apply feedback without sender row locks."""
    if not monitored_sender(event["sender"]):
        return {"status": "ignored"}
    now = now or timezone.now()
    receipt = hashlib.sha256(message_id.encode()).hexdigest()
    with transaction.atomic():
        # Preserve replay protection for receipts recorded before EmailEvent existed.
        legacy = CrmAuditEvent.objects.filter(target_type="resend_event", target_id=receipt).exists()
        stored, created = EmailEvent.objects.get_or_create(receipt=receipt, defaults={
            "source": "resend", "event_type": event["type"], "provider_message_id": event["email_id"],
            "recipient": event["recipient"], "sender": event["sender"],
            "recipient_provider": provider_for(event["recipient"]), "occurred_at": event.get("occurred_at") or now,
            **{key: event.get(key, "") for key in ("bounce_type", "bounce_subtype", "smtp_code", "enhanced_code", "diagnostic")},
        })
        if created:
            # Dispatch locks contacts before delivery rows. Keep the same order
            # when feedback arrives while the final consent check is running.
            Contact.objects.select_for_update().filter(email=event["recipient"]).first()
            delivery = _match_delivery(event, now)
            stored.stream = SendingStream.objects.filter(domain=event["sender"].rsplit("@", 1)[-1]).first()
            if delivery:
                stored.delivery, stored.blast = delivery, delivery.blast
                stored.stream_id = delivery.blast.stream_id or (stored.stream.pk if stored.stream else None)
                stored.wave_id = delivery.wave_id
                stored.recipient_provider = delivery.provider or stored.recipient_provider
            stored.save(update_fields=["stream", "blast", "delivery", "wave", "recipient_provider"])
            if not legacy:
                if delivery:
                    delivery.provider_id = event["email_id"]
                    delivery.status, delivery.error_code = next_delivery_state(delivery.status, delivery.error_code,
                        event["type"], bounce_type=event.get("bounce_type", ""))
                    if event["type"] == "email.bounced" and delivery.error_code != "resend_complained":
                        if delivery.status == "bounced":
                            delivery.bounce_kind = "hard"
                        elif delivery.status == "undelivered":
                            delivery.bounce_kind = "soft"
                    if event["type"] == "email.delivered" and delivery.status in ("delivered", "read"):
                        delivery.bounce_kind = ""
                    if (event["type"] == "email.delivered" and delivery.delivered_at is None
                            and (delivery.status in ("delivered", "read") or delivery.error_code == "resend_complained")):
                        delivery.delivered_at = stored.occurred_at
                    delivery.save(update_fields=["provider_id", "status", "error_code", "bounce_kind", "delivered_at", "updated_at"])
                _apply_contact_feedback(event, stored, delivery, now)
    # Retrying suppression after a provider outage must work for duplicate receipts too.
    contact = Contact.objects.filter(email=event["recipient"]).first()
    if (event["type"] in ("email.bounced", "email.complained", "email.suppressed")
            and contact and contact.email_status in ("bounced", "complained", "unsubscribed")
            and contact.listmonk_subscriber_id):
        from . import listmonk
        listmonk.blocklist_subscribers([contact.listmonk_subscriber_id])
    return {"status": "recorded" if created and not legacy else "duplicate"}


class ResendWebhookView(APIView):
    """Svix authentication; persistence or suppression failures return 5xx for retry."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = []

    def post(self, request):
        secret = getattr(settings, "CRM_RESEND_WEBHOOK_SECRET", "")
        if not secret:
            return Response({"error": "not_configured"}, status=503)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except (ValueError, TypeError):
            return Response({"error": "invalid_event"}, status=400)
        if length > MAX_BODY:
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
