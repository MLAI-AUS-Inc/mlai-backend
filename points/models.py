from django.db import models
from django.conf import settings
import uuid

class Minter(models.Model):
    """
    Users authorized to mint and approve tasks.
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
        # Add more as needed
    )

    slack_user_id = models.CharField(max_length=50, unique=True, help_text="Slack User ID (e.g. U123ABC)")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='minter_profile')
    name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default='committee')
    portfolio = models.CharField(max_length=50, choices=PORTFOLIO_CHOICES, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.slack_user_id}) - {self.role}"

class Task(models.Model):
    """
    Tasks with points attached.
    """
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('claimed', 'Claimed'),
        ('pending_approval', 'Pending Approval'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled'),
    )
    
    # Portfolios allowed for filtering/categorization
    PORTFOLIO_CHOICES = Minter.PORTFOLIO_CHOICES

    id = models.AutoField(primary_key=True) # Explicit ID (or UUID if preferred, sticking to int for simple #ID ref)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    portfolio = models.CharField(max_length=50, choices=PORTFOLIO_CHOICES, default='events')
    points = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')

    # Slack Integrations
    created_by_user_id = models.CharField(max_length=50, help_text="Slack ID of minter")
    assigned_to_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="Slack ID of volunteer")
    closed_by_user_id = models.CharField(max_length=50, blank=True, null=True, help_text="Slack ID of approver")
    
    slack_channel_id = models.CharField(max_length=50, blank=True, null=True)
    slack_thread_ts = models.CharField(max_length=50, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"#{self.id} {self.title} ({self.points} pts)"

class Ledger(models.Model):
    """
    Ledger of points awarded.
    """
    slack_user_id = models.CharField(max_length=50, help_text="User receiving points")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='ledger_entries')
    points_delta = models.IntegerField(help_text="Points awarded (can be negative for corrections)")
    reason = models.TextField(blank=True)
    
    created_by_user_id = models.CharField(max_length=50, help_text="Slack ID of who awarded points")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.points_delta} pts to {self.slack_user_id} for Task #{self.task.id}"
