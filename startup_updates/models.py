import uuid

from django.conf import settings
from django.db import models


class GmailRelevanceLabel(models.TextChoices):
    PENDING = "pending", "Pending"
    UPDATE_WORTHY = "update_worthy", "Update Worthy"
    RELEVANT = "relevant", "Relevant"
    BACKGROUND = "background", "Background"
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


class StartupDataDeletionStatus(models.TextChoices):
    DELETING = "deleting", "Deleting"
    DELETED = "deleted", "Deleted"
    FAILED = "failed", "Failed"


class StartupProfile(models.Model):
    organization = models.OneToOneField(
        "organizations.Organization",
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
    organization_kind = models.CharField(max_length=32, blank=True, default="")
    short_description = models.TextField(blank=True, default="")
    problem_solved = models.TextField(blank=True, default="")
    target_audience = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_startupprofile"
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
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="user_startup_bindings",
    )
    google_connection = models.ForeignKey(
        "integrations.GoogleConnection",
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
        db_table = "integrations_userstartupbinding"
        unique_together = [("user", "organization")]
        indexes = [
            models.Index(fields=["user", "is_default_for_gmail"], name="startup_bind_user_default_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} -> {self.organization.domain}"


class StartupManualDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="manual_documents",
    )
    company = models.ForeignKey(
        "founder_tools.VibeRaisingCompany",
        on_delete=models.CASCADE,
        related_name="manual_documents",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="startup_manual_documents",
    )
    original_filename = models.CharField(max_length=512)
    content_type = models.CharField(max_length=255, blank=True, default="")
    file_size_bytes = models.PositiveIntegerField(default=0)
    storage_path = models.CharField(max_length=1024, unique=True)
    extraction_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    extracted_text = models.TextField(blank=True, default="")
    text_size_chars = models.PositiveIntegerField(default=0)
    parse_notes = models.TextField(blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_startupmanualdocument"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="manual_doc_org_created_idx"),
            models.Index(fields=["company", "created_at"], name="manual_doc_company_created_idx"),
            models.Index(fields=["created_by", "created_at"], name="manual_doc_user_created_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.original_filename}"


class GmailSyncCursor(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_sync_cursors",
    )
    google_connection = models.ForeignKey(
        "integrations.GoogleConnection",
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
        db_table = "integrations_gmailsynccursor"
        unique_together = [("organization", "google_connection")]
        indexes = [
            models.Index(fields=["organization", "updated_at"], name="gmail_cursor_org_updated_idx"),
        ]

    def __str__(self):
        return f"Gmail Cursor ({self.organization.domain}/{self.google_connection_id})"


class GmailMessageArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_message_artifacts",
    )
    google_connection = models.ForeignKey(
        "integrations.GoogleConnection",
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
        db_table = "integrations_gmailmessageartifact"
        unique_together = [("organization", "google_connection", "gmail_message_id")]
        indexes = [
            models.Index(fields=["organization", "relevance_label", "internal_date"], name="gmail_msg_org_rel_date_idx"),
            models.Index(fields=["organization", "gmail_thread_id"], name="gmail_msg_org_thread_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.gmail_message_id}"


class GmailThreadArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="gmail_thread_artifacts",
    )
    google_connection = models.ForeignKey(
        "integrations.GoogleConnection",
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
        db_table = "integrations_gmailthreadartifact"
        unique_together = [("organization", "google_connection", "gmail_thread_id")]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="gmail_thread_org_extract_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.gmail_thread_id}"


class SlackChannelSelection(models.Model):
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="slack_channel_selections",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="slack_channel_selections",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="slack_channel_selections",
        null=True,
        blank=True,
    )
    channel_id = models.CharField(max_length=100)
    channel_name = models.CharField(max_length=255, blank=True, default="")
    is_private = models.BooleanField(default=False)
    selected = models.BooleanField(default=False, db_index=True)
    sync_cursor = models.JSONField(default=dict, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_slackchannelselection"
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "channel_id"],
                name="slack_channel_selection_conn_channel_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "selected"], name="slack_chan_user_selected_idx"),
            models.Index(fields=["organization", "selected"], name="slack_chan_org_selected_idx"),
        ]
        ordering = ["channel_name", "channel_id"]

    def __str__(self):
        return f"{self.connection_id}:{self.channel_name or self.channel_id}"


class SlackMessageArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="slack_message_artifacts",
    )
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="slack_message_artifacts",
    )
    channel_id = models.CharField(max_length=100)
    channel_name = models.CharField(max_length=255, blank=True, default="")
    slack_message_ts = models.CharField(max_length=64)
    thread_ts = models.CharField(max_length=64, blank=True, default="")
    parent_ts = models.CharField(max_length=64, blank=True, default="")
    author_id = models.CharField(max_length=100, blank=True, default="")
    author_name = models.CharField(max_length=255, blank=True, default="")
    posted_at = models.DateTimeField(db_index=True)
    text = models.TextField(blank=True, default="")
    cleaned_text = models.TextField(blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_slackmessageartifact"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "connection", "channel_id", "slack_message_ts"],
                name="slack_message_org_conn_channel_ts_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "channel_id", "posted_at"], name="slack_msg_org_channel_post_idx"),
            models.Index(fields=["organization", "thread_ts"], name="slack_msg_org_thread_idx"),
        ]
        ordering = ["-posted_at", "-id"]

    def __str__(self):
        return f"{self.organization.domain}:{self.channel_id}:{self.slack_message_ts}"


class SlackThreadArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="slack_thread_artifacts",
    )
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="slack_thread_artifacts",
    )
    channel_id = models.CharField(max_length=100)
    channel_name = models.CharField(max_length=255, blank=True, default="")
    thread_ts = models.CharField(max_length=64)
    source_message_ids = models.JSONField(default=list, blank=True)
    source_message_count = models.IntegerField(default=0)
    cleaned_text = models.TextField(blank=True, default="")
    participant_summary = models.JSONField(default=dict, blank=True)
    message_payloads = models.JSONField(default=list, blank=True)
    latest_message_at = models.DateTimeField(null=True, blank=True)
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
    needs_extraction = models.BooleanField(default=False)
    extraction_hints = models.JSONField(default=dict, blank=True)
    classified_at = models.DateTimeField(null=True, blank=True)
    extraction_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    extracted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_slackthreadartifact"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "connection", "channel_id", "thread_ts"],
                name="slack_thread_org_conn_channel_ts_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="slack_thread_org_extract_idx"),
            models.Index(fields=["organization", "latest_message_at"], name="slack_thread_org_latest_idx"),
            models.Index(fields=["organization", "relevance_label", "latest_message_at"], name="slack_thr_org_rel_latest_idx"),
        ]
        ordering = ["-latest_message_at", "-id"]

    def __str__(self):
        return f"{self.organization.domain}:{self.channel_id}:{self.thread_ts}"


