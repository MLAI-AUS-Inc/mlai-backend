"""Delivery log, suppressions and webhook dedupe for transactional email.

No row ever holds a message body, a sign-in code or a link.
"""

import uuid

from django.db import models


class SystemEmailStatus(models.TextChoices):
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    DELAYED = "delayed", "Delivery delayed"
    BOUNCED = "bounced", "Bounced"
    COMPLAINED = "complained", "Marked as spam"
    SUPPRESSED = "suppressed", "Suppressed"
    FAILED = "failed", "Failed"


class SystemEmail(models.Model):
    """One send attempt group for one message to one recipient."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message_key = models.CharField(max_length=64, db_index=True)
    category = models.CharField(max_length=32)
    provider = models.CharField(max_length=32)
    to_email = models.EmailField(max_length=320)
    user_id = models.BigIntegerField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=256, unique=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    status = models.CharField(max_length=16, choices=SystemEmailStatus.choices, default=SystemEmailStatus.SENDING)
    error_code = models.CharField(max_length=120, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("category", "created_at"), name="system_email_cat_created_idx")]

    def __str__(self):
        return f"{self.message_key} {self.status}"


class SystemEmailSuppression(models.Model):
    """An address that hard-bounced or complained. Clearing it keeps the row."""

    email = models.EmailField(max_length=320, unique=True)
    reason = models.CharField(max_length=32)
    source_event_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    cleared_by = models.CharField(max_length=255, blank=True, default="")

    @property
    def active(self):
        return self.cleared_at is None

    def __str__(self):
        return f"{self.reason} ({'active' if self.active else 'cleared'})"


class SystemEmailWebhookEvent(models.Model):
    """Resend webhook deliveries already processed, keyed by Svix message id."""

    svix_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
