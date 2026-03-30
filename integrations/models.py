from django.conf import settings
from django.db import models
from .fields import EncryptedTextField


class GmailRelevanceLabel(models.TextChoices):
    PENDING = "pending", "Pending"
    RELEVANT = "relevant", "Relevant"
    IRRELEVANT = "irrelevant", "Irrelevant"
    AMBIGUOUS = "ambiguous", "Ambiguous"


class ArtifactProcessingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    HYDRATED = "hydrated", "Hydrated"
    PROCESSED = "processed", "Processed"
    UNSUPPORTED = "unsupported", "Unsupported"
    ERROR = "error", "Error"


class StartupEventType(models.TextChoices):
    CUSTOMER_WIN = "customer_win", "Customer Win"
    CHURN_OR_RENEWAL = "churn_or_renewal", "Churn Or Renewal"
    FUNDRAISING = "fundraising", "Fundraising"
    PRODUCT_MILESTONE = "product_milestone", "Product Milestone"
    OUTAGE_OR_INCIDENT = "outage_or_incident", "Outage Or Incident"
    HIRING_OR_DEPARTURE = "hiring_or_departure", "Hiring Or Departure"
    PARTNERSHIP = "partnership", "Partnership"
    LEGAL_OR_COMPLIANCE = "legal_or_compliance", "Legal Or Compliance"
    BURN_OR_RUNWAY = "burn_or_runway", "Burn Or Runway"
    INVESTOR_ASK = "investor_ask", "Investor Ask"
    BOARD_MILESTONE = "board_milestone", "Board Milestone"


class StartupEventDatePrecision(models.TextChoices):
    DAY = "day", "Day"
    MONTH = "month", "Month"
    QUARTER = "quarter", "Quarter"
    UNKNOWN = "unknown", "Unknown"


class MonthlyUpdateDraftStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    NEEDS_REVIEW = "needs_review", "Needs Review"
    READY = "ready", "Ready"
    ERROR = "error", "Error"


class GroundednessStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed"
    NEEDS_REVIEW = "needs_review", "Needs Review"

class GoogleConnection(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='google_connection')
    google_email = models.EmailField()
    refresh_token = EncryptedTextField()  # encrypted at rest
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Google Connection for {self.user.email} ({self.google_email})"


class UserIntegration(models.Model):
    slack_user_id = models.TextField(primary_key=True, unique=True)
    github_access_token = EncryptedTextField(null=True, blank=True)
    github_refresh_token = EncryptedTextField(null=True, blank=True)
    github_token_expires_at = models.DateTimeField(null=True, blank=True)
    github_user_name = models.TextField(null=True, blank=True)
    github_repo = models.CharField(max_length=255, null=True, blank=True)  # e.g. "owner/repo"
    github_scopes = models.JSONField(default=list, blank=True)
    github_installation_id = models.CharField(max_length=50, null=True, blank=True)
    project_scanned = models.BooleanField(default=False)
    last_scanned_sha = models.CharField(max_length=40, null=True, blank=True)
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    pending_intent = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_integrations'

    def __str__(self):
        return f"UserIntegration ({self.slack_user_id})"


