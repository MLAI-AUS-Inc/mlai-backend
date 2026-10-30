"""MLAI's contact store: Luma guests, imported opt-ins and message blasts.

mlai-backend is the source of truth for every person and their consent.
listmonk only receives what it needs to deliver a campaign. All rows belong to
MLAI's own community; there is no per-organisation tenancy in this version.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class Channel(models.TextChoices):
    EMAIL = "email", "Email"
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"


class TimestampedRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CrmEvent(TimestampedRecord):
    """One MLAI calendar event; founder connections keep LumaEventSelection."""

    luma_event_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=500)
    url = models.URLField(max_length=2048, blank=True)
    cover_url = models.URLField(max_length=2048, blank=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    visibility = models.CharField(max_length=32, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    guest_count = models.PositiveIntegerField(default=0)
    approved_count = models.PositiveIntegerField(default=0)
    waitlist_count = models.PositiveIntegerField(default=0)
    checked_in_count = models.PositiveIntegerField(default=0)
    guests_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-start_at",)
        indexes = [
            models.Index(fields=("start_at",), name="crm_event_start_idx"),
            models.Index(fields=("guests_synced_at",), name="crm_event_guest_sync_idx"),
        ]

    def __str__(self):
        return self.name


class Contact(TimestampedRecord):
    """One person, keyed by lowercase email; manual edits outrank synced data."""

    class Source(models.TextChoices):
        LUMA = "luma", "Luma"
        IMPORT = "import", "CSV import"
        MANUAL = "manual", "Added by hand"
        CHAT_ACCOUNT = "chat_account", "MLAI Chat account"

    class EmailStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Marked as spam"

    class MarketingConsent(models.TextChoices):
        # Inferred consent (recent attendance) is computed at send time and
        # never stored, so it lapses without a job.
        UNKNOWN = "unknown", "Unknown"
        GRANTED = "granted", "Granted"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class SmsStatus(models.TextChoices):
        NONE = "none", "No mobile number"
        ACTIVE = "active", "Can receive texts"
        OPTED_OUT = "opted_out", "Opted out"
        INVALID = "invalid", "Number can't receive texts"

    class WhatsAppOptIn(models.TextChoices):
        # A phone number alone is never WhatsApp opt-in.
        UNKNOWN = "unknown", "Not asked"
        GRANTED = "granted", "Opted in"
        WITHDRAWN = "withdrawn", "Opted out"

    email = models.EmailField(max_length=254, unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    display_name = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    phone_e164 = models.CharField(max_length=16, blank=True)
    company = models.CharField(max_length=300, blank=True)
    job_title = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=120, blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)
    tags = models.JSONField(default=list, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_contacts",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    email_status = models.CharField(
        max_length=16, choices=EmailStatus.choices, default=EmailStatus.ACTIVE
    )
    email_status_changed_at = models.DateTimeField(null=True, blank=True)
    marketing_consent = models.CharField(
        max_length=16, choices=MarketingConsent.choices, default=MarketingConsent.UNKNOWN
    )
    marketing_consent_source = models.CharField(max_length=255, blank=True)
    marketing_consent_at = models.DateTimeField(null=True, blank=True)
    sms_status = models.CharField(
        max_length=16, choices=SmsStatus.choices, default=SmsStatus.NONE
    )
    sms_status_changed_at = models.DateTimeField(null=True, blank=True)
    whatsapp_opt_in = models.CharField(
        max_length=16, choices=WhatsAppOptIn.choices, default=WhatsAppOptIn.UNKNOWN
    )
    whatsapp_opt_in_source = models.CharField(max_length=255, blank=True)
    whatsapp_opt_in_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_attended_at = models.DateTimeField(null=True, blank=True)
    events_registered_count = models.PositiveIntegerField(default=0)
    events_attended_count = models.PositiveIntegerField(default=0)
    listmonk_subscriber_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="crm_contact_email_lowercase"
            ),
        ]
        indexes = [
            models.Index(
                fields=("email_status", "marketing_consent"), name="crm_contact_sendable_idx"
            ),
            models.Index(fields=("last_attended_at",), name="crm_contact_attended_idx"),
            models.Index(fields=("phone_e164",), name="crm_contact_phone_idx"),
        ]

    def __str__(self):
        return self.email


class EventRegistration(TimestampedRecord):
    """A Luma guest record; removed guests are marked, never deleted."""

    event = models.ForeignKey(CrmEvent, on_delete=models.CASCADE, related_name="registrations")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="registrations")
    luma_guest_id = models.CharField(max_length=255, blank=True)
    approval_status = models.CharField(max_length=32, blank=True)
    registered_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    ticket_names = models.JSONField(default=list, blank=True)
    registration_answers = models.JSONField(default=list, blank=True)
    utm_source = models.CharField(max_length=255, blank=True)
    custom_source = models.CharField(max_length=255, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("event", "contact"), name="crm_registration_once"),
        ]
        indexes = [
            models.Index(fields=("contact", "checked_in_at"), name="crm_registration_contact_idx"),
            models.Index(fields=("event", "approval_status"), name="crm_registration_status_idx"),
        ]


class ContactConsentEvent(models.Model):
    """Append-only evidence for every consent or deliverability change."""

    class Kind(models.TextChoices):
        GRANTED = "granted", "Granted"
        WITHDRAWN = "withdrawn", "Withdrawn"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Marked as spam"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="consent_events")
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.EMAIL)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    source = models.CharField(max_length=255)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("contact", "created_at"), name="crm_consent_history_idx"),
        ]


class Segment(TimestampedRecord):
    """A saved audience; ``definition`` is the JSON rule set, never SQL."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    definition = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_count = models.PositiveIntegerField(null=True, blank=True)
    last_counted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class SenderMailbox(TimestampedRecord):
    """A From address and its daily cap; SMTP credentials live only in listmonk."""

    class Route(models.TextChoices):
        GOOGLE_RELAY = "google_relay", "Google Workspace SMTP relay"
        RESEND = "resend", "Resend"
        SES = "ses", "Amazon SES"

    email = models.EmailField(max_length=254, unique=True)
    display_name = models.CharField(max_length=200)
    domain = models.CharField(max_length=253)
    route = models.CharField(max_length=16, choices=Route.choices)
    reply_to = models.EmailField(max_length=254, blank=True)
    daily_limit = models.PositiveIntegerField(default=500)
    enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.display_name} <{self.email}>"


