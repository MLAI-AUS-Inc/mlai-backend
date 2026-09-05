import uuid
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


POINTS_PURCHASE_EXPIRY_HOURS = 24


def default_points_purchase_expires_at():
    return timezone.now() + timedelta(hours=POINTS_PURCHASE_EXPIRY_HOURS)


def default_coding_turn_expires_at():
    return timezone.now() + timedelta(hours=24)


class PointsAdmin(models.Model):
    """
    Users authorized to mint and approve tasks.
    Formerly known as Minter - renamed for clarity.
    """
    ROLE_CHOICES = (
        ('committee', 'Committee'),
        ('portfolio_lead', 'Portfolio Lead'),
        ('admin', 'Admin'),
        ('partner', 'Partner'),
    )
    PORTFOLIO_CHOICES = (
        ('events', 'Events'),
        ('marketing', 'Marketing'),
        ('tech', 'Tech'),
        ('ops', 'Ops'),
        ('sales', 'Sales'),
    )

    slack_user_id = models.CharField(max_length=50, unique=True, help_text="Slack User ID (e.g. U123ABC)")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='points_admin_profile')
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default='committee')
    portfolio = models.CharField(max_length=50, choices=PORTFOLIO_CHOICES, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    added_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    weekly_allowance = models.IntegerField(default=100, help_text="Max points this admin can award per week")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Points Admin"
        verbose_name_plural = "Points Admins"

    def __str__(self):
        # Use linked user's name if available, otherwise fall back to Slack ID
        display_name = self.user.full_name if self.user else self.slack_user_id
        if not display_name and self.user:
             display_name = self.user.email
        return f"{display_name} ({self.slack_user_id}) - {self.role}"


# Keep Minter as an alias for backwards compatibility
Minter = PointsAdmin


class PointsAccount(models.Model):
    """
    Cached balance and lifetime stats per user.
    One row per user - serves as the source of truth for current balance.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='points_account', 
        primary_key=True
    )
    balance = models.IntegerField(default=0, help_text="Current spendable balance")
    earned_balance = models.IntegerField(default=0, help_text="Current balance from earned contribution points")
    purchased_topup_balance = models.IntegerField(default=0, help_text="Current balance from purchased top-up points")
    lifetime_earned = models.IntegerField(default=0, help_text="Total points ever earned")
    lifetime_purchased_topup = models.IntegerField(default=0, help_text="Total purchased top-up points ever credited")
    lifetime_spent = models.IntegerField(default=0, help_text="Total points ever spent")
    expired_or_reversed_points = models.IntegerField(default=0, help_text="Total points expired or reversed")
    # Microroo fields are the precision-safe source of truth.  The historical
    # integer fields above remain during the compatibility window and expose
    # only whole, spendable Roo to older clients.
    balance_microroo = models.BigIntegerField(default=0, help_text="Current spendable balance in microroo")
    earned_balance_microroo = models.BigIntegerField(default=0, help_text="Earned balance in microroo")
    purchased_topup_balance_microroo = models.BigIntegerField(default=0, help_text="Purchased balance in microroo")
    lifetime_earned_microroo = models.BigIntegerField(default=0, help_text="Lifetime earned in microroo")
    lifetime_purchased_topup_microroo = models.BigIntegerField(default=0, help_text="Lifetime purchased in microroo")
    lifetime_spent_microroo = models.BigIntegerField(default=0, help_text="Lifetime spent in microroo")
    expired_or_reversed_microroo = models.BigIntegerField(default=0, help_text="Expired or reversed amount in microroo")
    microroo_initialized = models.BooleanField(
        default=False,
        help_text="Whether precision fields have been initialized from legacy whole Roo",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Points Account"
        verbose_name_plural = "Points Accounts"

    def __str__(self):
        return f"{self.user.email}: {self.balance} pts (earned: {self.lifetime_earned}, spent: {self.lifetime_spent})"

    def save(self, *args, **kwargs):
        """Mirror explicit legacy field updates during the transition window.

        Existing code and operational tests sometimes use
        ``save(update_fields=[...])`` to set a whole-Roo balance directly.  An
        explicit update remains authoritative and is mirrored to microroo.
        Normal service saves omit ``update_fields`` or update both projections,
        so fractional precision is never rounded away accidentally.
        """
        requested = kwargs.get("update_fields")
        if requested is not None:
            update_fields = set(requested)
            mapping = {
                "balance": "balance_microroo",
                "earned_balance": "earned_balance_microroo",
                "purchased_topup_balance": "purchased_topup_balance_microroo",
                "lifetime_earned": "lifetime_earned_microroo",
                "lifetime_purchased_topup": "lifetime_purchased_topup_microroo",
                "lifetime_spent": "lifetime_spent_microroo",
                "expired_or_reversed_points": "expired_or_reversed_microroo",
            }
            mirrored = False
            for legacy_field, micro_field in mapping.items():
                if legacy_field in update_fields and micro_field not in update_fields:
                    setattr(self, micro_field, getattr(self, legacy_field) * 1_000_000)
                    update_fields.add(micro_field)
                    mirrored = True
            if mirrored:
                self.microroo_initialized = True
                update_fields.add("microroo_initialized")
                kwargs["update_fields"] = tuple(update_fields)
        super().save(*args, **kwargs)


class PointsPurchase(models.Model):
    """
    Pending and completed Top-up Roo Points purchases.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('refunded', 'Refunded'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='points_purchases',
    )
    slack_user_id = models.CharField(max_length=50, db_index=True)
    pack_id = models.CharField(max_length=50)
    points_amount = models.PositiveIntegerField()
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default='aud')
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='pending', db_index=True)
    stripe_checkout_session_id = models.CharField(max_length=255, unique=True, blank=True, null=True)
    stripe_checkout_session_url = models.URLField(max_length=2048, blank=True, null=True)
    checkout_request_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    terms_version_accepted = models.CharField(max_length=100, blank=True, null=True)
    terms_accepted_at = models.DateTimeField(blank=True, null=True)
    privacy_version_accepted = models.CharField(max_length=100, blank=True, null=True)
    privacy_accepted_at = models.DateTimeField(blank=True, null=True)
    purchase_from = models.JSONField(default=dict, blank=True)
    ledger_entry = models.ForeignKey(
        'Ledger',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='points_purchases',
    )
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(default=default_points_purchase_expires_at)
    paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Points Purchase"
        verbose_name_plural = "Points Purchases"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status'], name='roo_purchase_user_status_idx'),
            models.Index(fields=['slack_user_id', 'status'], name='roo_purchase_slack_status_idx'),
            models.Index(fields=['status', 'created_at'], name='roo_purchase_status_ct_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['checkout_request_id', 'pack_id'],
                condition=models.Q(checkout_request_id__isnull=False),
                name='roo_purchase_request_pack_uniq',
            ),
        ]

    def __str__(self):
        return f"{self.points_amount} Top-up Roo Points for {self.slack_user_id} ({self.status})"


