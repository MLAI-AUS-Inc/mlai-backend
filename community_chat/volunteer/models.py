"""Additive Volunteer records; existing Roo ledger remains authoritative."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class ScopedRecord(models.Model):
    """Common server-derived community boundary and stable identifier."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    community = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class VolunteerProject(ScopedRecord):
    """A curated public-safe brief, independent from the internal backlog."""

    title = models.CharField(max_length=200)
    purpose = models.CharField(max_length=500)
    description = models.TextField()
    guide = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_projects",
    )
    source = models.JSONField(default=dict)
    published = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(
                fields=("community", "published"), name="vol_project_public_idx"
            )
        ]


class VolunteerOpportunity(ScopedRecord):
    """One shared conversation invitation; never an assignment or roster."""

    project = models.ForeignKey(
        VolunteerProject,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunities",
    )
    event_id = models.CharField(max_length=200, blank=True)
    kind = models.CharField(
        max_length=16, choices=(("event", "Event"), ("project", "Project"))
    )
    action_key = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    purpose = models.CharField(max_length=500)
    description = models.TextField()
    learning = models.CharField(max_length=500, blank=True)
    guide = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="guided_volunteer_opportunities",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_volunteer_opportunities",
    )
    source = models.JSONField(default=dict)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    reward_microroo = models.PositiveBigIntegerField()
    reward_max_microroo = models.PositiveBigIntegerField()
    recommended_level = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        default="open",
        choices=(("open", "Open"), ("closed", "Closed"), ("archived", "Archived")),
    )
    audience = models.CharField(max_length=16, default="community")
    version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(
                fields=("community", "status", "event_id"),
                name="vol_opportunity_feed_idx",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("community", "event_id"),
                condition=Q(kind="event") & ~Q(event_id=""),
                name="vol_one_opportunity_per_event",
            ),
            models.CheckConstraint(
                check=Q(reward_max_microroo__gte=F("reward_microroo")),
                name="vol_reward_range_valid",
            ),
        ]


class VolunteerMemberState(ScopedRecord):
    """Audited historical opening and per-member serialisation point."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_states",
    )
    historical_microroo = models.PositiveBigIntegerField(null=True, blank=True)
    historical_ledger_cutoff = models.PositiveBigIntegerField(default=0)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciled_volunteer_states",
    )
    reconciliation_note = models.TextField(blank=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("community", "user"), name="vol_member_community_unique"
            )
        ]


class VolunteerRecognition(ScopedRecord):
    """Private, source-backed completed work and its append-only decision trail."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_recognitions",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="volunteer_review_queue",
    )
    opportunity = models.ForeignKey(
        VolunteerOpportunity,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recognitions",
    )
    outcome_key = models.CharField(max_length=255)
    action_key = models.CharField(max_length=64)
    source = models.JSONField(default=dict)
    policy_snapshot = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    note = models.TextField(blank=True)
    evidence = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="pending")
    version = models.PositiveIntegerField(default=1)
    reward_microroo = models.PositiveBigIntegerField(default=0)
    ledger = models.OneToOneField(
        "roo.Ledger",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="volunteer_recognition",
    )
    review_history = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("community", "user", "outcome_key"),
                name="vol_recognition_outcome_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("community", "user", "status", "created_at"),
                name="vol_recognition_member_idx",
            ),
            models.Index(fields=("reviewer", "status"), name="vol_review_queue_idx"),
        ]


class VolunteerMilestone(ScopedRecord):
    """A stable once-only bonus identity independent of policy versions."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_milestones",
    )
    level_key = models.CharField(max_length=64)
    ledger = models.OneToOneField(
        "roo.Ledger",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="volunteer_milestone",
    )
    recognition = models.ForeignKey(
        VolunteerRecognition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="milestones",
    )
    reached_at = models.DateTimeField()
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("community", "user", "level_key"),
                name="vol_milestone_once_unique",
            )
        ]


class VolunteerAttendance(ScopedRecord):
    """A verified check-in; registration and interest are never attendance."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_attendances",
    )
    event_id = models.CharField(max_length=200)
    checked_in_at = models.DateTimeField()
    source_id = models.CharField(max_length=255)
    verifier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_volunteer_attendances",
    )
    reason = models.TextField(blank=True)
    audit_history = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("community", "user", "event_id"),
                name="vol_attendance_event_unique",
            )
        ]


class VolunteerSourceReceipt(ScopedRecord):
    """Durable authoritative metadata and retry state; never client telemetry."""

    source_key = models.CharField(max_length=255)
    origin = models.CharField(max_length=64)
    kind = models.CharField(max_length=32)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="volunteer_source_receipts",
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="volunteer_received_sources",
    )
    source = models.JSONField(default=dict)
    metadata = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")
    error = models.CharField(max_length=120, blank=True)
    recognition = models.ForeignKey(
        VolunteerRecognition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_receipts",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("community", "source_key"), name="vol_source_origin_unique"
            )
        ]
        indexes = [
            models.Index(
                fields=("community", "kind", "status"), name="vol_source_processing_idx"
            ),
            models.Index(fields=("actor", "occurred_at"), name="vol_source_actor_idx"),
        ]
