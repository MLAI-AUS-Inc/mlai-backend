from django.db import models
from django.db.models import Q
from django.conf import settings
import uuid


class PointsAdmin(models.Model):
    """
    Users authorized to mint and approve tasks.
    Formerly known as Minter - renamed for clarity.
    """
    ROLE_CHOICES = (
        ('committee', 'Committee'),
        ('portfolio_lead', 'Portfolio Lead'),
        ('admin', 'Admin'),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Points Account"
        verbose_name_plural = "Points Accounts"

    def __str__(self):
        return f"{self.user.email}: {self.balance} pts (earned: {self.lifetime_earned}, spent: {self.lifetime_spent})"


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
        ('EVENT', 'Event'),
        ('MERCH', 'Merch'),
        ('CONTENT_FACTORY', 'Content Factory'),
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
        if not self.description and self.reason:
            self.description = self.reason
        if not self.created_by_slack_id and self.created_by_user_id:
            self.created_by_slack_id = self.created_by_user_id
        super().save(*args, **kwargs)


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