class TaskTemplate(models.Model):
    """
    Standard templates for tasks with fixed point values (Rate Card).
    """
    name = models.CharField(max_length=255)
    alias = models.SlugField(max_length=100, unique=True, help_text="Unique identifier for lookup e.g. 'newsletter'")
    points = models.IntegerField()
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Task Template"
        verbose_name_plural = "Task Templates (Rate Card)"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.points} pts)"


class Task(models.Model):
    """
    Volunteer work definition / opportunity.
    """
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('claimed', 'Claimed'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('pending_approval', 'Pending Approval'),  # Legacy status
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled'),
    )
    
    PORTFOLIO_CHOICES = PointsAdmin.PORTFOLIO_CHOICES
    WORK_DOMAIN_CHOICES = (
        ('tech', 'Tech'),
        ('event_ops', 'Event Ops'),
        ('content_comms', 'Content & Comms'),
        ('community', 'Community'),
        ('governance', 'Governance'),
        ('partnerships', 'Partnerships'),
        ('grants', 'Grants'),
        ('finance', 'Finance'),
        ('design', 'Design'),
        ('ops', 'Ops'),
    )
    REVIEW_FLOW_CHOICES = (
        ('pr_review', 'PR Review'),
        ('deliverable_review', 'Deliverable Review'),
        ('attendance_confirmation', 'Attendance Confirmation'),
    )
    VISIBILITY_CHOICES = (
        ('internal', 'Internal'),
        ('volunteer', 'Volunteer'),
        ('public', 'Public'),
    )
    DIFFICULTY_CHOICES = (
        ('tiny', 'Tiny'),
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
        ('lead', 'Lead'),
    )

    id = models.AutoField(primary_key=True)
    task_code = models.CharField(max_length=20, unique=True, blank=True, null=True, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    portfolio = models.CharField(max_length=50, choices=PORTFOLIO_CHOICES, default='events')
    work_domain = models.CharField(max_length=50, choices=WORK_DOMAIN_CHOICES, default='event_ops')
    review_flow = models.CharField(max_length=50, choices=REVIEW_FLOW_CHOICES, default='deliverable_review')
    points = models.IntegerField(default=1)
    points_estimate = models.IntegerField(default=1)
    points_min = models.IntegerField(default=1)
    points_max = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default='volunteer')
    volunteer_ready = models.BooleanField(default=False)
    difficulty = models.CharField(max_length=20, choices=DIFFICULTY_CHOICES, default='small', blank=True)
    estimate_minutes = models.PositiveIntegerField(blank=True, null=True)
    outcome = models.TextField(blank=True)
    definition_of_done = models.TextField(blank=True)
    acceptance_criteria = models.TextField(blank=True)
    how_to_test = models.TextField(blank=True)
    repo = models.CharField(max_length=255, blank=True)
    reviewer_slack_id = models.CharField(max_length=50, blank=True, null=True)
    fallback_reviewer_slack_id = models.CharField(max_length=50, blank=True, null=True)
    source_system = models.CharField(max_length=50, blank=True)
    source_ref = models.CharField(max_length=100, blank=True)
    source_url = models.URLField(blank=True)
    group_key = models.CharField(max_length=100, blank=True)
    slot_label = models.CharField(max_length=100, blank=True)
    group_capacity = models.PositiveIntegerField(blank=True, null=True)
    blocked_reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    # Slack Integrations - keeping original field names for backwards compat
    created_by_user_id = models.CharField(max_length=50, help_text="Slack ID of minter")
    assigned_to_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="Slack ID of volunteer")
    closed_by_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="Slack ID of approver")
    
    # New FK to User for better linking (optional enhancement)
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, blank=True,
        related_name='assigned_tasks',
        help_text="User who claimed this task (linked via FK)"
    )
    
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    slack_thread_ts = models.CharField(max_length=50, blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.task_code or f'#{self.id}'} {self.title} ({self.points_estimate} pts)"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating and not self.task_code:
            self.task_code = f"ROO-{self.id:04d}"
            super().save(update_fields=['task_code'])

    def get_active_assignment(self):
        return self.assignments.filter(status__in=TaskAssignment.ACTIVE_STATUSES).order_by('-claimed_at', '-created_at').first()

    def get_current_assignment(self):
        active = self.get_active_assignment()
        if active:
            return active
        return self.assignments.exclude(status='released').order_by('-approved_at', '-submitted_at', '-claimed_at', '-created_at').first()

    def sync_status_projection(self):
        if self.status == 'cancelled':
            return 'cancelled'

        assignment = self.get_current_assignment()
        if assignment is None:
            projected = 'open'
        elif assignment.status == 'claimed':
            projected = 'claimed'
        elif assignment.status == 'submitted':
            projected = 'submitted'
        elif assignment.status == 'approved':
            projected = 'approved'
        else:
            projected = 'open'

        return projected

