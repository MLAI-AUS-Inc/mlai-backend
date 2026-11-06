"""Mounted at ``/api/v1/system-email/webhooks/``."""

from django.urls import path

from .webhooks import SystemEmailResendWebhookView

urlpatterns = [
    path("resend/", SystemEmailResendWebhookView.as_view(), name="system_email_resend_webhook"),
]