class StartupProfile(models.Model):
    organization = models.OneToOneField(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="startup_profile",
    )
    company_aliases = models.JSONField(default=list, blank=True)
    domain_aliases = models.JSONField(default=list, blank=True)
    product_names = models.JSONField(default=list, blank=True)
    founder_names = models.JSONField(default=list, blank=True)
    team_names = models.JSONField(default=list, blank=True)
    investor_names = models.JSONField(default=list, blank=True)
    investor_domains = models.JSONField(default=list, blank=True)
    competitor_names = models.JSONField(default=list, blank=True)
    competitor_domains = models.JSONField(default=list, blank=True)
    customer_names = models.JSONField(default=list, blank=True)
    customer_domains = models.JSONField(default=list, blank=True)
    prospect_names = models.JSONField(default=list, blank=True)
    prospect_domains = models.JSONField(default=list, blank=True)
    positive_keywords = models.JSONField(default=list, blank=True)
    negative_keywords = models.JSONField(default=list, blank=True)
    kpi_definitions = models.JSONField(default=list, blank=True)
    default_currency = models.CharField(max_length=12, default="USD")
    stage = models.CharField(max_length=64, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__domain"]

    def __str__(self):
        return f"Startup Profile ({self.organization.domain})"


class UserStartupBinding(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="startup_bindings",
    )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="user_startup_bindings",
    )
    google_connection = models.ForeignKey(
        GoogleConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="startup_bindings",
    )
    role = models.CharField(max_length=64, blank=True, default="")
    is_default_for_gmail = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "organization")]
        indexes = [
            models.Index(fields=["user", "is_default_for_gmail"], name="startup_bind_user_default_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} -> {self.organization.domain}"


class GmailSyncCursor(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_sync_cursors",
    )
    google_connection = models.ForeignKey(
        GoogleConnection,
        on_delete=models.CASCADE,
        related_name="sync_cursors",
    )
    last_history_id = models.CharField(max_length=255, blank=True, default="")
    backfill_window_start = models.DateTimeField(null=True, blank=True)
    backfill_window_end = models.DateTimeField(null=True, blank=True)
    last_synced_internal_date = models.DateTimeField(null=True, blank=True)
    last_message_internal_date = models.DateTimeField(null=True, blank=True)
    backfill_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "google_connection")]
        indexes = [
            models.Index(fields=["organization", "updated_at"], name="gmail_cursor_org_updated_idx"),
        ]

    def __str__(self):
        return f"Gmail Cursor ({self.organization.domain}/{self.google_connection_id})"