TASK_ASSIGNMENT_ACTIVE_STATUSES = ('claimed', 'submitted')


class TaskAssignment(models.Model):
    """
    Execution / ownership record for a task.
    """
    STATUS_CHOICES = (
        ('claimed', 'Claimed'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('released', 'Released'),
        ('cancelled', 'Cancelled'),
    )
    ACTIVE_STATUSES = TASK_ASSIGNMENT_ACTIVE_STATUSES

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='assignments')
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='task_assignments',
    )
    assigned_to_slack_id = models.CharField(max_length=50, blank=True, null=True)
    claimed_points_snapshot = models.IntegerField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='claimed')
    claimed_at = models.DateTimeField(blank=True, null=True)
    released_at = models.DateTimeField(blank=True, null=True)
    submitted_at = models.DateTimeField(blank=True, null=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    approved_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    awarded_points = models.IntegerField(blank=True, null=True)
    closed_reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['task'],
                condition=Q(status__in=TASK_ASSIGNMENT_ACTIVE_STATUSES),
                name='roo_taskassignment_one_active_per_task',
            )
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.task} -> {self.assigned_to_slack_id or self.assigned_user_id or 'unassigned'} ({self.status})"


class TaskSubmission(models.Model):
    """
    Submission records for task assignments - tracks the submit → review workflow.
    """
    STATUS_CHOICES = (
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='submissions')
    assignment = models.ForeignKey(
        TaskAssignment,
        on_delete=models.CASCADE,
        related_name='submissions',
        blank=True,
        null=True,
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='task_submissions')
    submission_text = models.TextField(help_text="Description of work completed")
    submission_url = models.URLField(blank=True, null=True, help_text="Link to proof of work")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='submitted')
    evidence_kind = models.CharField(max_length=30, blank=True, default='text')
    evidence_payload = models.JSONField(default=dict, blank=True)
    review_notes = models.TextField(blank=True)
    reviewed_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    approved_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    ledger_entry = models.ForeignKey(
        'Ledger', 
        on_delete=models.SET_NULL, 
        blank=True, null=True,
        related_name='task_submission'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Task Submission"
        verbose_name_plural = "Task Submissions"

    def __str__(self):
        return f"Submission for Task #{self.task.id} by {self.user.email}"


class TaskActivity(models.Model):
    """
    Append-only task workflow audit trail.
    """
    EVENT_CHOICES = (
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('published', 'Published'),
        ('claimed', 'Claimed'),
        ('unclaimed', 'Unclaimed'),
        ('submitted', 'Submitted'),
        ('changes_requested', 'Changes Requested'),
        ('approved', 'Approved'),
        ('cancelled', 'Cancelled'),
        ('blocked', 'Blocked'),
        ('unblocked', 'Unblocked'),
    )

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='activity')
    assignment = models.ForeignKey(TaskAssignment, on_delete=models.SET_NULL, blank=True, null=True, related_name='activity')
    submission = models.ForeignKey(TaskSubmission, on_delete=models.SET_NULL, blank=True, null=True, related_name='activity')
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES)
    actor_slack_id = models.CharField(max_length=50, blank=True, null=True)
    summary = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"{self.task} {self.event_type}"


