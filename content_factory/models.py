import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from organizations.models import Organization

from .auth import content_factory_github_connection_state


class OrganizationContentConfig(models.Model):
    """Content factory configuration per organization."""
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='content_config'
    )
    connected_slack_user_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        db_index=True,
        help_text="Slack user ID that owns this domain-to-GitHub connection",
    )
    default_timezone = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Default IANA timezone for scheduled content suggestions when a user timezone is unavailable",
    )
    daily_discovery_enabled = models.BooleanField(
        default=False,
        help_text="Whether this organization participates in the shared daily discovery queue.",
    )
    daily_discovery_priority = models.PositiveSmallIntegerField(
        default=0,
        help_text="Lower numbers run earlier in the shared daily discovery queue.",
    )
    baseline_skipped_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the founder explicitly skipped the website baseline prerequisite.",
    )
    baseline_skip_reason = models.TextField(blank=True, default="")
    article_template = models.TextField(blank=True, null=True)
    design_guide = models.TextField(blank=True, null=True)
    resource_prompt = models.TextField(blank=True, null=True)
    company_context = models.TextField(blank=True, null=True, help_text="Auto-generated company overview for article generation context")
    github_repo = models.CharField(max_length=255, blank=True, null=True)
    github_token_encrypted = models.TextField(blank=True, null=True)

    # Domain-level GitHub credentials (supports multiple domains with different GitHub accounts)
    github_refresh_token_encrypted = models.TextField(blank=True, null=True)
    github_token_expires_at = models.DateTimeField(blank=True, null=True)
    github_user_name = models.CharField(max_length=255, blank=True, null=True)
    github_installation_id = models.CharField(max_length=50, blank=True, null=True)
    github_scopes = models.JSONField(default=list, blank=True)
    article_delivery_mode = models.CharField(max_length=32, blank=True, null=True)
    article_path_pattern = models.CharField(
        max_length=255, default="app/articles/content/{category}/{slug}.tsx"
    )
    registry_path = models.CharField(max_length=255, default="app/articles/registry.ts")
    publish_targets = models.JSONField(
        default=list,
        blank=True,
        help_text="Cached publish target metadata derived from repository scans or live repo hints",
    )
    default_publish_target_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Preferred publish target identifier for direct preview runs",
    )
    auto_publish = models.BooleanField(
        default=False,
        help_text="When true, generated article PRs auto-merge once automated build/preview verification passes (no human review).",
    )
    requires_review = models.BooleanField(
        default=False,
        help_text="Force human review: open the publish PR but never auto-merge, even when auto_publish is true. Overrides auto_publish.",
    )
    scan_summary = models.TextField(blank=True, null=True)
    tech_stack = models.JSONField(default=dict, blank=True)
    installed_packages = models.JSONField(
        default=dict, blank=True,
        help_text="Full list of installed packages from package.json {name: version}"
    )
    pillar_strategy = models.JSONField(
        default=dict, blank=True,
        help_text="SEO content pillars with slugs and topics derived from company context"
    )
    build_healing_hints = models.JSONField(
        default=list,
        blank=True,
        help_text="Reusable build/browser healing rules promoted from publish-time verification.",
    )
    repo_execution_contract = models.JSONField(
        default=dict,
        blank=True,
        help_text="Runtime family and command contract used to verify and publish this repository.",
    )
    brand_name = models.CharField(max_length=100, blank=True, null=True)
    articles_scaffolded = models.BooleanField(
        default=False,
        help_text="Whether the articles directory has been scaffolded in the GitHub repo"
    )
    articles_scaffold_pr_url = models.URLField(
        blank=True, null=True,
        help_text="PR URL from the articles scaffolding operation"
    )
    articles_scaffold_preview_url = models.URLField(
        blank=True, null=True,
        help_text="Cloudflare Pages preview URL from the articles scaffolding operation"
    )
    article_system = models.JSONField(
        default=dict,
        blank=True,
        help_text="Canonical article/blog system readiness state for this organization"
    )
    last_scanned_sha = models.CharField(max_length=40, blank=True, null=True)
    last_scanned_at = models.DateTimeField(blank=True, null=True)
    scan_request_fingerprint = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Fingerprint of the scan request shape; with last_scanned_sha lets an unchanged re-scan short-circuit instead of re-running.",
    )
    article_system_setup_cache = models.JSONField(
        default=dict, blank=True,
        help_text="Self-contained component-reuse cache (inventory, managed files, context fingerprint) from the last scan.",
    )
    framework_component_specs = models.JSONField(
        default=dict, blank=True,
        help_text="Framework-adapted component spec docs from the last scan, reused to skip regeneration.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'content_factory_org_config'

    @property
    def has_github_token(self) -> bool:
        return bool(str(self.github_token_encrypted or "").strip())

    @property
    def has_github_repo(self) -> bool:
        return bool(str(self.github_repo or "").strip())

    @property
    def github_connection_state(self) -> str:
        return content_factory_github_connection_state(self)


class WebsiteBaselineSnapshot(models.Model):
    """Historical website performance baseline for a Vibe Marketing organization."""

    STATUS_CHOICES = [
        ("completed", "Completed"),
        ("partial", "Partial"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="website_baseline_snapshots",
    )
    domain = models.CharField(max_length=255, db_index=True)
    run_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="completed", db_index=True)
    collected_at = models.DateTimeField(default=timezone.now, db_index=True)
    overall_score = models.PositiveSmallIntegerField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    source_status = models.JSONField(default=dict, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_website_baseline_snapshot"
        ordering = ["-collected_at", "-created_at"]
        indexes = [
            models.Index(fields=["organization", "-collected_at"], name="website_base_org_collected_idx"),
            models.Index(fields=["domain", "-collected_at"], name="website_base_domain_idx"),
        ]

    def __str__(self):
        score = self.overall_score if self.overall_score is not None else "n/a"
        return f"{self.domain} baseline {score} ({self.status})"


class GeneratedComponent(models.Model):
    """
    Stores a generated/adapted React component for an organization.
    
    Components are created by content-factory's component generation pipeline
    during codebase scanning.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='generated_components'
    )
    
    # Component identity
    name = models.CharField(max_length=100)  # e.g., "ArticleHeroHeader"
    
    # Component content
    content = models.TextField()  # Full TSX code
    
    # Source tracking
    SOURCE_CHOICES = [
        ('generated', 'Generated'),
        ('adapted', 'Adapted'),
    ]
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    original_path = models.CharField(max_length=500, blank=True, null=True)  # If adapted
    
    # Matching metadata
    similarity_score = models.FloatField(default=0.0)  # 0.0 - 1.0
    matched_component = models.CharField(max_length=100, blank=True, null=True)  # Their component name
    adaptation_notes = models.TextField(blank=True, default='')
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'content_factory_generated_component'
        unique_together = ['organization', 'name']  # One component per name per org
        ordering = ['name']
    
    def __str__(self):
        return f"{self.organization.domain} / {self.name} ({self.source})"


class ComponentMapping(models.Model):
    """
    Stores the component mapping results from a scan.
    
    This is a JSON field containing the full mapping of:
    - our_component -> matched/unmatched status
    - similarity scores
    - adaptation notes
    """
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name='component_mapping'
    )
    
    # Full mapping as JSON (Dict[str, ComponentMatch])
    mapping_data = models.JSONField(default=dict)
    
    # Summary stats
    total_components = models.IntegerField(default=0)
    matched_count = models.IntegerField(default=0)
    generated_count = models.IntegerField(default=0)
    
    # Generation pipeline result summary
    generation_status = models.CharField(max_length=20, blank=True, null=True)  # success/partial/failed
    design_guide_path = models.CharField(max_length=500, blank=True, null=True)
    storage_local_path = models.CharField(max_length=500, blank=True, null=True)
    storage_pr_url = models.URLField(blank=True, null=True)
    storage_branch_url = models.URLField(blank=True, null=True)
    failed_components = models.JSONField(default=list)  # List of failed component names
    
    # Last scan info
    last_scan_commit = models.CharField(max_length=40, blank=True, null=True)
    last_scan_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'content_factory_component_mapping'
    
    def __str__(self):
        return f"{self.organization.domain} mapping ({self.matched_count}/{self.total_components} matched)"


class ContentFactoryJob(models.Model):
    """
    Tracks content-factory pipeline jobs for callback routing.

    When content-factory sends callbacks (topic_selection, article_complete, error),
    this model maintains the mapping between job IDs and Slack users to enable
    proper notification routing.
    """
    job_id = models.CharField(max_length=100, unique=True, db_index=True)
    slack_user_id = models.CharField(max_length=50, db_index=True)
    domain = models.CharField(max_length=255)

    # Job state
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('researching', 'Researching'),
        ('awaiting_confirmation', 'Awaiting Confirmation'),
        ('awaiting_delivery_mode', 'Awaiting Delivery Mode'),
        ('awaiting_approval', 'Awaiting Approval'),
        ('generating', 'Generating'),
        ('blocked', 'Blocked'),
        ('pr_opened', 'PR Opened'),
        ('needs_review', 'Needs Review'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
        ('error', 'Error'),
        ('auth_required', 'Auth Required'),
    ]
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='queued')

    # Request metadata for retry (populated on creation)
    request_meta = models.JSONField(default=dict, blank=True, help_text="Original request parameters for retry")
    client_request_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    billing_source_job_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    billing_amount = models.IntegerField(default=0)
    billing_status = models.CharField(max_length=20, blank=True, default="")
    billing_ledger = models.ForeignKey(
        'roo.Ledger',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='content_factory_jobs',
    )

    # Topic selection data (populated on topic_selection callback)
    selected_keyword = models.CharField(max_length=255, blank=True, null=True)
    selection_reason = models.TextField(blank=True, null=True)
    selection_data = models.JSONField(default=dict, blank=True)  # Full selection payload

    # Slack thread context for in-thread replies
    slack_channel_id = models.CharField(max_length=100, blank=True, default="")
    slack_root_message_ts = models.CharField(max_length=50, blank=True, default="")
    slack_thread_ts = models.CharField(max_length=50, blank=True, default="")
    progress_message_ts = models.CharField(max_length=50, blank=True, default="")
    posted_progress_ids = models.JSONField(default=list, blank=True)
    last_progress_milestone_index = models.IntegerField(default=0)
    last_progress_milestone_key = models.CharField(max_length=100, blank=True, default="")
    last_progress_updated_at = models.DateTimeField(blank=True, null=True)
    still_working_pinged_at = models.DateTimeField(blank=True, null=True)

    # Result data (populated on article_complete callback)
    article_url = models.URLField(blank=True, null=True)
    pr_url = models.URLField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'content_factory_job'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.job_id} ({self.status}) - {self.domain}"


class ContentFactoryCallbackEvent(models.Model):
    """
    Durable idempotency ledger for content-factory callback deliveries.

    content-factory stamps each callback with a unique event_id and retries
    failed deliveries from a durable outbox, so the same event can arrive more
    than once. A row here means the event was already acknowledged with a 2xx
    response; replays return 200 without reprocessing. This must be a DB table
    rather than Django cache: production uses per-process LocMemCache, which
    is invisible to other workers and lost on restart.
    """

    event_id = models.CharField(max_length=100, unique=True, db_index=True)
    job_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    event_type = models.CharField(max_length=100, blank=True, default="")
    emitted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'content_factory_callback_event'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_id} ({self.event_type}) - {self.job_id}"


class ScheduledDiscoveryDispatchState(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    QUEUED = "queued", "Queued"
    TOPIC_SELECTION_SENT = "topic_selection_sent", "Topic Selection Sent"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    FAILED_TIMEOUT = "failed_timeout", "Failed Timeout"
    EXPIRED = "expired", "Expired"


class ScheduledDiscoveryDispatch(models.Model):
    """
    Tracks one scheduled daily discovery attempt per user/domain/local date.
    """

    slack_user_id = models.CharField(max_length=50, db_index=True)
    domain = models.CharField(max_length=255, db_index=True)
    timezone = models.CharField(max_length=64, default="Australia/Melbourne")
    local_date = models.DateField(db_index=True)
    scheduled_for_at = models.DateTimeField(blank=True, null=True, db_index=True)
    slot_index = models.PositiveSmallIntegerField(default=0)
    trigger_source = models.CharField(max_length=50, default="daily_scheduler", db_index=True)
    state = models.CharField(
        max_length=30,
        choices=ScheduledDiscoveryDispatchState.choices,
        default=ScheduledDiscoveryDispatchState.SCHEDULED,
        db_index=True,
    )
    content_factory_job_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    last_error = models.TextField(blank=True, default="")
    slack_channel_id = models.CharField(max_length=100, blank=True, default="")
    slack_message_ts = models.CharField(max_length=50, blank=True, default="")
    slack_thread_ts = models.CharField(max_length=50, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scheduled_discovery_dispatch"
        ordering = ["-local_date", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["slack_user_id", "domain", "local_date", "trigger_source"],
                name="scheduled_discovery_dispatch_unique_target_day",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "updated_at"], name="sched_disc_state_updated_idx"),
            models.Index(fields=["domain", "slack_user_id", "state"], name="sched_disc_target_state_idx"),
        ]

    def __str__(self):
        return f"{self.domain}/{self.slack_user_id}/{self.local_date} ({self.state})"


class NotificationChannelType(models.TextChoices):
    SLACK = "slack", "Slack"
    WHATSAPP = "whatsapp", "WhatsApp"
    EMAIL = "email", "Email"


class NotificationConsentState(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    OPTED_OUT = "opted_out", "Opted Out"
    REVOKED = "revoked", "Revoked"


class ResearchAutomationStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    DISABLED = "disabled", "Disabled"


class AutomationRunStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    QUEUED = "queued", "Queued"
    TOPIC_SELECTION_SENT = "topic_selection_sent", "Topic Selection Sent"
    DELIVERY_MODE_REQUIRED = "delivery_mode_required", "Delivery Mode Required"
    GENERATING = "generating", "Generating"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


class NotificationDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    BOUNCED = "bounced", "Bounced"
    OPTED_OUT = "opted_out", "Opted Out"


class NotificationChannel(models.Model):
    """A consented route that can receive scheduled research notifications."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notification_channels",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="content_notification_channels",
        null=True,
        blank=True,
    )
    channel_type = models.CharField(max_length=20, choices=NotificationChannelType.choices, db_index=True)
    route_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Slack user ID, E.164 WhatsApp number, or email address.",
    )
    display_name = models.CharField(max_length=255, blank=True, default="")
    consent_state = models.CharField(
        max_length=20,
        choices=NotificationConsentState.choices,
        default=NotificationConsentState.PENDING,
        db_index=True,
    )
    provider_connection = models.ForeignKey(
        "integrations.ExternalServiceConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="content_notification_channels",
    )
    provider_metadata = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    opted_out_at = models.DateTimeField(blank=True, null=True)
    verification_code_hash = models.CharField(max_length=128, blank=True, default="")
    verification_expires_at = models.DateTimeField(blank=True, null=True)
    verification_attempts = models.PositiveSmallIntegerField(default=0)
    verification_last_sent_at = models.DateTimeField(blank=True, null=True)
    verification_send_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_notification_channel"
        ordering = ["organization_id", "channel_type", "route_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "channel_type", "route_id"],
                name="content_notify_channel_unique_route",
            ),
        ]
        indexes = [
            models.Index(fields=["channel_type", "consent_state"], name="cf_notify_channel_consent_idx"),
            models.Index(fields=["user", "channel_type"], name="cf_notify_channel_user_idx"),
        ]

    def __str__(self):
        return f"{self.channel_type}:{self.route_id} ({self.consent_state})"