class GmailMessageArtifact(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_message_artifacts",
    )
    google_connection = models.ForeignKey(
        GoogleConnection,
        on_delete=models.CASCADE,
        related_name="message_artifacts",
    )
    gmail_message_id = models.CharField(max_length=255)
    gmail_thread_id = models.CharField(max_length=255, db_index=True)
    history_id = models.CharField(max_length=255, blank=True, default="")
    internal_date = models.DateTimeField(db_index=True)
    subject = models.TextField(blank=True, default="")
    from_address = models.CharField(max_length=500, blank=True, default="")
    to_addresses = models.JSONField(default=list, blank=True)
    cc_addresses = models.JSONField(default=list, blank=True)
    bcc_addresses = models.JSONField(default=list, blank=True)
    reply_to_addresses = models.JSONField(default=list, blank=True)
    label_ids = models.JSONField(default=list, blank=True)
    header_values = models.JSONField(default=dict, blank=True)
    snippet = models.TextField(blank=True, default="")
    cleaned_text = models.TextField(blank=True, default="")
    body_preview = models.TextField(blank=True, default="")
    attachment_manifest = models.JSONField(default=list, blank=True)
    has_attachments = models.BooleanField(default=False)
    heuristic_score = models.IntegerField(default=0)
    heuristic_reasons = models.JSONField(default=list, blank=True)
    relevance_label = models.CharField(
        max_length=20,
        choices=GmailRelevanceLabel.choices,
        default=GmailRelevanceLabel.PENDING,
        db_index=True,
    )
    relevance_score = models.FloatField(default=0.0)
    relevance_reason = models.TextField(blank=True, default="")
    needs_thread_context = models.BooleanField(default=False)
    metadata_hydrated_at = models.DateTimeField(null=True, blank=True)
    classified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "google_connection", "gmail_message_id")]
        indexes = [
            models.Index(fields=["organization", "relevance_label", "internal_date"], name="gmail_msg_org_rel_date_idx"),
            models.Index(fields=["organization", "gmail_thread_id"], name="gmail_msg_org_thread_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.gmail_message_id}"


class GmailThreadArtifact(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_thread_artifacts",
    )
    google_connection = models.ForeignKey(
        GoogleConnection,
        on_delete=models.CASCADE,
        related_name="thread_artifacts",
    )
    gmail_thread_id = models.CharField(max_length=255)
    source_message_ids = models.JSONField(default=list, blank=True)
    message_payloads = models.JSONField(default=list, blank=True)
    participant_summary = models.JSONField(default=dict, blank=True)
    cleaned_text = models.TextField(blank=True, default="")
    attachment_ids = models.JSONField(default=list, blank=True)
    hydration_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    extraction_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    source_message_count = models.IntegerField(default=0)
    latest_message_internal_date = models.DateTimeField(null=True, blank=True)
    hydrated_at = models.DateTimeField(null=True, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "google_connection", "gmail_thread_id")]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="gmail_thread_org_extract_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.gmail_thread_id}"


class GmailAttachmentArtifact(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_attachment_artifacts",
    )
    thread_artifact = models.ForeignKey(
        GmailThreadArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attachments",
    )
    message_artifact = models.ForeignKey(
        GmailMessageArtifact,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    gmail_attachment_id = models.CharField(max_length=1024, blank=True, default="")
    mime_type = models.CharField(max_length=255, blank=True, default="")
    filename = models.CharField(max_length=500, blank=True, default="")
    part_id = models.CharField(max_length=1024, blank=True, default="")
    content_disposition = models.CharField(max_length=500, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    is_inline = models.BooleanField(default=False)
    raw_content_base64 = models.TextField(blank=True, default="")
    extracted_text = models.TextField(blank=True, default="")
    extraction_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    parse_notes = models.TextField(blank=True, default="")
    hydrated_at = models.DateTimeField(null=True, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("message_artifact", "part_id", "gmail_attachment_id")]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="gmail_attach_org_extract_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.filename or self.part_id}"


class StartupMetricObservation(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="startup_metric_observations",
    )
    run = models.ForeignKey(
        "core.ContentFactoryRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="startup_metric_observations",
    )
    source_thread = models.ForeignKey(
        GmailThreadArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="metric_observations",
    )
    metric_key = models.CharField(max_length=100, db_index=True)
    metric_name = models.CharField(max_length=255)
    value_text = models.CharField(max_length=255)
    value_number = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    unit = models.CharField(max_length=50, blank=True, default="")
    observed_at = models.DateTimeField(null=True, blank=True)
    period_month = models.DateField(db_index=True)
    confidence = models.FloatField(default=0.0)
    evidence_message_ids = models.JSONField(default=list, blank=True)
    evidence_attachment_ids = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "period_month", "metric_key"], name="startup_metric_org_month_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.metric_key}:{self.period_month}"


class StartupEvent(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="startup_events",
    )
    run = models.ForeignKey(
        "core.ContentFactoryRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="startup_events",
    )
    canonical_key = models.CharField(max_length=255, db_index=True)
    event_type = models.CharField(max_length=50, choices=StartupEventType.choices)
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True, default="")
    event_date = models.DateField(null=True, blank=True)
    month_bucket = models.DateField(db_index=True)
    date_precision = models.CharField(
        max_length=20,
        choices=StartupEventDatePrecision.choices,
        default=StartupEventDatePrecision.DAY,
    )
    sentiment = models.CharField(max_length=20, blank=True, default="")
    investor_importance = models.PositiveSmallIntegerField(default=3)
    quantitative_facts = models.JSONField(default=list, blank=True)
    evidence_message_ids = models.JSONField(default=list, blank=True)
    evidence_attachment_ids = models.JSONField(default=list, blank=True)
    source_thread_ids = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(default=0.0)
    status = models.CharField(max_length=20, blank=True, default="open")
    needs_review = models.BooleanField(default=False)
    merge_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "canonical_key")]
        indexes = [
            models.Index(fields=["organization", "month_bucket", "event_type"], name="startup_event_org_month_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.canonical_key}"


class MonthlyUpdateDraft(models.Model):
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="monthly_update_drafts",
    )
    run = models.ForeignKey(
        "core.ContentFactoryRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="monthly_update_drafts",
    )
    month = models.DateField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=MonthlyUpdateDraftStatus.choices,
        default=MonthlyUpdateDraftStatus.DRAFT,
        db_index=True,
    )
    title = models.CharField(max_length=255, blank=True, default="")
    model_name = models.CharField(max_length=100, blank=True, default="")
    groundedness_status = models.CharField(
        max_length=20,
        choices=GroundednessStatus.choices,
        default=GroundednessStatus.PENDING,
    )
    structured_memo = models.JSONField(default=dict, blank=True)
    rendered_markdown = models.TextField(blank=True, default="")
    evidence_event_ids = models.JSONField(default=list, blank=True)
    evidence_metric_ids = models.JSONField(default=list, blank=True)
    carry_forward_event_ids = models.JSONField(default=list, blank=True)
    groundedness_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "month")]
        indexes = [
            models.Index(fields=["organization", "status", "month"], name="monthly_draft_org_status_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.month}"