class Ledger(models.Model):
    """
    Append-only ledger of all points transactions.
    Every earn/spend/adjust/refund creates a row.
    """
    KIND_CHOICES = (
        ('EARN', 'Earn'),
        ('SPEND', 'Spend'),
        ('ADJUST', 'Adjust'),
        ('REFUND', 'Refund'),
    )
    SOURCE_CHOICES = (
        ('TASK', 'Task'),
        ('COWORKING', 'Coworking'),
        ('MEETING_ROOM', 'Meeting Room'),
        ('EVENT', 'Event'),
        ('MERCH', 'Merch'),
        ('CONTENT_FACTORY', 'Content Factory'),
        ('STARTUP_UPDATE', 'Startup Update'),
        ('TOOLS', 'Tools'),
        ('DONATION', 'Donation'),
        ('purchased_topup', 'Purchased Top-Up'),
        ('MANUAL', 'Manual'),
        ('LEGACY', 'Legacy'),  # For migrated entries
    )

    # Keep existing ID as BigAutoField (don't change to UUID)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='points_ledger',
        null=True, blank=True  # Allow null for legacy entries
    )
    # New structured fields - nullable for backwards compat
    delta = models.IntegerField(null=True, blank=True, help_text="Points change (positive=earn, negative=spend)")
    delta_microroo = models.BigIntegerField(null=True, blank=True, help_text="Exact change in microroo")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='LEGACY')
    reference_type = models.CharField(max_length=50, blank=True, null=True, help_text="e.g. TASK_SUBMISSION, BOOKING")
    reference_id = models.CharField(max_length=100, blank=True, null=True, help_text="ID of related object")
    description = models.TextField(blank=True, default='')
    created_by_slack_id = models.CharField(max_length=50, blank=True, null=True, help_text="Who initiated/approved")
    idempotency_key = models.CharField(max_length=255, unique=True, null=True, blank=True, help_text="Prevents duplicate transactions")
    created_at = models.DateTimeField(auto_now_add=True)

    # Keep backwards compatibility fields (deprecated but functional)
    slack_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="DEPRECATED: Use user FK instead")
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name='legacy_ledger_entries')
    points_delta = models.IntegerField(null=True, blank=True, help_text="DEPRECATED: Use delta instead")
    points_delta_microroo = models.BigIntegerField(null=True, blank=True, help_text="DEPRECATED exact legacy change in microroo")
    reason = models.TextField(blank=True, null=True, help_text="DEPRECATED: Use description instead")
    created_by_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="DEPRECATED: Use created_by_slack_id instead")

    class Meta:
        verbose_name = "Ledger Entry"
        verbose_name_plural = "Ledger Entries"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_by_slack_id', 'created_at'], name='roo_ledger_admin_week_idx'),
        ]

    def __str__(self):
        if self.delta is not None and self.user:
            return f"{self.kind or 'LEGACY'} {self.delta:+d} pts for {self.user.email} ({self.source})"
        # Legacy format
        return f"{self.points_delta or 0:+d} pts to {self.slack_user_id or 'unknown'}"
    
    def save(self, *args, **kwargs):
        # Migrate legacy data on save
        if self.delta is None and self.points_delta is not None:
            self.delta = self.points_delta
        if self.delta_microroo is None and self.delta is not None:
            self.delta_microroo = self.delta * 1_000_000
        if self.points_delta_microroo is None and self.points_delta is not None:
            self.points_delta_microroo = self.points_delta * 1_000_000
        if not self.description and self.reason:
            self.description = self.reason
        if not self.created_by_slack_id and self.created_by_user_id:
            self.created_by_slack_id = self.created_by_user_id
        super().save(*args, **kwargs)


