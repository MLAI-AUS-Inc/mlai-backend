import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class DeviceBindingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VERIFIED = "verified", "Verified"
    REVOKED = "revoked", "Revoked"


class CommunityChatDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="community_chat_devices",
    )
    public_key = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=DeviceBindingStatus.choices,
        default=DeviceBindingStatus.PENDING,
    )
    verified_at = models.DateTimeField(blank=True, null=True)
    last_verified_membership_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="revoked_community_chat_devices",
    )
    revocation_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("public_key",),
                condition=Q(status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED)),
                name="community_chat_unique_active_public_key",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "status"), name="chat_device_user_status_idx"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.public_key[:12]} ({self.status})"


class CommunityChatChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="community_chat_challenges",
    )
    public_key = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    audience = models.CharField(max_length=255)
    origin = models.CharField(max_length=255)
    nonce_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "public_key", "created_at"), name="chat_challenge_lookup_idx"),
            models.Index(fields=("expires_at",), name="chat_challenge_expiry_idx"),
        ]


class CommunityChatInviteAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        CommunityChatDevice,
        on_delete=models.PROTECT,
        related_name="invite_audits",
    )
    challenge = models.OneToOneField(
        CommunityChatChallenge,
        on_delete=models.PROTECT,
        related_name="invite_audit",
    )
    adapter_invite_id = models.CharField(max_length=128)
    adapter_request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    expires_at = models.DateTimeField()
    issued_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-issued_at",)
        indexes = [
            models.Index(fields=("device", "issued_at"), name="chat_invite_device_idx"),
        ]

