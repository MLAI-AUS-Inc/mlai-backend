"""Mentor profiles, weekly availability and paid mentor session requests.

Points are never stored here. Every movement goes through roo.PointsService and
the resulting Ledger rows are linked from MentorSession.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

DEFAULT_PRICE_ROO = 40
MIN_SESSION_MINUTES = 15
MAX_SESSION_MINUTES = 120

LOCATION_MLAI_SPACE = 'mlai_space'
LOCATION_VIDEO = 'video'
LOCATION_CHOICES = (
    (LOCATION_MLAI_SPACE, 'MLAI space'),
    (LOCATION_VIDEO, 'Video'),
)


class MentorProfile(models.Model):
    """A mentor. Only an admin can set is_active; mentors edit the rest."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mentor_profile'
    )
    is_active = models.BooleanField(
        default=False,
        help_text='Set by an admin. Inactive mentors are hidden and cannot be booked.',
    )
    headline = models.CharField(max_length=140, blank=True, default='')
    bio = models.TextField(blank=True, default='', max_length=2000)
    topics = models.JSONField(default=list, blank=True)
    timezone = models.CharField(max_length=64, default='Australia/Melbourne')
    away_dates = models.JSONField(
        default=list, blank=True,
        help_text='List of {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} local date ranges with no bookings.',
    )
    session_minutes = models.PositiveSmallIntegerField(default=30)
    price_roo = models.PositiveIntegerField(default=DEFAULT_PRICE_ROO)
    location_type = models.CharField(
        max_length=16, choices=LOCATION_CHOICES, default=LOCATION_VIDEO
    )
    busy_calendar_email = models.EmailField(
        max_length=254, blank=True, default='',
        help_text=(
            'Email of the Google Calendar the mentor shared free/busy of with the MLAI '
            'mentoring account. Optional; busy times are hidden from the slot list.'
        ),
    )
    space_notes = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('user__first_name', 'user__last_name', 'pk')

    def __str__(self):
        return f'Mentor {self.user_id}'


class MentorAvailabilityRule(models.Model):
    """One weekly window in the mentor's own timezone (weekday 0 = Monday)."""

    mentor = models.ForeignKey(
        MentorProfile, on_delete=models.CASCADE, related_name='availability_rules'
    )
    weekday = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ('weekday', 'start_time')
        constraints = [
            models.CheckConstraint(
                condition=Q(weekday__lte=6), name='mentor_rule_weekday_range'
            ),
            models.CheckConstraint(
                condition=Q(end_time__gt=F('start_time')), name='mentor_rule_end_after_start'
            ),
        ]


class MentorSession(models.Model):
    STATUS_REQUESTED = 'requested'
    STATUS_ACCEPTED = 'accepted'
    STATUS_DECLINED = 'declined'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = (
        (STATUS_REQUESTED, 'Requested'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_DECLINED, 'Declined'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_COMPLETED, 'Completed'),
    )
    ACTIVE_STATUSES = (STATUS_REQUESTED, STATUS_ACCEPTED)

    MEETING_PENDING = 'pending'
    MEETING_CREATED = 'created'
    MEETING_FAILED = 'failed'
    MEETING_UNAVAILABLE = 'unavailable'
    MEETING_CHOICES = (
        (MEETING_PENDING, 'Pending'),
        (MEETING_CREATED, 'Created'),
        (MEETING_FAILED, 'Failed'),
        (MEETING_UNAVAILABLE, 'Unavailable'),
    )

    NOTIFY_PENDING = 'pending'
    NOTIFY_SENT = 'sent'
    NOTIFY_FAILED = 'failed'
    NOTIFY_SKIPPED = 'skipped'
    NOTIFY_CHOICES = (
        (NOTIFY_PENDING, 'Pending'),
        (NOTIFY_SENT, 'Sent'),
        (NOTIFY_FAILED, 'Failed'),
        (NOTIFY_SKIPPED, 'Skipped'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mentor = models.ForeignKey(
        MentorProfile, on_delete=models.CASCADE, related_name='sessions'
    )
    founder = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mentor_sessions'
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_REQUESTED
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()

    # Snapshots taken at request time so later profile edits change nothing.
    price_roo = models.PositiveIntegerField()
    # Set when the founder is charged (on accept); needed for an exact refund split.
    purchased_points_cost_microroo = models.BigIntegerField(default=0)
    location_type = models.CharField(max_length=16, choices=LOCATION_CHOICES)
    location_detail = models.CharField(max_length=500, blank=True, default='')

    brief_stage = models.CharField(max_length=200)
    brief_struggle = models.CharField(max_length=1000)
    brief_outcome = models.CharField(max_length=1000)
    brief_link = models.URLField(max_length=500, blank=True, default='')

    client_request_id = models.UUIDField()

    charge_ledger_entry = models.ForeignKey(
        'roo.Ledger', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    release_ledger_entry = models.ForeignKey(
        'roo.Ledger', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    payout_ledger_entry = models.ForeignKey(
        'roo.Ledger', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    requested_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    # Google Meet link, created by the MLAI organiser account when the mentor
    # accepts a video session. A failure here never changes status.
    meet_link = models.URLField(max_length=500, blank=True, default='')
    calendar_event_id = models.CharField(max_length=255, blank=True, default='')
    meeting_status = models.CharField(
        max_length=12, choices=MEETING_CHOICES, default=MEETING_PENDING
    )
    meeting_error = models.CharField(max_length=40, blank=True, default='')
    meeting_path = models.CharField(
        max_length=16, blank=True, default='',
        help_text='Which Google path made the link: meet_api or calendar.',
    )

    # Delivery outcome is deliberately separate from status: a failed Slack DM
    # must never change the booking.
    notification_status = models.CharField(
        max_length=8, choices=NOTIFY_CHOICES, default=NOTIFY_PENDING
    )
    notification_attempted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('starts_at', 'pk')
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F('starts_at')), name='mentor_session_end_after_start'
            ),
            models.UniqueConstraint(
                fields=('founder', 'client_request_id'),
                name='mentor_session_unique_request',
            ),
        ]
        indexes = [
            models.Index(fields=('mentor', 'status', 'starts_at'), name='mentor_sess_mentor_idx'),
            models.Index(fields=('founder', 'status', 'starts_at'), name='mentor_sess_founder_idx'),
        ]