class CodingPricingVersion(models.Model):
    """Immutable pricing inputs used to settle Kimi inference calls."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=80, unique=True)
    model = models.CharField(max_length=80, default="kimi-k3")
    input_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6)
    cached_input_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6)
    output_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6)
    usd_aud_rate = models.DecimalField(max_digits=12, decimal_places=6)
    margin_multiplier = models.DecimalField(max_digits=8, decimal_places=6, default="1.300000")
    aud_per_roo = models.DecimalField(max_digits=12, decimal_places=6, default="1.000000")
    is_active = models.BooleanField(default=True, db_index=True)
    effective_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_at", "-created_at")

    def __str__(self):
        return f"{self.model}:{self.version}"


class CodingTurn(models.Model):
    """A locally executed coding turn with a server-side Roo reservation."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RECONCILING = "reconciling", "Reconciling"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="coding_turns",
    )
    account_session = models.ForeignKey(
        "community_chat.CommunityChatAccountSession",
        on_delete=models.PROTECT,
        related_name="coding_turns",
    )
    device_id = models.UUIDField()
    local_session_id = models.UUIDField()
    idempotency_key = models.UUIDField()
    model = models.CharField(max_length=80, default="kimi-k3")
    pricing_version = models.ForeignKey(
        CodingPricingVersion,
        on_delete=models.PROTECT,
        related_name="turns",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    reserved_microroo = models.BigIntegerField(default=0)
    settled_microroo = models.BigIntegerField(default=0)
    released_microroo = models.BigIntegerField(default=0)
    finalize_outcome = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(default=default_coding_turn_expires_at, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "idempotency_key"),
                name="roo_coding_turn_user_idem_uniq",
            ),
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(status__in=("active", "reconciling")),
                name="roo_coding_one_open_turn_user",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "status"), name="roo_coding_turn_usr_status_idx"),
            models.Index(fields=("device_id", "status"), name="roo_coding_turn_device_idx"),
        ]


