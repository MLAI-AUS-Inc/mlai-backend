"""Mentor profiles, weekly availability and paid mentor session requests.

Points are never stored here. Every movement goes through roo.PointsService and
the resulting Ledger rows are linked from MentorSession.
"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

# Stored model defaults (price 40, session length 15). They stay as they are on purpose:
# changing a field default needs a migration. New mentors get the values below instead,
# applied in the application flow and the admin add form.
DEFAULT_PRICE_ROO = 40
NEW_MENTOR_PRICE_ROO = 30
NEW_MENTOR_SESSION_MINUTES = 30
MIN_SESSION_MINUTES = 15
MAX_SESSION_MINUTES = 60

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
    session_minutes = models.PositiveSmallIntegerField(default=15)
    price_roo = models.PositiveIntegerField(default=DEFAULT_PRICE_ROO)
    # Staff approval stays separate from the mentor's explicit publication choice.
    accepting_bookings = models.BooleanField(default=False)
    booking_mode = models.CharField(max_length=16, default='legacy_request')
    buffer_before_minutes = models.PositiveSmallIntegerField(default=15)
    buffer_after_minutes = models.PositiveSmallIntegerField(default=15)
    minimum_notice_minutes = models.PositiveIntegerField(default=240)
    booking_horizon_days = models.PositiveSmallIntegerField(default=30)
    start_increment_minutes = models.PositiveSmallIntegerField(default=15)
    booking_revision = models.PositiveIntegerField(default=1)
    meeting_artifact_policy = models.JSONField(default=dict, blank=True)
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
    brief_questions = models.JSONField(
        default=list, blank=True,
        help_text=(
            'Booking questions as [{"id", "text", "required"}]. Empty means the default '
            'four questions.'
        ),
    )
    # Staff review trail, written by the Mentoring admin screen. An application is
    # pending while none of approved_at, rejected_at and paused_at is set.
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    rejected_at = models.DateTimeField(null=True, blank=True, editable=False)
    paused_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('user__first_name', 'user__last_name', 'pk')

    def __str__(self):
        return f'Mentor {self.user_id}'


INVITE_TTL_DAYS = 14
# Slack-imported people who never signed up get this address. Nobody can sign in
# with it, so an invite to one can never be used.
PLACEHOLDER_EMAIL_SUFFIX = '@slack.placeholder.com'


def invite_link(token):
    base = str(getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', '')).rstrip('/')
    return f'{base}/mentoring/apply?mentor_invite={token}'


def hash_invite_token(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def is_placeholder_email(email):
    return str(email or '').strip().lower().endswith(PLACEHOLDER_EMAIL_SUFFIX)


def mask_email(email):
    """``s•••@example.com``: enough for the invited person to recognise it, no more."""
    local, _, domain = str(email or '').partition('@')
    if not local or not domain:
        return ''
    return f'{local[0]}\u2022\u2022\u2022@{domain}'


class MentorInvite(models.Model):
    """A single-use, expiring invitation for one person to apply as a mentor.

    The invite names an existing user, or only an email address for someone who
    has no account yet. An address-only invite is bound to the account that signs
    in with that address the first time the link is opened. Only the SHA-256 of
    the token is stored; the plaintext goes into the invite email or is shown to
    staff once.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='mentor_invites',
    )
    email = models.EmailField(max_length=320, blank=True, default='')
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', editable=False,
    )
    # The latest invite email: when, and its system_email.SystemEmail row (no FK,
    # so the apps stay independent). send_count also numbers the idempotency key.
    emailed_at = models.DateTimeField(null=True, blank=True)
    email_log_id = models.UUIDField(null=True, blank=True)
    send_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [
            models.Index(fields=('user', 'used_at'), name='mentor_invite_user_idx'),
            models.Index(fields=('email',), name='mentor_invite_email_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(user__isnull=False) | ~Q(email=''),
                name='mentor_invite_has_target',
            ),
        ]

    def __str__(self):
        if self.user_id:
            return f'Mentor invite for user {self.user_id}'
        return f'Mentor invite {self.pk} by email'

    @classmethod
    def issue(cls, user=None, created_by=None, ttl_days=INVITE_TTL_DAYS, email=''):
        """Create an invite and return (invite, plaintext token)."""
        address = str(email or getattr(user, 'email', '') or '').strip().lower()
        if user is None and not address:
            raise ValueError('An invite needs a user or an email address.')
        token = secrets.token_urlsafe(32)
        invite = cls.objects.create(
            user=user,
            email=address,
            token_hash=hash_invite_token(token),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )
        return invite, token

    def rotate(self, ttl_days=INVITE_TTL_DAYS):
        """A new token and a fresh expiry; the old link stops working. Caller saves."""
        token = secrets.token_urlsafe(32)
        self.token_hash = hash_invite_token(token)
        self.expires_at = timezone.now() + timedelta(days=ttl_days)
        return token


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
    STATUS_PROVISIONING = 'provisioning'
    STATUS_REQUESTED = 'requested'
    STATUS_ACCEPTED = 'accepted'
    STATUS_DECLINED = 'declined'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = (
        (STATUS_PROVISIONING, 'Confirming'),
        (STATUS_REQUESTED, 'Requested'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_DECLINED, 'Declined'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_COMPLETED, 'Completed'),
    )
    ACTIVE_STATUSES = (STATUS_PROVISIONING, STATUS_REQUESTED, STATUS_ACCEPTED)

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

    booking_mode = models.CharField(max_length=16, default='legacy_request')
    buffer_before_minutes = models.PositiveSmallIntegerField(default=0)
    buffer_after_minutes = models.PositiveSmallIntegerField(default=0)
    calendar_snapshot = models.JSONField(default=dict, blank=True)
    booking_snapshot = models.JSONField(default=dict, blank=True)
    meeting_provisioning = models.JSONField(default=dict, blank=True)
    failure_code = models.CharField(max_length=64, default='', blank=True)

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
    # Snapshot taken at request time: [{"id", "question", "required", "answer"}].
    # Empty on sessions made before per-mentor questions; those use the four
    # fields above.
    brief_answers = models.JSONField(default=list, blank=True)

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
    # When a meeting attempt was claimed. A pending claim older than the service
    # timeout is stale (the process died) and may be reclaimed by a retry.
    meeting_claimed_at = models.DateTimeField(null=True, blank=True)
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


