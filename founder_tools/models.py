import uuid

from django.conf import settings
from django.db import models


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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        VibeRaisingProfile,
        on_delete=models.CASCADE,
        related_name="companies",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        related_name="founder_companies",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True, null=True)
    abn = models.CharField(max_length=64, blank=True, null=True)
    location = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(blank=True, null=True)
    registered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vibe_raising_viberaisingcompany"
        ordering = ["created_at", "name"]
        constraints = [
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
        return f"{self.name} ({self.profile.user.email})"
