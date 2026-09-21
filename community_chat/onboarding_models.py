"""Private membership applications and purpose-specific consent history."""

from django.conf import settings
from django.db import models


class CommunityMemberProfile(models.Model):
    """Private onboarding state, separate from the shared account's activation."""

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"
        PENDING = "pending_review", "Needs review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="community_member_profile",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INCOMPLETE)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    adult_confirmed_at = models.DateTimeField(null=True, blank=True)
    policy_version = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=40, blank=True)
    interests = models.JSONField(default=list, blank=True)
    marketing_opt_in = models.BooleanField(null=True, blank=True)
    personalisation_skipped = models.BooleanField(default=False)
    review_reasons = models.JSONField(default=list, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_community_applications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("status", "submitted_at"), name="chat_member_review_queue")]


class CommunityMemberConsent(models.Model):
    """Append-only evidence; an absent optional choice is never permission."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=32, choices=(
        ("adult_eligibility", "18+ eligibility"),
        ("terms", "Terms"),
        ("code_of_conduct", "Code of Conduct"),
        ("marketing_email", "Marketing email"),
    ))
    granted = models.BooleanField()
    policy_version = models.CharField(max_length=80)
    source = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("user", "purpose", "created_at"), name="chat_member_consent_history")]


class CommunityMemberReviewRule(models.Model):
    """Committee-managed exact names or words requiring a human review."""

    class Match(models.TextChoices):
        EXACT = "exact", "Exact name"
        WORD = "word", "Whole word or phrase"

    phrase = models.CharField(max_length=80)
    match = models.CharField(max_length=8, choices=Match.choices, default=Match.EXACT)
    reason = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

