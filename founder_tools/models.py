import uuid

from django.conf import settings
from django.db import models

from vibe_raising.audience_visibility import default_audience_visibility


class VibeRaisingProfile(models.Model):
    ROLE_FOUNDER = "founder"
    ROLE_INVESTOR = "investor"
    ROLE_CHOICES = (
        (ROLE_FOUNDER, "Founder"),
        (ROLE_INVESTOR, "Investor"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vibe_raising_profile",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    organization_name = models.CharField(max_length=255, blank=True, null=True)
    active_company = models.ForeignKey(
        "VibeRaisingCompany",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vibe_raising_viberaisingprofile"
        ordering = ["user_id"]

    def __str__(self):
        return f"{self.user.email} ({self.role})"


class VibeRaisingCompany(models.Model):
    LEGACY_ACCESS = 0
    SHARED_ACCESS = 1

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        VibeRaisingProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="companies",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        related_name="founder_companies",
        blank=True,
        null=True,
    )
    membership_policy = models.PositiveSmallIntegerField(default=LEGACY_ACCESS, db_default=LEGACY_ACCESS)
    closed_at = models.DateTimeField(null=True, blank=True)
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True, null=True)
    abn = models.CharField(max_length=64, blank=True, null=True)
    # Optional ACN for Australian companies. Associations and other not-for-profits
    # qualify through their active ABR-verified ABN without a company ACN.
    acn = models.CharField(max_length=32, blank=True, null=True)
    # ABR entity-type code (e.g. "PRV") captured at verification time, kept for audit
    # and so the gate decision is inspectable without re-hitting the register.
    entity_type_code = models.CharField(max_length=16, blank=True, default="")
    # When the startup was last successfully verified as an active Australian entity
    # against the Australian Business Register. Null means never verified.
    abr_verified_at = models.DateTimeField(blank=True, null=True)
    location = models.CharField(max_length=255, blank=True, default="")
    # Firebase download URLs include the encoded object path and access token.
    avatar_url = models.URLField(max_length=2048, blank=True, null=True)
    email_invites_enabled = models.BooleanField(default=False)
    default_audience_visibility = models.JSONField(default=default_audience_visibility, blank=True)
    registered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vibe_raising_viberaisingcompany"
        ordering = ["created_at", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(membership_policy=1, organization__isnull=False),
                name="uniq_shared_startup_organization",
            ),
            # A profile cannot register two companies on the same domain — they
            # would collapse onto one Organization (keyed on a unique domain) and
            # silently share marketing/raising data. Partial so domainless drafts
            # are exempt and two *different* founders may share a domain.
            models.UniqueConstraint(
                fields=["profile", "domain"],
                condition=models.Q(domain__isnull=False) & ~models.Q(domain=""),
                name="uniq_profile_domain",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.profile.user.email})" if self.profile_id else self.name


class StartupMembership(models.Model):
    """A person's access to one canonical startup."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(VibeRaisingCompany, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="startup_memberships")
    admitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admitted_startup_memberships",
    )
    joined_at = models.DateTimeField()
    removed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=32)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "user"], name="uniq_startup_membership"),
        ]
        indexes = [
            models.Index(fields=["user", "company"], condition=models.Q(removed_at__isnull=True),
                         name="startup_member_by_user"),
        ]


class StartupMembershipEvent(models.Model):
    """Durable membership and closure history."""

    company = models.ForeignKey(VibeRaisingCompany, on_delete=models.CASCADE, related_name="membership_events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
    )
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
    )
    kind = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)


class StartupTeamInvitation(models.Model):
    """Recipient-bound invitation to one startup, independent of account creation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    company = models.ForeignKey(VibeRaisingCompany, on_delete=models.CASCADE, related_name="team_invitations")
    email = models.EmailField()
    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    policy = models.PositiveSmallIntegerField()
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_open_startup_invite",
            ),
        ]


class StartupTeamInviteDelivery(models.Model):
    """Durable, leased send with a stable payload across transport retries."""

    class State(models.TextChoices):
        QUEUED = "queued"
        SENDING = "sending"
        SENT = "sent"
        FAILED = "failed"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invitation = models.ForeignKey(StartupTeamInvitation, on_delete=models.CASCADE, related_name="deliveries")
    public_id = models.UUIDField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    context = models.JSONField(default=dict)
    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED)
    created_at = models.DateTimeField()
    available_at = models.DateTimeField()
    leased_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    retryable = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["state", "available_at"], name="startup_invite_delivery_due"),
            models.Index(fields=["actor", "created_at"], name="startup_invite_actor_recent"),
        ]