class ResearchAutomation(models.Model):
    """User-configured scheduled research cadence for one organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="research_automations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="content_research_automations",
        null=True,
        blank=True,
    )
    notification_channel = models.ForeignKey(
        NotificationChannel,
        on_delete=models.PROTECT,
        related_name="research_automations",
    )
    name = models.CharField(max_length=255, blank=True, default="")
    timezone = models.CharField(max_length=64, default="Australia/Melbourne")
    frequency_per_day = models.PositiveSmallIntegerField(default=1)
    local_send_times = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        choices=ResearchAutomationStatus.choices,
        default=ResearchAutomationStatus.ACTIVE,
        db_index=True,
    )
    last_scheduled_for_at = models.DateTimeField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_research_automation"
        ordering = ["organization_id", "created_at"]
        indexes = [
            models.Index(fields=["status", "updated_at"], name="cf_research_auto_status_idx"),
            models.Index(fields=["organization", "status"], name="cf_research_auto_org_idx"),
        ]

    def __str__(self):
        return self.name or f"{self.organization_id} research automation"


class AutomationRun(models.Model):
    """One scheduled slot/run for a research automation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    automation = models.ForeignKey(
        ResearchAutomation,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    scheduled_for_at = models.DateTimeField(db_index=True)
    local_date = models.DateField(db_index=True)
    slot_index = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=32,
        choices=AutomationRunStatus.choices,
        default=AutomationRunStatus.SCHEDULED,
        db_index=True,
    )
    content_factory_run_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    article_content_factory_run_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True, db_index=True)
    selected_topic = models.JSONField(default=dict, blank=True)
    selected_delivery_mode = models.CharField(max_length=32, blank=True, default="")
    request_payload = models.JSONField(default=dict, blank=True)
    callback_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_automation_run"
        ordering = ["-scheduled_for_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["automation", "local_date", "slot_index"],
                name="content_auto_run_unique_slot",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "scheduled_for_at"], name="cf_auto_run_due_idx"),
            models.Index(fields=["content_factory_run_id"], name="cf_auto_run_cf_idx"),
        ]

    def __str__(self):
        return f"{self.automation_id}:{self.local_date}:{self.slot_index} ({self.status})"