class MessageTemplate(TimestampedRecord):
    """An approved WhatsApp Content template; Twilio holds the wording."""

    class Category(models.TextChoices):
        UTILITY = "utility", "Utility"
        MARKETING = "marketing", "Marketing"
        AUTHENTICATION = "authentication", "Authentication"

    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WHATSAPP)
    name = models.CharField(max_length=200)
    content_sid = models.CharField(max_length=64, unique=True)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.UTILITY)
    body = models.TextField(blank=True)
    variables = models.JSONField(default=list, blank=True)
    approved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Blast(TimestampedRecord):
    """One send to many people over email, SMS and WhatsApp.

    Provider ids are stored before any status change so a retried dispatch
    reuses them instead of sending twice.
    """

    class Purpose(models.TextChoices):
        EVENT_UPDATE = "event_update", "Event update"
        MARKETING = "marketing", "Marketing"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        PARTIALLY_SENT = "partially_sent", "Partly sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    event = models.ForeignKey(
        CrmEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="blasts"
    )
    segment = models.ForeignKey(
        Segment, on_delete=models.SET_NULL, null=True, blank=True, related_name="blasts"
    )
    segment_definition = models.JSONField(default=dict, blank=True)
    quick_segment = models.CharField(max_length=32, blank=True)
    channels = models.JSONField(default=list, blank=True)
    mailbox = models.ForeignKey(
        SenderMailbox, on_delete=models.PROTECT, null=True, blank=True, related_name="blasts"
    )
    subject = models.CharField(max_length=300, blank=True)
    body_markdown = models.TextField(blank=True)
    sms_body = models.TextField(blank=True)
    sms_only_without_whatsapp = models.BooleanField(default=True)
    whatsapp_template = models.ForeignKey(
        MessageTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name="blasts"
    )
    whatsapp_variables = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    recipient_count = models.PositiveIntegerField(default=0)
    idempotency_key = models.CharField(max_length=64, blank=True)
    listmonk_list_id = models.PositiveIntegerField(null=True, blank=True)
    listmonk_campaign_id = models.PositiveIntegerField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("idempotency_key",),
                condition=~Q(idempotency_key=""),
                name="crm_blast_idempotency_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "queued_at"), name="crm_blast_queue_idx"),
        ]


