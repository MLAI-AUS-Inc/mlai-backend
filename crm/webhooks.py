"""Twilio webhooks for blasts: delivery status callbacks and SMS replies.

Server to server, so there is no account authentication. Every request must
carry a valid ``X-Twilio-Signature``. Anything handled or ignored answers 200,
because Twilio would otherwise replay it, opt-outs included.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.services.notification_adapters import verify_whatsapp_webhook_signature

from .inbound import handle_inbound
from .messaging import record_status_callback

logger = logging.getLogger(__name__)

# An empty TwiML reply: Twilio sends nothing back to the person.
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class TwilioWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    @staticmethod
    def signature_valid(request) -> bool:
        # The shared check accepts everything when no auth token is set; for
        # contact data that is only acceptable in local development.
        if not str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip() and not settings.DEBUG:
            return False
        # Twilio signs the URL it was configured with. Rebuild it from
        # DEFAULT_BACKEND_URL: behind the reverse proxy the request would
        # reassemble as http:// and every check would fail.
        base_url = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").rstrip("/")
        url = f"{base_url}{request.get_full_path()}" if base_url else request.build_absolute_uri()
        signature = request.headers.get("X-Twilio-Signature", "")
        return verify_whatsapp_webhook_signature(url=url, params=request.POST, signature=signature)

    @staticmethod
    def forbidden() -> Response:
        return Response({"error": "invalid_signature"}, status=status.HTTP_403_FORBIDDEN)


class TwilioStatusWebhookView(TwilioWebhookView):
    """StatusCallback for every SMS and WhatsApp blast message."""

    def post(self, request):
        if not self.signature_valid(request):
            return self.forbidden()
        try:
            result = record_status_callback(request.POST.dict())
        except Exception as exc:
            logger.warning("crm Twilio status callback failed: %s", exc.__class__.__name__)
            result = {"status": "error"}
        return Response(result, status=status.HTTP_200_OK)


class TwilioSmsInboundWebhookView(TwilioWebhookView):
    """Incoming texts on the Messaging Service: replies, STOP and START."""

    def post(self, request):
        if not self.signature_valid(request):
            return self.forbidden()
        payload = request.POST
        handle_inbound(
            channel="sms",
            from_address=payload.get("From", ""),
            body=payload.get("Body", ""),
            provider_id=payload.get("MessageSid") or payload.get("SmsSid") or "",
            opt_out_type=payload.get("OptOutType", ""),
        )
        return HttpResponse(EMPTY_TWIML, content_type="text/xml", status=status.HTTP_200_OK)