class NotificationDelivery(models.Model):
    """Provider delivery attempt for an automation event."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    automation_run = models.ForeignKey(
        AutomationRun,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.ForeignKey(
        NotificationChannel,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=NotificationDeliveryStatus.choices,
        default=NotificationDeliveryStatus.PENDING,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=255, unique=True, db_index=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True, default="")
    delivered_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_notification_delivery"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_type", "status"], name="cf_notify_delivery_event_idx"),
            models.Index(fields=["channel", "status"], name="cf_notify_delivery_channel_idx"),
        ]

    def __str__(self):
        return f"{self.event_type}:{self.channel_id}:{self.status}"


class ContentFactoryHealingPromotionState(models.TextChoices):
    CANDIDATE = "candidate", "Candidate"
    PROMOTED = "promoted", "Promoted"


class ContentFactoryHealingRecord(models.Model):
    """Reusable publish-time healing memory for a specific repo/domain failure family."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="content_factory_healing_records",
    )
    domain = models.CharField(max_length=255, db_index=True)
    github_repo = models.CharField(max_length=255, blank=True, default="", db_index=True)
    failure_kind = models.CharField(max_length=100, db_index=True)
    failure_family_key = models.CharField(max_length=64, db_index=True)
    exact_signature = models.CharField(max_length=64, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    normalized_failure = models.JSONField(default=dict, blank=True)
    changed_files = models.JSONField(default=list, blank=True)
    patch_manifest = models.JSONField(default=dict, blank=True)
    validation_results = models.JSONField(default=dict, blank=True)
    evidence_artifacts = models.JSONField(default=dict, blank=True)
    snippet_or_rule = models.TextField(blank=True, default="")
    applies_to = models.JSONField(default=list, blank=True)
    promoted_payload = models.JSONField(default=dict, blank=True)
    promotion_state = models.CharField(
        max_length=32,
        choices=ContentFactoryHealingPromotionState.choices,
        default=ContentFactoryHealingPromotionState.CANDIDATE,
        db_index=True,
    )
    latest_run_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_healing_record"
        ordering = ["-updated_at"]
        unique_together = (
            "domain",
            "github_repo",
            "failure_kind",
            "failure_family_key",
        )

    def __str__(self):
        return f"{self.domain}:{self.github_repo}:{self.failure_kind}:{self.failure_family_key}"


class VibeMarketingComponentCommentStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    APPLIED = "applied", "Applied"
    SUPERSEDED = "superseded", "Superseded"


class VibeMarketingComponentComment(models.Model):
    """Founder review comment attached to one generated article component."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        "workflow_runs.ContentFactoryRun",
        on_delete=models.CASCADE,
        related_name="component_comments",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vibe_marketing_component_comments",
    )
    component_id = models.CharField(max_length=255, db_index=True)
    component_type = models.CharField(max_length=120, blank=True, default="")
    component_label = models.CharField(max_length=255, blank=True, default="")
    source_section_id = models.CharField(max_length=255, blank=True, default="")
    selector = models.CharField(max_length=500, blank=True, default="")
    anchor = models.JSONField(blank=True, default=dict)
    context = models.JSONField(blank=True, default=dict)
    body = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=VibeMarketingComponentCommentStatus.choices,
        default=VibeMarketingComponentCommentStatus.DRAFT,
        db_index=True,
    )
    batch_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_vibe_component_comment"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["run", "status"], name="vibe_comment_run_status_idx"),
            models.Index(fields=["run", "batch_id"], name="vibe_comment_run_batch_idx"),
        ]

    def __str__(self):
        return f"{self.run_id}:{self.component_id}:{self.status}"
# =============================================================================
# SEO Research Models
# =============================================================================


class KeywordTier(models.TextChoices):
    """GEO-based topic prioritization tiers."""
    TIER_1_BLUE_OCEAN = "tier_1_blue_ocean", "Blue Ocean"
    TIER_2_AUTHORITY = "tier_2_authority", "Authority Builder"
    TIER_3_LONG_TAIL = "tier_3_long_tail", "Long Tail Gem"
    TIER_4_DISCARD = "tier_4_discard", "Discard"


class KeywordSource(models.TextChoices):
    """How the keyword was discovered."""
    SEED = "seed", "Seed Keyword"
    COMPETITOR = "competitor", "Competitor Analysis"
    RELATED = "related", "Related/Semantic"
    PAA = "paa", "People Also Ask"


class KeywordStatus(models.TextChoices):
    """Keyword writing status."""
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    IN_PROGRESS = "in_progress", "In Progress"
    WRITTEN = "written", "Written"
    SKIPPED = "skipped", "Skipped"


class TrendStatus(models.TextChoices):
    """Trend velocity status."""
    BREAKOUT = "breakout", "Breakout"
    RISING = "rising", "Rising"
    STABLE = "stable", "Stable"
    DECLINING = "declining", "Declining"


class ArticlePublishStatus(models.TextChoices):
    """Real publish lifecycle of a written article.

    A completed writing run only means the content was packaged; the article
    is not on the customer's site until its PR merges and the site deploys.
    """
    WRITTEN = "written", "Written"
    PR_OPEN = "pr_open", "PR Open"
    PR_CLOSED = "pr_closed", "PR Closed"
    MERGED = "merged", "Merged"
    LIVE = "live", "Live"


class WrittenArticle(models.Model):
    """
    Links researched keywords to published articles.

    Tracks the output of the content-factory pipeline.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='written_articles'
    )

    # Article identity
    title = models.CharField(max_length=500)
    slug = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=100)

    # URLs
    article_url = models.URLField(blank=True, null=True)
    pr_url = models.URLField(blank=True, null=True)

    # Publish lifecycle (see ArticlePublishStatus): written -> pr_open -> merged -> live.
    publish_status = models.CharField(
        max_length=20,
        choices=ArticlePublishStatus.choices,
        default=ArticlePublishStatus.WRITTEN,
        db_index=True,
    )
    pr_number = models.IntegerField(null=True, blank=True)
    pr_merged_at = models.DateTimeField(null=True, blank=True)
    # The merge commit the PR landed on origin's default branch — the anchor for
    # proving "this content is really in the code on main".
    merge_commit_sha = models.CharField(max_length=64, blank=True, default="")

    # Source-of-truth facts. An article counts as "published" only once its
    # content is confirmed present on origin's default branch (main). This is set
    # by the reconciler when the PR is observed merged into main (see
    # article_publish_status.refresh_publish_statuses) and never downgrades; it is
    # independent of the weaker sitemap-based `live_*` signal below.
    on_main_verified_at = models.DateTimeField(null=True, blank=True)
    on_main_commit_sha = models.CharField(max_length=64, blank=True, default="")
    # Repo path of the article's content/registry file, captured from publish
    # evidence; lets the reconciler verify the file literally exists on main.
    content_path = models.CharField(max_length=500, blank=True, default="")

    live_url = models.URLField(
        blank=True, null=True,
        help_text="Production URL confirmed against the customer site's sitemap",
    )
    live_checked_at = models.DateTimeField(null=True, blank=True)
    live_verified_at = models.DateTimeField(null=True, blank=True)

    # Primary keyword (denormalized for quick access)
    primary_keyword = models.CharField(max_length=500)

    # Content factory job reference
    job = models.ForeignKey(
        ContentFactoryJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='articles'
    )

    # run_id of the most recent *writing* run (never a publish child); powers
    # the dashboard "Edit & republish" link back to the run review page.
    source_run_id = models.CharField(max_length=100, blank=True, default="")

    # Timestamps
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'seo_written_article'
        unique_together = ['organization', 'slug']
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'primary_keyword']),
            # Covers build_topic_coverage_memory's filter(organization).order_by('-created_at'),
            # run on every bootstrap/navigation.
            models.Index(fields=['organization', '-created_at'], name='wa_org_created_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.slug})"


class ResearchedKeyword(models.Model):
    """
    Core SEO keyword with metrics from content-factory research.

    This is the central table linking keywords to organizations
    with full GEO metrics and writing status tracking.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='researched_keywords',
        db_index=True
    )

    # Keyword identity
    keyword = models.CharField(max_length=500, db_index=True)
    keyword_normalized = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Lowercase, trimmed version for deduplication"
    )

    # Core metrics (refreshable)
    volume = models.IntegerField(default=0, help_text="Monthly search volume")
    difficulty = models.IntegerField(default=50, help_text="SEO difficulty 0-100")
    difficulty_source = models.CharField(
        max_length=30,
        default="legacy_default",
        help_text="Source for difficulty: dataforseo_labs, dataforseo_bulk, missing, or legacy_default",
    )
    intent = models.CharField(max_length=50, default="informational")

    # GEO metrics
    tier = models.CharField(
        max_length=30,
        choices=KeywordTier.choices,
        default=KeywordTier.TIER_4_DISCARD
    )
    opportunity_index = models.FloatField(default=0.0, db_index=True)

    # Provenance
    source = models.CharField(
        max_length=20,
        choices=KeywordSource.choices,
        default=KeywordSource.SEED
    )
    source_detail = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="E.g., competitor domain or seed keyword"
    )
    competitor_urls = models.JSONField(default=list, blank=True)
    related_keywords = models.JSONField(default=list, blank=True)
    monthly_searches = models.JSONField(default=list, blank=True)

    # Writing status
    status = models.CharField(
        max_length=20,
        choices=KeywordStatus.choices,
        default=KeywordStatus.PENDING,
        db_index=True
    )

    # Research memory
    times_shown = models.IntegerField(default=0)
    last_shown_at = models.DateTimeField(null=True, blank=True)
    times_rejected = models.IntegerField(default=0)
    last_rejected_at = models.DateTimeField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    times_selected = models.IntegerField(default=0)
    last_selected_at = models.DateTimeField(null=True, blank=True)
    cluster_fingerprint = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # Link to written article
    written_article = models.ForeignKey(
        WrittenArticle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_keywords'
    )

    # Timestamps
    discovered_at = models.DateTimeField(default=timezone.now)
    metrics_updated_at = models.DateTimeField(auto_now=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'seo_researched_keyword'
        unique_together = ['organization', 'keyword_normalized']
        ordering = ['-opportunity_index']
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'tier']),
            models.Index(fields=['organization', 'opportunity_index']),
            models.Index(fields=['organization', 'status', 'opportunity_index']),
            models.Index(fields=['organization', 'cooldown_until'], name='seo_kw_org_cooldown_idx'),
        ]

    def save(self, *args, **kwargs):
        # Auto-generate normalized keyword
        self.keyword_normalized = self.keyword.lower().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.keyword} ({self.organization.domain}) - {self.tier}"


class TopicFeedback(models.Model):
    """
    Explicit topic-level research memory captured from product UX.

    This is separate from ResearchedKeyword lifecycle status: a declined topic
    means "do not recommend this or close variants again for this organization"
    until restored.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='topic_feedback',
        db_index=True,
    )
    keyword = models.CharField(max_length=500)
    keyword_normalized = models.CharField(max_length=500, db_index=True)
    feedback_type = models.CharField(max_length=32, default='declined', db_index=True)
    reason_code = models.CharField(max_length=64, default='not_appropriate')
    reason_text = models.TextField(blank=True, null=True)
    decline_scope = models.CharField(max_length=32, default='similar')
    source = models.CharField(max_length=80, default='homepage_topic_card')
    session_id = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    restored_at = models.DateTimeField(blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'seo_topic_feedback'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'feedback_type', 'restored_at'], name='seo_tf_org_type_active_idx'),
            models.Index(fields=['organization', 'keyword_normalized'], name='seo_tf_org_keyword_idx'),
        ]

    def save(self, *args, **kwargs):
        self.keyword_normalized = " ".join(str(self.keyword or "").lower().strip().split())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.feedback_type}: {self.keyword} ({self.organization.domain})"