class BlastDelivery(models.Model):
    """One message to one person on one channel, with its provider status."""

    class ConsentBasis(models.TextChoices):
        EVENT_REGISTRATION = "event_registration", "Registered for the event"
        EXPLICIT = "explicit", "Explicit opt-in"
        INFERRED_ATTENDANCE = "inferred_attendance", "Recent attendance"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SUBMITTING = "submitting", "Submitting"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"
        UNDELIVERED = "undelivered", "Undelivered"
        BOUNCED = "bounced", "Bounced"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        # The provider call timed out: it may or may not have been sent, so
        # a person decides rather than an automatic retry.
        UNKNOWN = "unknown", "Unknown"

    blast = models.ForeignKey(Blast, on_delete=models.CASCADE, related_name="deliveries")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="blast_deliveries")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    address = models.CharField(max_length=254)
    consent_basis = models.CharField(max_length=24, choices=ConsentBasis.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    provider_id = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    segments = models.PositiveSmallIntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=5, null=True, blank=True)
    price_unit = models.CharField(max_length=3, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    clicked_urls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("blast", "contact", "channel"), name="crm_delivery_once"
            ),
            models.UniqueConstraint(
                fields=("provider_id",),
                condition=~Q(provider_id=""),
                name="crm_delivery_provider_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("contact", "created_at"), name="crm_delivery_history_idx"),
            models.Index(fields=("channel", "created_at"), name="crm_delivery_channel_idx"),
        ]


class InboundMessage(models.Model):
    """A text or WhatsApp message to MLAI's number; opt-out keywords included."""

    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, null=True, blank=True, related_name="inbound_messages"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    from_address = models.CharField(max_length=32)
    body = models.TextField(blank=True)
    provider_id = models.CharField(max_length=64, unique=True)
    blast = models.ForeignKey(
        Blast, on_delete=models.SET_NULL, null=True, blank=True, related_name="replies"
    )
    received_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("contact", "received_at"), name="crm_inbound_contact_idx"),
            models.Index(fields=("blast", "received_at"), name="crm_inbound_blast_idx"),
        ]


class ContactImport(TimestampedRecord):
    """One CSV import; the file itself is never stored."""

    class Origin(models.TextChoices):
        COMMAND = "command", "Management command"
        UPLOAD = "upload", "Upload in MLAI Chat"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Consent(models.TextChoices):
        GRANTED = "granted", "Everyone in the file opted in"
        NONE = "none", "No consent recorded"

    file_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    consent = models.CharField(max_length=16, choices=Consent.choices)
    consent_source = models.CharField(max_length=255, blank=True)
    tags = models.JSONField(default=list, blank=True)
    dry_run = models.BooleanField(default=False)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.COMMAND)
    column_plan = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    rows_received = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    counts = models.JSONField(default=dict, blank=True)
    error_sample = models.JSONField(default=list, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class CrmAuditEvent(models.Model):
    """Append-only record of who read, changed or sent contact data."""

    class Action(models.TextChoices):
        SYNC = "sync", "Sync"
        IMPORT = "import", "Import"
        EDIT = "edit", "Edit"
        SEND = "send", "Send"
        EXPORT = "export", "Export"
        OPT_OUT = "opt_out", "Opt-out"
        DELETE = "delete", "Delete"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("action", "created_at"), name="crm_audit_action_idx"),
        ]
