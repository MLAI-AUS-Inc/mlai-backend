"""Minimal Apple purchase receipts; signed payloads and payment details stay out."""

import uuid

from django.conf import settings
from django.db import models


class AppleIapTransaction(models.Model):
    """One account-bound, idempotent consumable delivery and refund state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    environment = models.CharField(max_length=16)
    transaction_id = models.CharField(max_length=64)
    original_transaction_id = models.CharField(max_length=64)
    account_token = models.UUIDField()
    product_id = models.CharField(max_length=100)
    quantity = models.PositiveSmallIntegerField()
    granted_microroo = models.PositiveBigIntegerField(default=0)
    price_milliunits = models.PositiveBigIntegerField(null=True)
    currency = models.CharField(max_length=3, blank=True)
    purchased_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=16, default="pending")
    latest_signed_at = models.DateTimeField()
    payload_digest = models.CharField(max_length=64)
    grant_ledger = models.ForeignKey(
        "roo.Ledger", null=True, on_delete=models.SET_NULL, related_name="apple_grants",
    )
    reversal_ledger = models.ForeignKey(
        "roo.Ledger", null=True, on_delete=models.SET_NULL, related_name="apple_reversals",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=("environment", "transaction_id"), name="roo_apple_environment_tx_uniq",
        )]
        indexes = [
            models.Index(fields=("user", "environment", "purchased_at"), name="roo_apple_owner_date_idx"),
            models.Index(fields=("status",), name="roo_apple_status_idx"),
        ]


class AppleIapNotification(models.Model):
    """Replay receipt for an independently verified Apple server notification."""

    id = models.UUIDField(primary_key=True, editable=False)
    notification_type = models.CharField(max_length=64)
    subtype = models.CharField(max_length=64, blank=True)
    environment = models.CharField(max_length=16)
    signed_at = models.DateTimeField()
    payload_digest = models.CharField(max_length=64)
    transaction = models.ForeignKey(AppleIapTransaction, null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, default="pending")
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