class LinearProjectSelection(models.Model):
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="linear_project_selections",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="linear_project_selections",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="linear_project_selections",
        null=True,
        blank=True,
    )
    linear_project_id = models.CharField(max_length=100)
    project_name = models.CharField(max_length=255, blank=True, default="")
    project_status = models.CharField(max_length=100, blank=True, default="")
    project_health = models.CharField(max_length=100, blank=True, default="")
    selected = models.BooleanField(default=False, db_index=True)
    sync_cursor = models.JSONField(default=dict, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_linearprojectselection"
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "linear_project_id"],
                name="linear_project_sel_conn_project_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "selected"], name="linear_sel_user_selected_idx"),
            models.Index(fields=["organization", "selected"], name="linear_sel_org_selected_idx"),
        ]
        ordering = ["project_name", "linear_project_id"]

    def __str__(self):
        return f"{self.connection_id}:{self.project_name or self.linear_project_id}"


class LinearProjectArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="linear_project_artifacts",
    )
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="linear_project_artifacts",
    )
    linear_project_id = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    status_name = models.CharField(max_length=100, blank=True, default="")
    status_type = models.CharField(max_length=100, blank=True, default="")
    health = models.CharField(max_length=100, blank=True, default="")
    progress = models.FloatField(null=True, blank=True)
    scope = models.FloatField(null=True, blank=True)
    priority = models.IntegerField(default=0)
    lead_name = models.CharField(max_length=255, blank=True, default="")
    lead_email = models.CharField(max_length=255, blank=True, default="")
    team_names = models.JSONField(default=list, blank=True)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    url = models.URLField(max_length=1024, blank=True, default="")
    source_record_ids = models.JSONField(default=list, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
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
    needs_extraction = models.BooleanField(default=False)
    extraction_hints = models.JSONField(default=dict, blank=True)
    classified_at = models.DateTimeField(null=True, blank=True)
    extraction_status = models.CharField(
        max_length=20,
        choices=ArtifactProcessingStatus.choices,
        default=ArtifactProcessingStatus.PENDING,
        db_index=True,
    )
    extracted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_linearprojectartifact"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "connection", "linear_project_id"],
                name="linear_project_org_conn_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="linear_proj_org_extract_idx"),
            models.Index(fields=["organization", "status_type", "health"], name="linear_proj_org_state_idx"),
            models.Index(fields=["organization", "relevance_label", "updated_at"], name="linear_proj_org_rel_upd_idx"),
        ]
        ordering = ["name", "linear_project_id"]

    def __str__(self):
        return f"{self.organization.domain}:{self.name or self.linear_project_id}"


class LinearIssueArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="linear_issue_artifacts",
    )
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="linear_issue_artifacts",
    )
    project = models.ForeignKey(
        LinearProjectArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues",
    )
    linear_issue_id = models.CharField(max_length=100)
    identifier = models.CharField(max_length=100, blank=True, default="")
    title = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")
    state_name = models.CharField(max_length=100, blank=True, default="")
    state_type = models.CharField(max_length=100, blank=True, default="")
    priority = models.FloatField(null=True, blank=True)
    priority_label = models.CharField(max_length=100, blank=True, default="")
    assignee_name = models.CharField(max_length=255, blank=True, default="")
    assignee_email = models.CharField(max_length=255, blank=True, default="")
    team_key = models.CharField(max_length=50, blank=True, default="")
    team_name = models.CharField(max_length=255, blank=True, default="")
    label_names = models.JSONField(default=list, blank=True)
    estimate = models.FloatField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    created_at_linear = models.DateTimeField(null=True, blank=True)
    updated_at_linear = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    url = models.URLField(max_length=1024, blank=True, default="")
    source_record_id = models.CharField(max_length=255, blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_linearissueartifact"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "connection", "linear_issue_id"],
                name="linear_issue_org_conn_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "updated_at_linear"], name="linear_issue_org_updated_idx"),
            models.Index(fields=["project", "updated_at_linear"], name="linear_issue_proj_updated_idx"),
            models.Index(fields=["organization", "state_type"], name="linear_issue_org_state_idx"),
        ]
        ordering = ["-updated_at_linear", "-id"]

    def __str__(self):
        return f"{self.organization.domain}:{self.identifier or self.linear_issue_id}"


class LinearProjectUpdateArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="linear_project_update_artifacts",
    )
    connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.CASCADE,
        related_name="linear_project_update_artifacts",
    )
    project = models.ForeignKey(
        LinearProjectArtifact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_updates",
    )
    linear_project_update_id = models.CharField(max_length=100)
    body = models.TextField(blank=True, default="")
    health = models.CharField(max_length=100, blank=True, default="")
    author_name = models.CharField(max_length=255, blank=True, default="")
    author_email = models.CharField(max_length=255, blank=True, default="")
    url = models.URLField(max_length=1024, blank=True, default="")
    created_at_linear = models.DateTimeField(null=True, blank=True)
    updated_at_linear = models.DateTimeField(null=True, blank=True)
    source_record_id = models.CharField(max_length=255, blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_linearprojectupdateartifact"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "connection", "linear_project_update_id"],
                name="linear_update_org_conn_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "updated_at_linear"], name="linear_update_org_updated_idx"),
            models.Index(fields=["project", "updated_at_linear"], name="linear_update_proj_updated_idx"),
        ]
        ordering = ["-updated_at_linear", "-id"]

    def __str__(self):
        return f"{self.organization.domain}:{self.linear_project_update_id}"


class GmailAttachmentArtifact(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
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
    last_error = models.TextField(blank=True, default="")
    hydrated_at = models.DateTimeField(null=True, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_gmailattachmentartifact"
        unique_together = [("message_artifact", "part_id", "gmail_attachment_id")]
        indexes = [
            models.Index(fields=["organization", "extraction_status"], name="gmail_attach_org_extract_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.filename or self.part_id}"


class StartupMetricObservation(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="startup_metric_observations",
    )
    run = models.ForeignKey(
        "workflow_runs.ContentFactoryRun",
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
    source_provider = models.CharField(max_length=32, blank=True, default="gmail", db_index=True)
    source_record_ids = models.JSONField(default=list, blank=True)
    source_metadata = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_startupmetricobservation"
        indexes = [
            models.Index(fields=["organization", "period_month", "metric_key"], name="startup_metric_org_month_idx"),
            models.Index(fields=["organization", "source_provider", "period_month"], name="startup_metric_org_source_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.metric_key}:{self.period_month}"


class StartupEvent(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="startup_events",
    )
    run = models.ForeignKey(
        "workflow_runs.ContentFactoryRun",
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
        db_table = "integrations_startupevent"
        unique_together = [("organization", "canonical_key")]
        indexes = [
            models.Index(fields=["organization", "month_bucket", "event_type"], name="startup_event_org_month_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.canonical_key}"


class MonthlyUpdateDraft(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="monthly_update_drafts",
    )
    run = models.ForeignKey(
        "workflow_runs.ContentFactoryRun",
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
        db_table = "integrations_monthlyupdatedraft"
        unique_together = [("organization", "month")]
        indexes = [
            models.Index(fields=["organization", "status", "month"], name="monthly_draft_org_status_idx"),
        ]

    def __str__(self):
        return f"{self.organization.domain}:{self.month}"


class StartupDataDeletionRequest(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="startup_data_deletion_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="startup_data_deletion_requests",
    )
    request_id = models.CharField(max_length=120, unique=True, db_index=True)
    provider = models.CharField(max_length=32, default="gmail", db_index=True)
    status = models.CharField(
        max_length=20,
        choices=StartupDataDeletionStatus.choices,
        default=StartupDataDeletionStatus.DELETING,
        db_index=True,
    )
    delete_derived_data = models.BooleanField(default=False)
    google_account = models.EmailField(blank=True, default="")
    reason = models.TextField(blank=True, default="")
    deleted_counts = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_startupdatadeletionrequest"
        indexes = [
            models.Index(fields=["organization", "status", "-updated_at"], name="startup_delete_org_status_idx"),
            models.Index(fields=["provider", "status"], name="startup_delete_provider_idx"),
        ]
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.organization_id}:{self.provider}:{self.status}"
