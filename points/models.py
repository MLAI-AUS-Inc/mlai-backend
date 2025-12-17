from django.db import models
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Points Admin"
        verbose_name_plural = "Points Admins"

    def __str__(self):
        # Use linked user's name if available, otherwise fall back to Slack ID
        display_name = self.user.get_full_name() if self.user else self.slack_user_id
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
    lifetime_earned = models.IntegerField(default=0, help_text="Total points ever earned")
    lifetime_spent = models.IntegerField(default=0, help_text="Total points ever spent")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Points Account"
        verbose_name_plural = "Points Accounts"

    def __str__(self):
        return f"{self.user.email}: {self.balance} pts (earned: {self.lifetime_earned}, spent: {self.lifetime_spent})"


class Task(models.Model):
    """
    Tasks with points attached.
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

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    portfolio = models.CharField(max_length=50, choices=PORTFOLIO_CHOICES, default='events')
    points = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')

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
        return f"#{self.id} {self.title} ({self.points} pts)"


class TaskSubmission(models.Model):
    """
    Submission records for tasks - tracks the submit → approve workflow.
    """
    STATUS_CHOICES = (
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='submissions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='task_submissions')
    submission_text = models.TextField(help_text="Description of work completed")
    submission_url = models.URLField(blank=True, null=True, help_text="Link to proof of work")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='submitted')
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
        ('TOOLS', 'Tools'),
        ('DONATION', 'Donation'),
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