# Register the mentoring-owned personal calendar model.
from .calendar_models import MentorCalendarConnection  # noqa: E402,F401


class MentorBookingAttempt(models.Model):
    """Durable provider-write outbox and provisional-debit recovery state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(MentorSession, on_delete=models.PROTECT, related_name='booking_attempt')
    payload_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='provisioning')
    deadline = models.DateTimeField()
    generation = models.PositiveIntegerField(default=1)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    provider_started = models.BooleanField(default=False)
    cleanup_pending = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=('status', 'next_attempt_at'), name='mentor_attempt_due_idx')]


class MentorTimeReservation(models.Model):
    """A principal's protected UTC interval; PostgreSQL excludes active overlaps.

    The reviewed schema migration installs the vendor-specific btree_gist range
    exclusion in addition to these portable checks. One merged interval per
    session/principal prevents a host also attending another meeting.
    """

    session = models.ForeignKey(MentorSession, on_delete=models.PROTECT, related_name='time_reservations')
    principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='mentor_reservation_end_after_start'),
            models.UniqueConstraint(fields=('session', 'principal'), name='mentor_reservation_principal_unique'),
        ]
        indexes = [models.Index(fields=('principal', 'active', 'starts_at'), name='mentor_reservation_user_idx')]


class MentorSessionFeedback(models.Model):
    """One person's rating of one session, asked for just after it ends.

    The founder gets one email and one Slack DM (Roo) a few minutes after the
    booked end, with three faces. A rating can come from either, or from MLAI
    Chat; the latest one wins and earlier ones stay in ``rating_history``.
    Delivery outcomes live here only: they never change the session.
    The links carry an HMAC token derived from ``id`` (mentoring.feedback), so
    no token is stored.
    """

    ROLE_FOUNDER = 'founder'
    ROLE_CHOICES = ((ROLE_FOUNDER, 'Founder'),)

    RATING_SAD = 'sad'
    RATING_NEUTRAL = 'neutral'
    RATING_HAPPY = 'happy'
    RATING_NO_SHOW = 'no_show'
    RATING_CHOICES = (
        (RATING_SAD, 'Not great'),
        (RATING_NEUTRAL, 'Okay'),
        (RATING_HAPPY, 'Great'),
        (RATING_NO_SHOW, "Didn't happen"),
    )
    NEGATIVE_RATINGS = (RATING_SAD, RATING_NO_SHOW)

    VIA_EMAIL = 'email'
    VIA_SLACK = 'slack'
    VIA_APP = 'app'
    VIA_PAGE = 'page'
    VIA_CHOICES = ((VIA_EMAIL, 'Email'), (VIA_SLACK, 'Slack'), (VIA_APP, 'MLAI Chat'), (VIA_PAGE, 'Feedback page'))

    DELIVERY_PENDING = 'pending'
    DELIVERY_SENDING = 'sending'
    DELIVERY_SENT = 'sent'
    DELIVERY_FAILED = 'failed'
    DELIVERY_SUPPRESSED = 'suppressed'
    DELIVERY_SKIPPED = 'skipped'
    DELIVERY_OPTED_OUT = 'opted_out'
    DELIVERY_CHOICES = (
        (DELIVERY_PENDING, 'Pending'),
        (DELIVERY_SENDING, 'Sending'),
        (DELIVERY_SENT, 'Sent'),
        (DELIVERY_FAILED, 'Failed'),
        (DELIVERY_SUPPRESSED, 'Suppressed'),
        (DELIVERY_SKIPPED, 'Skipped'),
        (DELIVERY_OPTED_OUT, 'Opted out'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(MentorSession, on_delete=models.CASCADE, related_name='feedback')
    respondent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mentor_session_feedback'
    )
    role = models.CharField(max_length=8, choices=ROLE_CHOICES, default=ROLE_FOUNDER)
    token_expires_at = models.DateTimeField()

    rating = models.CharField(max_length=8, choices=RATING_CHOICES, blank=True, default='')
    rated_at = models.DateTimeField(null=True, blank=True)
    rated_via = models.CharField(max_length=8, choices=VIA_CHOICES, blank=True, default='')
    rating_history = models.JSONField(default=list, blank=True)
    comment = models.TextField(blank=True, default='', max_length=2000)
    commented_at = models.DateTimeField(null=True, blank=True)
    # Three different ratings within seconds of the send look like a mail
    # scanner opening every link. Kept, but not alerted on and shown greyed.
    suspect_scanner = models.BooleanField(default=False)

    sent_at = models.DateTimeField(null=True, blank=True)
    email_status = models.CharField(max_length=10, choices=DELIVERY_CHOICES, default=DELIVERY_PENDING)
    email_log_id = models.UUIDField(null=True, blank=True)
    email_error = models.CharField(max_length=64, blank=True, default='')
    slack_status = models.CharField(max_length=10, choices=DELIVERY_CHOICES, default=DELIVERY_PENDING)
    slack_channel = models.CharField(max_length=32, blank=True, default='')
    slack_ts = models.CharField(max_length=32, blank=True, default='')
    alert_ts = models.CharField(max_length=32, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at', 'pk')
        constraints = [
            models.UniqueConstraint(
                fields=('session', 'respondent'), name='mentor_feedback_one_per_person',
            ),
        ]
        indexes = [
            models.Index(fields=('rating', 'rated_at'), name='mentor_feedback_rating_idx'),
            models.Index(fields=('email_status', 'slack_status', 'created_at'), name='mentor_feedback_delivery_idx'),
        ]

    def __str__(self):
        return f'Feedback {self.pk} on session {self.session_id}'


class MentorFeedbackOptOut(models.Model):
    """The person asked for no more session feedback emails (Slack DMs still go)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mentor_feedback_opt_out'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Feedback email opt-out for user {self.user_id}'