class CodingModelCall(models.Model):
    """One independently metered provider request within a coding turn."""

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        SETTLED = "settled", "Settled"
        RELEASED = "released", "Released"
        AMBIGUOUS = "ambiguous", "Ambiguous"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    turn = models.ForeignKey(CodingTurn, on_delete=models.PROTECT, related_name="model_calls")
    call_id = models.UUIDField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RESERVED, db_index=True)
    estimated_input_tokens = models.PositiveBigIntegerField(default=0)
    requested_output_tokens = models.PositiveBigIntegerField(default=0)
    max_output_tokens = models.PositiveBigIntegerField(default=0)
    reserved_microroo = models.BigIntegerField(default=0)
    charged_microroo = models.BigIntegerField(default=0)
    calculated_microroo = models.BigIntegerField(default=0)
    pricing_version_snapshot = models.CharField(max_length=80, default="do-kimi-k3-2026-08")
    input_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6, default="3.000000")
    cached_input_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6, default="0.600000")
    output_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6, default="15.000000")
    usd_aud_rate = models.DecimalField(max_digits=12, decimal_places=6, default="1.500000")
    margin_multiplier = models.DecimalField(max_digits=8, decimal_places=6, default="1.300000")
    aud_per_roo = models.DecimalField(max_digits=12, decimal_places=6, default="1.000000")
    input_tokens = models.PositiveBigIntegerField(default=0)
    cached_input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    provider_request_id = models.CharField(max_length=255, blank=True)
    trace_id = models.CharField(max_length=255, blank=True)
    # Store only a one-way digest of the gateway-generated owner nonce. The
    # nonce binds admission, dispatch-start, and every accounting report to the
    # same request handler without becoming another reusable credential at rest.
    dispatch_owner_hash = models.CharField(max_length=64)
    dispatch_lease_expires_at = models.DateTimeField(db_index=True)
    dispatch_started_at = models.DateTimeField(blank=True, null=True)
    failure_reason = models.CharField(max_length=500, blank=True)
    reconcile_after = models.DateTimeField(blank=True, null=True)
    ledger_entry = models.OneToOneField(
        Ledger,
        on_delete=models.PROTECT,
        related_name="coding_model_call",
        blank=True,
        null=True,
    )
    reserved_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("reserved_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("turn", "call_id"),
                name="roo_coding_call_turn_call_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "reconcile_after"), name="roo_coding_call_status_age_idx"),
            models.Index(
                fields=("status", "dispatch_lease_expires_at"),
                name="roo_coding_call_dispatch_idx",
            ),
        ]


class BoostPostAdmission(models.Model):
    """Durable, idempotent charge decision for one Slack boost root post."""

    STATUS_CHOICES = (
        ('processing', 'Processing'),
        ('approved', 'Approved'),
        ('insufficient_points', 'Insufficient points'),
        ('member_unlinked', 'Member unlinked'),
        ('invalid_post', 'Invalid post'),
    )

    submission_key = models.CharField(max_length=255, unique=True)
    workspace_id = models.CharField(max_length=50)
    channel_id = models.CharField(max_length=50)
    root_message_ts = models.CharField(max_length=50)
    poster_slack_id = models.CharField(max_length=50, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='boost_post_admissions',
    )
    root_text = models.TextField(blank=True, default='')
    social_post_url = models.URLField(max_length=2048)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, db_index=True)
    base_cost_points = models.PositiveIntegerField(default=8)
    charged_points = models.PositiveIntegerField(null=True, blank=True)
    discount_applied = models.BooleanField(default=False)
    balance_before = models.IntegerField(null=True, blank=True)
    new_balance = models.IntegerField(null=True, blank=True)
    rejection_message = models.TextField(blank=True, default='')
    ledger_entry = models.OneToOneField(
        Ledger,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='boost_post_admission',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['channel_id', 'root_message_ts'],
                name='uniq_boost_post_slack_root',
            ),
        ]
        indexes = [
            models.Index(
                fields=['poster_slack_id', 'created_at'],
                name='roo_boost_poster_created_idx',
            ),
        ]

    def __str__(self):
        return f"{self.poster_slack_id} {self.channel_id}:{self.root_message_ts} ({self.status})"


class CoworkingDayCapacity(models.Model):
    """
    Capacity override for specific dates.
    If no row exists for a date, use DEFAULT_COWORKING_CAPACITY from settings.
    """
    date = models.DateField(primary_key=True)
    capacity = models.IntegerField()
    notes = models.TextField(blank=True, null=True, help_text="Reason for capacity change")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Coworking Day Capacity"
        verbose_name_plural = "Coworking Day Capacities"
        ordering = ['date']

    def __str__(self):
        return f"{self.date}: {self.capacity} slots"


