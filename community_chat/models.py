import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class DeviceBindingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VERIFIED = "verified", "Verified"
    REVOKED = "revoked", "Revoked"


class EmailCodeDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class CommunityChatDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="community_chat_devices",
    )
    public_key = models.CharField(max_length=64)
    installation_id = models.UUIDField(default=uuid.uuid4)
    client_id = models.CharField(max_length=64, default="legacy")
    platform = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=16,
        choices=DeviceBindingStatus.choices,
        default=DeviceBindingStatus.PENDING,
    )
    verified_at = models.DateTimeField(blank=True, null=True)
    last_verified_membership_at = models.DateTimeField(blank=True, null=True)
    last_seen_at = models.DateTimeField(blank=True, null=True)
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
            models.UniqueConstraint(
                fields=("installation_id",),
                condition=Q(status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED)),
                name="chat_unique_active_installation",
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
    installation_id = models.UUIDField(default=uuid.uuid4)
    client_id = models.CharField(max_length=64, default="legacy")
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


class CommunityChatDeviceAuthRequest(models.Model):
    """Short-lived, PKCE-bound handoff from an MLAI browser session to an app."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_key = models.CharField(max_length=64)
    origin = models.CharField(max_length=255)
    state_hash = models.CharField(max_length=64)
    code_challenge = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="community_chat_auth_requests",
    )
    authorized_at = models.DateTimeField(blank=True, null=True)
    consumed_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("expires_at",), name="chat_auth_request_expiry_idx"),
            models.Index(fields=("public_key", "created_at"), name="chat_auth_request_key_idx"),
        ]


class CommunityChatBootstrapToken(models.Model):
    """Opaque, narrowly scoped bearer used only while enrolling one device key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="community_chat_bootstrap_tokens",
    )
    public_key = models.CharField(max_length=64)
    installation_id = models.UUIDField(default=uuid.uuid4)
    client_id = models.CharField(max_length=64, default="legacy")
    origin = models.CharField(max_length=255, default="legacy")
    platform = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=120, blank=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "public_key"), name="chat_bootstrap_user_key_idx"),
            models.Index(fields=("expires_at",), name="chat_bootstrap_expiry_idx"),
        ]


class CommunityChatEmailCodeChallenge(models.Model):
    """One-use email proof bound to one registered Chat installation.

    ``user`` is deliberately nullable. Unknown and ineligible email addresses
    receive the same API response and a non-deliverable challenge so account
    existence is not exposed through the public request contract.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="community_chat_email_code_challenges",
    )
    email_digest = models.CharField(max_length=64)
    code_digest = models.CharField(max_length=64)
    client_id = models.CharField(max_length=64)
    installation_id = models.UUIDField()
    origin = models.CharField(max_length=255)
    platform = models.CharField(max_length=32)
    device_name = models.CharField(max_length=120, blank=True)
    public_key = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempt_count = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    consumed_at = models.DateTimeField(blank=True, null=True)
    invalidated_at = models.DateTimeField(blank=True, null=True)
    requested_ip_digest = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("email_digest", "client_id", "installation_id", "created_at"),
                name="chat_email_code_lookup_idx",
            ),
            models.Index(fields=("expires_at",), name="chat_email_code_expiry_idx"),
            models.Index(fields=("user", "created_at"), name="chat_email_code_user_idx"),
        ]

    def __str__(self):
        return f"{self.id}:{self.client_id}"


class CommunityChatEmailCodeDelivery(models.Model):
    """Durable Customer.io outbox row containing an encrypted login code."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.OneToOneField(
        CommunityChatEmailCodeChallenge,
        on_delete=models.CASCADE,
        related_name="email_delivery",
    )
    encrypted_code = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=EmailCodeDeliveryStatus.choices,
        default=EmailCodeDeliveryStatus.PENDING,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    provider_delivery_id = models.CharField(max_length=255, blank=True)
    last_error_code = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(
                fields=("status", "available_at", "created_at"),
                name="chat_email_delivery_idx",
            ),
        ]

    def __str__(self):
        return f"{self.challenge_id}:{self.status}"


class CommunityChatAccountSession(models.Model):
    """Rotating, Chat-scoped account session for one app installation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="community_chat_account_sessions",
    )
    public_key = models.CharField(max_length=64)
    installation_id = models.UUIDField()
    client_id = models.CharField(max_length=64)
    origin = models.CharField(max_length=255)
    platform = models.CharField(max_length=32)
    name = models.CharField(max_length=120, blank=True)
    access_token_hash = models.CharField(max_length=64, unique=True)
    refresh_token_hash = models.CharField(max_length=64, unique=True)
    auth_version = models.PositiveIntegerField()
    access_expires_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("user", "revoked_at", "expires_at"),
                name="chat_session_user_active_idx",
            ),
            models.Index(
                fields=("client_id", "installation_id"),
                name="chat_session_install_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.client_id}:{self.installation_id}"