class KeywordVelocity(models.Model):
    """
    Trend velocity metrics for a keyword.

    Separate table to:
    1. Allow historical tracking (multiple snapshots)
    2. Store the daily_volumes array without bloating the main table
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='velocity_snapshots'
    )

    # Velocity metrics
    absolute_volume = models.IntegerField(default=0)
    velocity_score = models.FloatField(default=0.0, help_text="-1.0 to 1.0+")
    trend_status = models.CharField(
        max_length=20,
        choices=TrendStatus.choices,
        default=TrendStatus.STABLE
    )
    daily_volumes = models.JSONField(
        default=list,
        blank=True,
        help_text="Raw daily volume data from Glimpse/pytrends"
    )

    # Snapshot timestamp
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_keyword_velocity'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['keyword', 'captured_at']),
        ]

    def __str__(self):
        return f"{self.keyword.keyword} velocity: {self.velocity_score:.2f} ({self.trend_status})"


class AISaturation(models.Model):
    """
    AI saturation metrics for a keyword.

    Tracks SERP features that compete with organic clicks.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='ai_saturation_snapshots'
    )
    domain = models.CharField(max_length=255, blank=True, default='', db_index=True)

    # AI Overview detection
    ai_overview_present = models.BooleanField(default=False)
    ai_overview_quality = models.CharField(
        max_length=20,
        choices=[
            ('comprehensive', 'Comprehensive'),
            ('partial', 'Partial'),
            ('none', 'None'),
        ],
        default='none'
    )

    # Other SERP features
    featured_snippet_present = models.BooleanField(default=False)
    video_carousel_present = models.BooleanField(default=False)
    knowledge_panel_present = models.BooleanField(default=False)

    # Calculated score
    saturation_score = models.FloatField(
        default=0.0,
        help_text="0.0 (no AI) to 1.0 (fully saturated)"
    )

    # SERP hostility (combined metric)
    hostility_score = models.FloatField(default=0.0)
    hostility_recommendation = models.CharField(
        max_length=20,
        choices=[
            ('high_priority', 'High Priority'),
            ('pivot_angle', 'Pivot Angle'),
            ('low_priority', 'Low Priority'),
        ],
        default='high_priority'
    )
    organic_positions_above_fold = models.IntegerField(default=0)

    # Additional SERP features as flags
    serp_features = models.JSONField(
        default=list,
        blank=True,
        help_text="List of SERP feature types present"
    )

    # Snapshot timestamp
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_ai_saturation'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['keyword', 'captured_at']),
            models.Index(fields=['saturation_score']),
        ]

    def __str__(self):
        ai_status = "AI Overview" if self.ai_overview_present else "No AI"
        return f"{self.keyword.keyword}: {ai_status}, score={self.saturation_score:.2f}"


