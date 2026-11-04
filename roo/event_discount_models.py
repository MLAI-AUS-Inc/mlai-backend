"""Durable event offers, member quotes, and coupon fulfilment receipts."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q


class EventDiscountOffer(models.Model):
    """An explicitly enabled, administrator-priced discount for one Luma event."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reward = models.OneToOneField(
        "roo.RewardsCatalog", on_delete=models.PROTECT, related_name="event_discount_offer",
    )
    event_id = models.CharField(max_length=100, unique=True)
    event_ticket_type_id = models.CharField(max_length=100)
    discount_type = models.CharField(max_length=10, choices=(("percent", "Percent"), ("amount", "Amount")))
    percent_off = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    cents_off = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default="aud", choices=(("aud", "AUD"),))
    is_active = models.BooleanField(default=False)
    valid_start_at = models.DateTimeField(null=True, blank=True)
    valid_end_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(discount_type="percent", percent_off__isnull=False, percent_off__gte=1, percent_off__lte=100, cents_off__isnull=True)
                    | Q(discount_type="amount", cents_off__isnull=False, cents_off__gt=0, percent_off__isnull=True)
                ), name="event_offer_discount_valid",
            ),
            models.CheckConstraint(condition=Q(currency="aud"), name="event_offer_aud_only"),
            models.CheckConstraint(
                condition=Q(valid_start_at__isnull=True) | Q(valid_end_at__gt=models.F("valid_start_at")),
                name="event_offer_window_valid",
            ),
        ]

    def clean(self):
        """Reject terms that cannot form a paid, event-specific points offer."""
        super().clean()
        if self.reward_id and self.reward.cost_points <= 0:
            raise ValidationError({"reward": "An event offer must cost at least one Roo Point."})
        if not self.event_id.startswith("evt-") or not self.event_ticket_type_id:
            raise ValidationError("A canonical Luma event and ticket type are required.")

    def __str__(self):
        return f"{self.reward_id}: {self.event_id}"


class EventDiscountQuote(models.Model):
    """An expiring offer snapshot bound to a verified member's private chat."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="event_discount_quotes")
    offer = models.ForeignKey(EventDiscountOffer, on_delete=models.PROTECT, related_name="quotes")
    offer_fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    slack_team_id = models.CharField(max_length=30)
    slack_channel_id = models.CharField(max_length=30)
    slack_thread_ts = models.CharField(max_length=30, blank=True, default="")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("user", "expires_at"), name="event_quote_owner_expiry")]


class EventDiscountRedemption(models.Model):
    """One confirmed redemption, including a hold and recoverable external write."""

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        FULFILLED = "fulfilled", "Fulfilled"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="event_discount_redemptions")
    quote = models.OneToOneField(EventDiscountQuote, on_delete=models.PROTECT, related_name="redemption")
    offer = models.ForeignKey(EventDiscountOffer, on_delete=models.PROTECT, related_name="redemptions")
    event_id = models.CharField(max_length=100)
    request_id = models.CharField(max_length=100, unique=True)
    coupon_code = models.CharField(
        max_length=20, unique=True,
        validators=[RegexValidator(r"^ROO-[A-Z2-7]{16}$", "Use an opaque 20-character Roo coupon code.")],
    )
    luma_coupon_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSING)
    reserved_microroo = models.PositiveBigIntegerField(default=0)
    purchased_microroo = models.PositiveBigIntegerField(default=0)
    ledger_entry = models.OneToOneField("roo.Ledger", on_delete=models.PROTECT, null=True, blank=True)
    creation_attempted_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    needs_review = models.BooleanField(default=False)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "event_id"),
                condition=Q(status__in=("processing", "fulfilled")),
                name="one_member_event_discount",
            ),
            models.CheckConstraint(
                condition=Q(status="processing") | Q(reserved_microroo=0),
                name="event_hold_processing_only",
            ),
            models.CheckConstraint(
                condition=~Q(status="fulfilled") | Q(ledger_entry__isnull=False),
                name="event_fulfilled_has_ledger",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="event_redemption_work"),
        ]
