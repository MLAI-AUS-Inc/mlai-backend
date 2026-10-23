"""Twilio webhooks for crm blasts, mounted at ``/api/v1/crm/webhooks/``."""

from django.urls import path

from . import webhooks

urlpatterns = [
    path(
        "twilio/status/",
        webhooks.TwilioStatusWebhookView.as_view(),
        name="crm_twilio_status_webhook",
    ),
    path(
        "twilio/sms-inbound/",
        webhooks.TwilioSmsInboundWebhookView.as_view(),
        name="crm_twilio_sms_inbound_webhook",
    ),
]