class PAQuestion(models.Model):
    """
    'People Also Ask' question with depth tracking.

    Normalized to support nested question relationships
    and efficient querying by keyword or question text.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='paa_questions'
    )
    domain = models.CharField(max_length=255, blank=True, default='', db_index=True)

    # Question content
    question = models.TextField()
    question_normalized = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Lowercase version for dedup"
    )
    answer_snippet = models.TextField(blank=True, default='')
    source_url = models.URLField(blank=True, null=True)

    # Depth tracking (1 = top-level, 2-4 = nested)
    depth = models.IntegerField(default=1)

    # AI presence in this specific PAA
    has_ai_overview = models.BooleanField(default=False)

    # Parent question (for nested structure)
    parent_question = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_questions'
    )

    # Order within parent
    order = models.IntegerField(default=0)

    # Timestamps
    discovered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'seo_paa_question'
        ordering = ['depth', 'order']
        indexes = [
            models.Index(fields=['keyword', 'depth']),
            models.Index(fields=['question_normalized']),
        ]

    def save(self, *args, **kwargs):
        self.question_normalized = self.question.lower().strip()[:500]
        super().save(*args, **kwargs)

    def __str__(self):
        depth_indicator = "  " * (self.depth - 1)
        return f"{depth_indicator}Q: {self.question[:80]}..."


class SemanticCluster(models.Model):
    """
    A cluster of semantically related keywords (pillar structure).

    Maps to content-factory's TopicMap.pillars structure.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='semantic_clusters'
    )

    # Cluster identity
    cluster_id = models.IntegerField(help_text="Local ID within the topic map")
    pillar_keyword = models.CharField(max_length=500, db_index=True)

    # Cluster metrics (aggregated from members)
    average_similarity = models.FloatField(default=0.0)
    total_volume = models.IntegerField(default=0)
    avg_difficulty = models.FloatField(default=0.0)
    avg_velocity = models.FloatField(default=0.0)

    # Assigned tier for the cluster
    topic_tier = models.CharField(
        max_length=30,
        choices=KeywordTier.choices,
        default=KeywordTier.TIER_4_DISCARD
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'seo_semantic_cluster'
        unique_together = ['organization', 'cluster_id']
        ordering = ['-total_volume']
        indexes = [
            models.Index(fields=['organization', 'topic_tier']),
            models.Index(fields=['pillar_keyword']),
        ]

    def __str__(self):
        return f"Cluster {self.cluster_id}: {self.pillar_keyword} ({self.topic_tier})"


class ClusterMembership(models.Model):
    """
    Many-to-many relationship between keywords and clusters.

    A keyword can belong to one cluster per organization.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        ResearchedKeyword,
        on_delete=models.CASCADE,
        related_name='cluster_memberships'
    )
    cluster = models.ForeignKey(
        SemanticCluster,
        on_delete=models.CASCADE,
        related_name='member_keywords'
    )

    # Whether this keyword is the pillar
    is_pillar = models.BooleanField(default=False)

    # Similarity to cluster centroid
    similarity_score = models.FloatField(default=0.0)

    class Meta:
        db_table = 'seo_cluster_membership'
        unique_together = ['keyword', 'cluster']
        indexes = [
            models.Index(fields=['cluster', 'is_pillar']),
        ]

    def __str__(self):
        pillar_marker = " (PILLAR)" if self.is_pillar else ""
        return f"{self.keyword.keyword} -> {self.cluster.pillar_keyword}{pillar_marker}"


class TopicMap(models.Model):
    """
    Complete topic map snapshot for an organization.

    Represents a research session's clustering results.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='topic_maps'
    )

    # Clustering parameters
    clustering_threshold = models.FloatField(default=0.85)
    total_keywords = models.IntegerField(default=0)

    # Unclustered keywords (JSON list of keyword IDs or text)
    unclustered_keywords = models.JSONField(default=list, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'seo_topic_map'
        ordering = ['-created_at']

    def __str__(self):
        return f"TopicMap for {self.organization.domain} ({self.created_at.date()})"


class ResearchSession(models.Model):
    """
    Tracks a research session for provenance and refresh tracking.

    Each time content-factory runs research, it creates a session.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='research_sessions'
    )

    # Session metadata
    seed_keywords_used = models.JSONField(default=list)
    competitors_analyzed = models.JSONField(default=list)

    # Statistics
    keywords_discovered = models.IntegerField(default=0)
    keywords_updated = models.IntegerField(default=0)
    clusters_created = models.IntegerField(default=0)

    # GEO settings used
    geo_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="GEO flags/thresholds used in this session"
    )

    # Timestamps
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'seo_research_session'
        ordering = ['-started_at']

    def __str__(self):
        return f"Research {self.organization.domain} @ {self.started_at.date()}"