class CoworkingBooking(models.Model):
    """
    Coworking space bookings - auto-deducts points on booking.
    """
    STATUS_CHOICES = (
        ('booked', 'Booked'),
        ('cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='coworking_bookings')
    date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='booked')
    points_cost = models.IntegerField()
    ledger_entry = models.ForeignKey(
        Ledger, 
        on_delete=models.SET_NULL, 
        blank=True, null=True,
        related_name='coworking_booking'
    )
    refund_ledger_entry = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True, null=True,
        related_name='coworking_refund'
    )
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "Coworking Booking"
        verbose_name_plural = "Coworking Bookings"
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date', 'status'], name='roo_cowork_date_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date'],
                condition=models.Q(status='booked'),
                name='unique_active_booking_per_user_date'
            )
        ]

    def __str__(self):
        return f"{self.user.email} - {self.date} ({self.status})"


class CoworkingBookingOperation(models.Model):
    """Durable mapping from a caller operation to its committed response.

    The booking itself is mutable (it can later be cancelled), while a retry
    must recover the result that originally committed.  Keeping the operation
    receipt separately prevents a lost-response retry from creating a new
    booking after cancellation or after time/authorization has moved on.
    """

    KIND_CHOICES = (
        ('single', 'Single'),
        ('batch', 'Batch'),
    )

    id = models.UUIDField(primary_key=True, editable=False)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    request_fingerprint = models.CharField(max_length=64)
    response_payload = models.JSONField()
    http_status = models.PositiveSmallIntegerField(default=200)
    subjects = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='coworking_booking_operations',
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Coworking booking operation"
        verbose_name_plural = "Coworking booking operations"
        ordering = ['-created_at']


class MeetingRoom(models.Model):
    """A reservable room managed by Roo."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return self.name


class MeetingRoomBooking(models.Model):
    """An exclusive, points-backed reservation for a meeting room."""

    STATUS_CHOICES = (
        ('booked', 'Booked'),
        ('cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        MeetingRoom,
        on_delete=models.PROTECT,
        related_name='bookings',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='meeting_room_bookings',
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='booked')
    points_cost = models.PositiveIntegerField()
    purchased_points_cost = models.PositiveIntegerField(blank=True, null=True)
    purchased_points_cost_microroo = models.PositiveBigIntegerField(default=0)
    client_request_id = models.UUIDField(unique=True)
    ledger_entry = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='meeting_room_booking',
    )
    refund_ledger_entry = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='meeting_room_refund',
    )
    requested_by_slack_id = models.CharField(max_length=50)
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['starts_at', 'id']
        indexes = [
            models.Index(
                fields=['room', 'status', 'starts_at', 'ends_at'],
                name='roo_room_time_status_idx',
            ),
            models.Index(
                fields=['user', 'status', 'starts_at'],
                name='roo_room_user_start_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(ends_at__gt=models.F('starts_at')),
                name='meeting_room_booking_end_after_start',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(purchased_points_cost__isnull=True)
                    | models.Q(purchased_points_cost__lte=models.F('points_cost'))
                ),
                name='meeting_room_purchased_cost_lte_total',
            ),
            models.CheckConstraint(
                check=models.Q(
                    purchased_points_cost_microroo__lte=(
                        models.F('points_cost') * 1_000_000
                    )
                ),
                name='meeting_room_purchased_micro_lte_total',
            ),
        ]

    def __str__(self):
        return f"{self.room.name}: {self.starts_at} - {self.ends_at}"


class MeetingRoomBlock(models.Model):
    """An operations-managed period when a meeting room is unavailable."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        MeetingRoom,
        on_delete=models.CASCADE,
        related_name='blocks',
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['starts_at', 'id']
        indexes = [
            models.Index(
                fields=['room', 'starts_at', 'ends_at'],
                name='roo_room_block_time_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(ends_at__gt=models.F('starts_at')),
                name='meeting_room_block_end_after_start',
            ),
        ]

    def clean(self):
        super().clean()
        invalid_interval = not self.starts_at or not self.ends_at
        if not invalid_interval:
            invalid_interval = (
                self.ends_at.astimezone(datetime_timezone.utc)
                <= self.starts_at.astimezone(datetime_timezone.utc)
            )
        if invalid_interval:
            raise ValidationError({'ends_at': 'End time must be after start time.'})
        if MeetingRoomBooking.objects.filter(
            room=self.room,
            status='booked',
            starts_at__lt=self.ends_at,
            ends_at__gt=self.starts_at,
        ).exists():
            raise ValidationError(
                'This block overlaps an active meeting-room booking.'
            )

    def save(self, *args, **kwargs):
        from .meeting_rooms import MeetingRoomService

        self.full_clean()
        with transaction.atomic():
            MeetingRoomService.lock_room_interval(
                room=self.room,
                starts_at=self.starts_at,
                ends_at=self.ends_at,
            )
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.room.name}: unavailable {self.starts_at} - {self.ends_at}"


class RewardsCatalog(models.Model):
    """
    Available rewards that can be redeemed with points.
    """
    FULFILLMENT_CHOICES = (
        ('auto', 'Auto'),
        ('manual', 'Manual'),
    )

    code = models.CharField(max_length=50, primary_key=True, help_text="Unique reward code e.g. HOTDESK_DAY")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    cost_points = models.IntegerField()
    fulfillment = models.CharField(max_length=10, choices=FULFILLMENT_CHOICES, default='manual')
    is_active = models.BooleanField(default=True)
    max_per_user = models.IntegerField(null=True, blank=True, help_text="Max redemptions per user (null=unlimited)")
    stock_remaining = models.IntegerField(null=True, blank=True, help_text="Total stock available. Null = infinite")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Reward"
        verbose_name_plural = "Rewards Catalog"
        ordering = ['cost_points']

    def __str__(self):
        return f"{self.name} ({self.cost_points} pts)"


class RewardRedemption(models.Model):
    """
    Reward redemption requests and their status.
    """
    STATUS_CHOICES = (
        ('requested', 'Requested'),
        ('approved', 'Approved'),
        ('fulfilled', 'Fulfilled'),
        ('declined', 'Declined'),
        ('cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reward_redemptions')
    reward = models.ForeignKey(RewardsCatalog, on_delete=models.CASCADE, related_name='redemptions')
    quantity = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested')
    requested_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    fulfilled_at = models.DateTimeField(blank=True, null=True)
    approved_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    ledger_entry = models.ForeignKey(
        Ledger, 
        on_delete=models.SET_NULL, 
        blank=True, null=True,
        related_name='reward_redemption'
    )
    notes = models.TextField(blank=True, null=True, help_text="User notes or admin fulfillment notes")
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    slack_thread_ts = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        verbose_name = "Reward Redemption"
        verbose_name_plural = "Reward Redemptions"
        ordering = ['-requested_at']

    def __str__(self):
        return f"{self.user.email} - {self.reward.name} x{self.quantity} ({self.status})"


class PointsRequest(models.Model):
    """
    Pending points requests created in Slack and later approved by an admin.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    )

    requester_slack_id = models.CharField(max_length=50)
    target_slack_id = models.CharField(max_length=50)
    points = models.IntegerField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    approved_by_slack_id = models.CharField(max_length=50, blank=True, null=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    ledger_entry = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='points_requests',
    )
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    slack_thread_ts = models.CharField(max_length=50, blank=True, null=True)
    slack_summary_message_ts = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Points Request"
        verbose_name_plural = "Points Requests"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at'], name='roo_pointsr_status_8f1eab_idx'),
            models.Index(fields=['slack_channel_id', 'slack_summary_message_ts'], name='roo_pointsr_slack_c7c99b_idx'),
        ]

    def __str__(self):
        return (
            f"{self.requester_slack_id} requested {self.points} pts for "
            f"{self.target_slack_id} ({self.status})"
        )


class ChannelFirstPost(models.Model):
    """
    Track first posts in channels for point awards.
    """
    slack_user_id = models.CharField(max_length=50)
    channel_id = models.CharField(max_length=50)
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Channel First Post"
        verbose_name_plural = "Channel First Posts"
        unique_together = ('slack_user_id', 'channel_id')

    def __str__(self):
        return f"{self.slack_user_id} in {self.channel_id}"


class QuestProgress(models.Model):
    """
    Track quest progress for gamification.
    One row per user per quest.
    """
    slack_user_id = models.CharField(max_length=50)
    quest_id = models.CharField(max_length=50)
    current_count = models.IntegerField(default=0)
    completed = models.BooleanField(default=False)
    first_progress_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Quest Progress"
        verbose_name_plural = "Quest Progress"
        unique_together = ('slack_user_id', 'quest_id')
        indexes = [
            models.Index(fields=['slack_user_id']),
            models.Index(fields=['slack_user_id', 'completed']),
        ]

    def __str__(self):
        status = "✓" if self.completed else f"{self.current_count}"
        return f"{self.slack_user_id} - {self.quest_id}: {status}"
