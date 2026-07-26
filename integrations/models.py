from django.conf import settings
from django.db import models
from .fields import EncryptedTextField


class CommunityBridgePlatform(models.TextChoices):
    SLACK = "slack", "Slack"
    DISCORD = "discord", "Discord"


class CommunityBridgeDeliveryType(models.TextChoices):
    CREATE = "create", "Create"
    EDIT = "edit", "Edit"
    DELETE = "delete", "Delete"


class CommunityBridgeDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    DEAD = "dead", "Dead"


class CommunityBridgeReceiptStatus(models.TextChoices):
    ACCEPTED = "accepted", "Accepted"
    ENQUEUED = "enqueued", "Enqueued"
    IGNORED = "ignored", "Ignored"
    DUPLICATE = "duplicate", "Duplicate"
    FAILED = "failed", "Failed"


class ExternalServiceProvider(models.TextChoices):
    STRIPE = "stripe", "Stripe"
    HUMANITIX = "humanitix", "Humanitix"
    XERO = "xero", "Xero"
    BANK_FEED = "bank_feed", "Bank Feed"
    NOTION = "notion", "Notion"
    GOOGLE_DRIVE = "google_drive", "Google Drive"
    SLACK = "slack", "Slack"
    LINEAR = "linear", "Linear"
    GOOGLE_ANALYTICS = "google_analytics", "Google Analytics"
    LUMA = "luma", "Luma"


class ExternalServiceConnectionStatus(models.TextChoices):
    CONNECTED = "connected", "Connected"
    SYNCING = "syncing", "Syncing"
    ERROR = "error", "Error"
    DISCONNECTED = "disconnected", "Disconnected"


class GoogleConnection(models.Model):
    # FK (not OneToOne) so one founder can connect a separate Gmail per startup.
    # The owning startup is `organization`; resolve the right connection with
    # external_connectors.active_google_connection / google_connection_for_org.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='google_connections')
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="google_connections",
        null=True,
        blank=True,
    )
    google_email = models.EmailField()
    refresh_token = EncryptedTextField()  # encrypted at rest
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One Gmail mailbox per (user, startup). NULL-organization rows
            # (legacy/unassigned) are treated as distinct by the DB and resolved
            # at the app layer.
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="uniq_google_connection_user_org",
            ),
        ]

    def __str__(self):
        return f"Google Connection for {self.user.email} ({self.google_email})"


class ExternalServiceConnection(models.Model):
    provider = models.CharField(max_length=32, choices=ExternalServiceProvider.choices, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_service_connections",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="external_service_connections",
        null=True,
        blank=True,
    )
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_type = models.CharField(max_length=64, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    external_account_id = models.CharField(max_length=255, blank=True, default="")
    account_label = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=ExternalServiceConnectionStatus.choices,
        default=ExternalServiceConnectionStatus.CONNECTED,
        db_index=True,
    )
    sync_cursor = models.JSONField(default=dict, blank=True)
    provider_metadata = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "provider"], name="extsvc_user_provider_idx"),
            models.Index(fields=["organization", "provider"], name="extsvc_org_provider_idx"),
            models.Index(fields=["provider", "external_account_id"], name="extsvc_provider_extid_idx"),
        ]
        constraints = [
            # One connection per (user, org, provider, external account). This is
            # the key _upsert_connection matches on, so a startup can hold its own
            # connection per provider without a sibling startup's connect
            # overwriting it. NULL organization rows (legacy/unassigned) are
            # treated as distinct by Postgres and are deduped at the app layer.
            models.UniqueConstraint(
                fields=["user", "organization", "provider", "external_account_id"],
                name="uniq_user_org_provider_account",
            ),
        ]
        ordering = ["provider", "-updated_at"]

    def __str__(self):
        account = self.account_label or self.external_account_id or "unknown"
        return f"{self.get_provider_display()} connection for {self.user_id} ({account})"


class FinancialAccount(models.Model):
    provider = models.CharField(max_length=32, choices=ExternalServiceProvider.choices, db_index=True)
    connection = models.ForeignKey(
        ExternalServiceConnection,
        on_delete=models.CASCADE,
        related_name="financial_accounts",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="financial_accounts",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="financial_accounts",
        null=True,
        blank=True,
    )
    external_account_id = models.CharField(max_length=255)
    account_label = models.CharField(max_length=255, blank=True, default="")
    institution_id = models.CharField(max_length=255, blank=True, default="")
    institution_name = models.CharField(max_length=255, blank=True, default="")
    account_type = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="")
    currency = models.CharField(max_length=12, blank=True, default="")
    balance = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    available_funds = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "connection", "external_account_id"],
                name="financial_account_provider_conn_extid_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "provider"], name="finacct_user_provider_idx"),
            models.Index(fields=["organization", "provider"], name="finacct_org_provider_idx"),
            models.Index(fields=["provider", "external_account_id"], name="finacct_provider_extid_idx"),
        ]
        ordering = ["provider", "account_label", "external_account_id"]

    def __str__(self):
        return self.account_label or self.external_account_id


class ExternalFinancialRecord(models.Model):
    RECORD_BANK_TRANSACTION = "bank_transaction"
    RECORD_XERO_REPEATING_INVOICE = "xero_repeating_invoice"
    RECORD_XERO_INVOICE = "xero_invoice"
    RECORD_XERO_BILL = "xero_bill"
    RECORD_XERO_PAYMENT = "xero_payment"

    RECORD_TYPE_CHOICES = [
        (RECORD_BANK_TRANSACTION, "Bank Transaction"),
        (RECORD_XERO_REPEATING_INVOICE, "Xero Repeating Invoice"),
        (RECORD_XERO_INVOICE, "Xero Invoice"),
        (RECORD_XERO_BILL, "Xero Bill"),
        (RECORD_XERO_PAYMENT, "Xero Payment"),
    ]

    provider = models.CharField(max_length=32, choices=ExternalServiceProvider.choices, db_index=True)
    record_type = models.CharField(
        max_length=64,
        choices=RECORD_TYPE_CHOICES,
        default=RECORD_BANK_TRANSACTION,
        db_index=True,
    )
    connection = models.ForeignKey(
        ExternalServiceConnection,
        on_delete=models.CASCADE,
        related_name="external_financial_records",
    )
    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.CASCADE,
        related_name="external_financial_records",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_financial_records",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="external_financial_records",
        null=True,
        blank=True,
    )
    external_record_id = models.CharField(max_length=255)
    external_account_id = models.CharField(max_length=255, blank=True, default="")
    currency = models.CharField(max_length=12, blank=True, default="")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    direction = models.CharField(max_length=16, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    transaction_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True, default="")
    merchant_name = models.CharField(max_length=255, blank=True, default="")
    category = models.CharField(max_length=255, blank=True, default="")
    class_name = models.CharField(max_length=255, blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_account_id", "external_record_id"],
                name="financial_record_provider_account_extid_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["connection", "record_type"], name="finrec_connection_type_idx"),
            models.Index(fields=["financial_account", "posted_at"], name="finrec_account_posted_idx"),
            models.Index(fields=["provider", "status"], name="finrec_provider_status_idx"),
            models.Index(fields=["transaction_date"], name="finrec_transaction_date_idx"),
        ]
        ordering = ["-posted_at", "-transaction_date", "-id"]

    def __str__(self):
        return f"{self.provider}:{self.external_record_id}"


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


class GitHubInstallation(models.Model):
    """A founder's GitHub App installation, shared across all their companies.

    GitHub access is intentionally per-founder — the inverse of Gmail / financial
    connectors (which are isolated per startup). A founder keeps their startups'
    repos under one or a few GitHub accounts, so an installation authorized while
    setting up one company is reused by every other company of the same user.
    One row per (user, installation); a founder may connect several (e.g. a
    personal account plus a GitHub org).

    Real repo operations mint short-lived GitHub App *installation* tokens from
    ``installation_id`` alone (``github_app.create_installation_access_token``),
    so the stored user token here is only for repo listing / fallback, not the
    source of truth for write access.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="github_installations",
    )
    installation_id = models.CharField(max_length=50, db_index=True)
    account_login = models.CharField(max_length=255, blank=True, default="")
    account_type = models.CharField(max_length=32, blank=True, default="")
    github_user_name = models.TextField(blank=True, default="")
    github_user_token_encrypted = EncryptedTextField(null=True, blank=True)
    github_refresh_token_encrypted = EncryptedTextField(null=True, blank=True)
    github_token_expires_at = models.DateTimeField(null=True, blank=True)
    github_scopes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # When the reconciliation sweep last verified this installation is still
    # live against GitHub. Throttles re-probing (see
    # integrations.services.github_installations.run_github_installation_reconciliation_sweep);
    # null means never probed.
    liveness_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One row per (founder, installation). A founder may hold several
            # installations (multiple GitHub accounts/orgs); resolve the union
            # with integrations.services.github_installations.
            models.UniqueConstraint(
                fields=["user", "installation_id"],
                name="uniq_github_installation_user_install",
            ),
        ]
        ordering = ["user_id", "account_login", "installation_id"]

    def __str__(self):
        return (
            f"GitHubInstallation({self.installation_id} / "
            f"{self.account_login or self.github_user_name}) for user {self.user_id}"
        )


from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    GroundednessStatus,
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupEvent,
    StartupEventDatePrecision,
    StartupEventType,
    StartupMetricObservation,
    StartupProfile,
    UserStartupBinding,
)


class CommunityBridgeChannel(models.Model):
    slack_channel_id = models.CharField(max_length=100, unique=True, db_index=True)
    slack_channel_name = models.CharField(max_length=255, blank=True, default="")
    discord_guild_id = models.CharField(max_length=100, blank=True, default="")
    discord_channel_id = models.CharField(max_length=100, unique=True, db_index=True)
    discord_channel_name = models.CharField(max_length=255, blank=True, default="")
    enabled = models.BooleanField(default=True, db_index=True)
    sync_edits = models.BooleanField(default=True)
    sync_deletes = models.BooleanField(default=True)
    sync_replies = models.BooleanField(default=True)
    pilot_settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "community_bridge_channel"
        ordering = ["slack_channel_id"]

    def __str__(self):
        slack_label = self.slack_channel_name or self.slack_channel_id
        discord_label = self.discord_channel_name or self.discord_channel_id
        return f"{slack_label} -> {discord_label}"


class CommunityBridgeReceipt(models.Model):
    channel = models.ForeignKey(
        CommunityBridgeChannel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
    )
    platform = models.CharField(max_length=20, choices=CommunityBridgePlatform.choices, db_index=True)
    receipt_key = models.CharField(max_length=255)
    event_type = models.CharField(max_length=50, blank=True, default="")
    source_channel_id = models.CharField(max_length=100, blank=True, default="")
    source_message_id = models.CharField(max_length=100, blank=True, default="")
    source_parent_message_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=CommunityBridgeReceiptStatus.choices,
        default=CommunityBridgeReceiptStatus.ACCEPTED,
        db_index=True,
    )
    queued_delivery_count = models.PositiveSmallIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    error_text = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "community_bridge_receipt"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "receipt_key"],
                name="community_bridge_receipt_platform_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["platform", "status", "created_at"], name="bridge_rcpt_status_idx"),
            models.Index(fields=["source_channel_id", "source_message_id"], name="bridge_rcpt_msg_idx"),
        ]

    def __str__(self):
        return f"{self.platform}:{self.receipt_key}"


class CommunityBridgeMessageLink(models.Model):
    channel = models.ForeignKey(
        CommunityBridgeChannel,
        on_delete=models.CASCADE,
        related_name="message_links",
    )
    source_platform = models.CharField(max_length=20, choices=CommunityBridgePlatform.choices, db_index=True)
    source_channel_id = models.CharField(max_length=100, db_index=True)
    source_message_id = models.CharField(max_length=100, db_index=True)
    source_parent_message_id = models.CharField(max_length=100, blank=True, default="")
    source_author_id = models.CharField(max_length=100, blank=True, default="")
    destination_platform = models.CharField(max_length=20, choices=CommunityBridgePlatform.choices, db_index=True)
    destination_channel_id = models.CharField(max_length=100, db_index=True)
    destination_message_id = models.CharField(max_length=100, db_index=True)
    destination_parent_message_id = models.CharField(max_length=100, blank=True, default="")
    source_payload = models.JSONField(default=dict, blank=True)
    destination_payload = models.JSONField(default=dict, blank=True)
    source_deleted_at = models.DateTimeField(null=True, blank=True)
    destination_deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "community_bridge_message_link"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "source_platform",
                    "source_channel_id",
                    "source_message_id",
                    "destination_platform",
                ],
                name="community_bridge_link_source_unique",
            ),
            models.UniqueConstraint(
                fields=[
                    "destination_platform",
                    "destination_channel_id",
                    "destination_message_id",
                    "source_platform",
                ],
                name="community_bridge_link_destination_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["source_platform", "source_channel_id", "source_message_id"],
                name="bridge_link_source_lookup_idx",
            ),
            models.Index(
                fields=["destination_platform", "destination_channel_id", "destination_message_id"],
                name="bridge_link_dest_lookup_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.source_platform}:{self.source_message_id}"
            f" -> {self.destination_platform}:{self.destination_message_id}"
        )


class CommunityBridgeDelivery(models.Model):
    channel = models.ForeignKey(
        CommunityBridgeChannel,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    receipt = models.ForeignKey(
        CommunityBridgeReceipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
    )
    target_platform = models.CharField(max_length=20, choices=CommunityBridgePlatform.choices, db_index=True)
    source_platform = models.CharField(max_length=20, choices=CommunityBridgePlatform.choices, db_index=True)
    delivery_type = models.CharField(max_length=20, choices=CommunityBridgeDeliveryType.choices)
    status = models.CharField(
        max_length=20,
        choices=CommunityBridgeDeliveryStatus.choices,
        default=CommunityBridgeDeliveryStatus.PENDING,
        db_index=True,
    )
    source_event_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    source_channel_id = models.CharField(max_length=100, blank=True, default="")
    source_message_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    source_parent_message_id = models.CharField(max_length=100, blank=True, default="")
    target_channel_id = models.CharField(max_length=100, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    available_at = models.DateTimeField(db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "community_bridge_delivery"
        ordering = ["available_at", "id"]
        indexes = [
            models.Index(fields=["status", "available_at"], name="bridge_dlv_ready_idx"),
            models.Index(fields=["target_platform", "status", "available_at"], name="bridge_dlv_tgt_idx"),
            models.Index(fields=["source_platform", "source_message_id"], name="bridge_dlv_src_msg_idx"),
        ]

    def __str__(self):
        return (
            f"{self.delivery_type}:{self.source_platform}:{self.source_message_id}"
            f" -> {self.target_platform} ({self.status})"
        )


class ReconciliationProfile(models.Model):
    """Per-organisation accounting policy for Stripe payout reconciliation.

    Account codes and tax types are deliberately configuration, never inferred
    from Stripe or Luma.  That keeps the integration from making tax decisions
    on behalf of the organisation's accountant.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="reconciliation_profile",
    )
    xero_connection = models.ForeignKey(
        ExternalServiceConnection,
        on_delete=models.SET_NULL,
        related_name="reconciliation_profiles",
        null=True,
        blank=True,
    )
    stripe_account_id = models.CharField(max_length=255, blank=True, default="")
    xero_bank_account_id = models.CharField(max_length=255, blank=True, default="")
    xero_bank_account_name = models.CharField(max_length=255, blank=True, default="")
    xero_contact_id = models.CharField(max_length=255, blank=True, default="")
    xero_contact_name = models.CharField(max_length=255, default="Stripe Payments")
    humanitix_contact_id = models.CharField(max_length=255, blank=True, default="")
    humanitix_contact_name = models.CharField(max_length=255, default="Humanitix")
    revenue_account_code = models.CharField(max_length=64, blank=True, default="")
    fee_account_code = models.CharField(max_length=64, blank=True, default="")
    refund_account_code = models.CharField(max_length=64, blank=True, default="")
    revenue_tax_type = models.CharField(max_length=64, blank=True, default="")
    fee_tax_type = models.CharField(max_length=64, blank=True, default="")
    refund_tax_type = models.CharField(max_length=64, blank=True, default="")
    line_amount_types = models.CharField(max_length=16, default="Inclusive")
    event_tracking_category_id = models.CharField(max_length=255, blank=True, default="")
    event_tracking_category_name = models.CharField(max_length=255, default="Event Name")
    project_tracking_category_id = models.CharField(max_length=255, blank=True, default="")
    project_tracking_category_name = models.CharField(max_length=255, default="Project Name")
    standalone_fee_project_option_id = models.CharField(max_length=255, blank=True, default="")
    standalone_fee_project_option_name = models.CharField(max_length=255, blank=True, default="")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stripe_xero_reconciliation_profile"


class ReconciliationMapping(models.Model):
    """Maps an immutable Stripe/Luma source ID to approved Xero dimensions."""

    SOURCE_LUMA_EVENT = "luma_event"
    SOURCE_HUMANITIX_EVENT = "humanitix_event"
    SOURCE_HUMANITIX_TICKET_TYPE = "humanitix_ticket_type"
    SOURCE_HUMANITIX_PAYOUT = "humanitix_payout"
    SOURCE_STRIPE_PRODUCT = "stripe_product"
    SOURCE_STRIPE_INVOICE = "stripe_invoice"
    SOURCE_STRIPE_METADATA = "stripe_metadata"
    SOURCE_UNATTRIBUTED = "unattributed"
    SOURCE_CHOICES = [
        (SOURCE_LUMA_EVENT, "Luma event"),
        (SOURCE_HUMANITIX_EVENT, "Humanitix event"),
        (SOURCE_HUMANITIX_TICKET_TYPE, "Humanitix ticket type"),
        (SOURCE_HUMANITIX_PAYOUT, "Humanitix payout"),
        (SOURCE_STRIPE_PRODUCT, "Stripe product"),
        (SOURCE_STRIPE_INVOICE, "Stripe invoice"),
        (SOURCE_STRIPE_METADATA, "Stripe metadata"),
        (SOURCE_UNATTRIBUTED, "Unattributed Stripe transaction"),
    ]
    TREATMENT_REVENUE = "revenue"
    TREATMENT_CLEARING = "clearing"
    TREATMENT_CHOICES = [
        (TREATMENT_REVENUE, "Recognise revenue"),
        (TREATMENT_CLEARING, "Clear revenue recorded elsewhere"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="reconciliation_mappings",
    )
    source_type = models.CharField(max_length=32, choices=SOURCE_CHOICES)
    source_id = models.CharField(max_length=255)
    source_label = models.CharField(max_length=500, blank=True, default="")
    accounting_treatment = models.CharField(
        max_length=16,
        choices=TREATMENT_CHOICES,
        blank=True,
        default="",
    )
    event_tracking_option_id = models.CharField(max_length=255, blank=True, default="")
    event_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    project_tracking_option_id = models.CharField(max_length=255, blank=True, default="")
    project_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    project_source_type = models.CharField(max_length=32, blank=True, default="")
    project_source_id = models.CharField(max_length=255, blank=True, default="")
    reconciliation_note = models.TextField(blank=True, default="")
    account_code = models.CharField(max_length=64, blank=True, default="")
    tax_type = models.CharField(max_length=64, blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stripe_xero_reconciliation_mapping"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "source_id"],
                name="recon_mapping_org_source_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "active"], name="recon_mapping_org_active_idx"),
        ]


class HumanitixEvent(models.Model):
    """PII-free Humanitix event catalogue used for accounting attribution."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="humanitix_events",
    )
    connection = models.ForeignKey(
        ExternalServiceConnection,
        on_delete=models.CASCADE,
        related_name="humanitix_events",
    )
    external_event_id = models.CharField(max_length=255)
    event_name = models.CharField(max_length=500)
    event_url = models.URLField(max_length=1000, blank=True, default="")
    currency = models.CharField(max_length=12, blank=True, default="")
    timezone_name = models.CharField(max_length=100, blank=True, default="")
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    total_capacity = models.PositiveIntegerField(null=True, blank=True)
    published = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    source_payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "humanitix_event"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "external_event_id"],
                name="humanitix_event_org_external_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "start_at"], name="humanitix_event_org_start_idx"),
            models.Index(fields=["connection", "last_synced_at"], name="humanitix_event_conn_sync_idx"),
        ]
        ordering = ["-start_at", "event_name", "external_event_id"]

    def __str__(self):
        return self.event_name or self.external_event_id


class HumanitixEventFinancialSummary(models.Model):
    """Aggregate event finances; deliberately excludes buyer/attendee PII."""

    event = models.OneToOneField(
        HumanitixEvent,
        on_delete=models.CASCADE,
        related_name="financial_summary",
    )
    order_count = models.PositiveIntegerField(default=0)
    paid_order_count = models.PositiveIntegerField(default=0)
    free_order_count = models.PositiveIntegerField(default=0)
    ticket_count = models.PositiveIntegerField(default=0)
    gross_sales = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_sales = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    refunds = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    discounts = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    donations = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    humanitix_fees = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    absorbed_fees = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    taxes = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    gateway_breakdown = models.JSONField(default=dict, blank=True)
    ticket_type_breakdown = models.JSONField(default=dict, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "humanitix_event_financial_summary"


class HumanitixPayout(models.Model):
    """Durable import and preview state for a Humanitix-native payout."""

    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_READY = "ready"
    STATUS_POSTING = "posting"
    STATUS_POSTED = "posted"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_READY, "Ready"),
        (STATUS_POSTING, "Posting"),
        (STATUS_POSTED, "Posted"),
        (STATUS_FAILED, "Failed"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="humanitix_payouts",
    )
    connection = models.ForeignKey(
        ExternalServiceConnection,
        on_delete=models.CASCADE,
        related_name="humanitix_payouts",
    )
    payout_reference = models.CharField(max_length=255)
    payout_date = models.DateField(null=True, blank=True)
    cleared_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=12, blank=True, default="")
    payout_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    humanitix_sales = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    box_office_card_sales = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    refunds = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    absorbed_fees = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    adjustments = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    source_payload = models.JSONField(default=dict, blank=True)
    preview_payload = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_NEEDS_REVIEW,
        db_index=True,
    )
    approved_by_slack_id = models.CharField(max_length=100, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    xero_bank_transaction_id = models.CharField(max_length=255, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "humanitix_payout"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "payout_reference"],
                name="humanitix_payout_org_reference_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "payout_date"], name="humanitix_payout_org_date_idx"),
        ]
        ordering = ["-payout_date", "-id"]

    def __str__(self):
        return self.payout_reference


class HumanitixPayoutLine(models.Model):
    COMPONENT_TICKET_SALES = "ticket_sales"
    COMPONENT_DONATIONS = "donations"
    COMPONENT_ADD_ONS = "add_ons"
    COMPONENT_REFUNDS = "refunds"
    COMPONENT_ABSORBED_FEES = "absorbed_fees"
    COMPONENT_ADJUSTMENTS = "adjustments"
    COMPONENT_NET_PAYOUT = "net_payout"
    COMPONENT_CHOICES = [
        (COMPONENT_TICKET_SALES, "Ticket sales"),
        (COMPONENT_DONATIONS, "Donations"),
        (COMPONENT_ADD_ONS, "Add-ons"),
        (COMPONENT_REFUNDS, "Refunds"),
        (COMPONENT_ABSORBED_FEES, "Absorbed fees"),
        (COMPONENT_ADJUSTMENTS, "Adjustments"),
        (COMPONENT_NET_PAYOUT, "Net payout"),
    ]

    payout = models.ForeignKey(
        HumanitixPayout,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    event = models.ForeignKey(
        HumanitixEvent,
        on_delete=models.SET_NULL,
        related_name="payout_lines",
        null=True,
        blank=True,
    )
    source_line_key = models.CharField(max_length=255)
    external_event_id = models.CharField(max_length=255, blank=True, default="")
    event_name = models.CharField(max_length=500, blank=True, default="")
    component = models.CharField(max_length=32, choices=COMPONENT_CHOICES)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "humanitix_payout_line"
        constraints = [
            models.UniqueConstraint(
                fields=["payout", "source_line_key"],
                name="humanitix_payout_line_source_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["event", "component"], name="humanitix_line_event_comp_idx"),
        ]
        ordering = ["payout_id", "source_line_key"]


class StripePayoutReconciliation(models.Model):
    """Durable, idempotent ledger and posting state for one Stripe payout."""

    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_READY = "ready"
    STATUS_POSTING = "posting"
    STATUS_POSTED = "posted"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_READY, "Ready"),
        (STATUS_POSTING, "Posting"),
        (STATUS_POSTED, "Posted"),
        (STATUS_FAILED, "Failed"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="stripe_payout_reconciliations",
    )
    stripe_account_id = models.CharField(max_length=255, blank=True, default="")
    payout_id = models.CharField(max_length=255)
    arrival_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=12, blank=True, default="")
    amount_cents = models.BigIntegerField(default=0)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    report_payload = models.JSONField(default=dict, blank=True)
    preview_payload = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_NEEDS_REVIEW, db_index=True)
    approved_by_slack_id = models.CharField(max_length=100, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    xero_bank_transaction_id = models.CharField(max_length=255, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stripe_payout_reconciliation"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "stripe_account_id", "payout_id"],
                name="recon_payout_org_account_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "arrival_date"], name="recon_payout_org_arrival_idx"),
        ]


class ReconciliationSuggestion(models.Model):
    """Evidence-backed Event/Project proposal produced by the monthly-update agent.

    Suggestions are deliberately separate from approved mappings.  Valley can
    propose dimensions and a review note, but only the reconciliation approval
    endpoint may copy those values onto :class:`ReconciliationMapping`.
    """

    STATUS_PROPOSED = "proposed"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Proposed"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUPERSEDED, "Superseded"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="reconciliation_suggestions",
    )
    payout = models.ForeignKey(
        StripePayoutReconciliation,
        on_delete=models.CASCADE,
        related_name="suggestions",
    )
    run_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    source_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=255)
    source_label = models.CharField(max_length=500, blank=True, default="")
    event_source_type = models.CharField(max_length=32, blank=True, default="")
    event_source_id = models.CharField(max_length=255, blank=True, default="")
    event_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    project_source_type = models.CharField(max_length=32, blank=True, default="")
    project_source_id = models.CharField(max_length=255, blank=True, default="")
    project_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    rationale = models.TextField(blank=True, default="")
    review_note = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=list, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PROPOSED, db_index=True)
    reviewed_by_slack_id = models.CharField(max_length=100, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stripe_xero_reconciliation_suggestion"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "payout", "run_id", "source_type", "source_id"],
                name="recon_suggest_org_payout_run_source_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="recon_suggest_org_status_idx"),
            models.Index(fields=["organization", "source_type", "source_id"], name="recon_suggest_org_source_idx"),
        ]


class XeroStatementLineSnapshot(models.Model):
    """A browser-observed unreconciled Xero statement line.

    Xero's Accounting API does not expose the bank-reconciliation statement
    queue, so explicit founder-run browser backfills import immutable line
    identifiers and the visible draft fields.  No browser import posts or
    reconciles anything.
    """

    DIRECTION_DEBIT = "debit"
    DIRECTION_CREDIT = "credit"
    DIRECTION_CHOICES = [
        (DIRECTION_DEBIT, "Debit"),
        (DIRECTION_CREDIT, "Credit"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="xero_statement_line_snapshots",
    )
    bank_account_id = models.CharField(max_length=255)
    statement_line_id = models.CharField(max_length=255)
    transaction_date = models.DateField()
    narration = models.TextField(blank=True, default="")
    reference = models.CharField(max_length=500, blank=True, default="")
    direction = models.CharField(max_length=16, choices=DIRECTION_CHOICES)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=12, blank=True, default="AUD")
    current_contact = models.CharField(max_length=255, blank=True, default="")
    current_account = models.CharField(max_length=255, blank=True, default="")
    current_description = models.TextField(blank=True, default="")
    current_event_name = models.CharField(max_length=255, blank=True, default="")
    current_project_name = models.CharField(max_length=255, blank=True, default="")
    current_tax_type = models.CharField(max_length=255, blank=True, default="")
    ready_in_xero = models.BooleanField(default=False, db_index=True)
    active = models.BooleanField(default=True, db_index=True)
    source_hash = models.CharField(max_length=64)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "xero_statement_line_snapshot"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "bank_account_id", "statement_line_id"],
                name="xero_stmt_org_account_line_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "active", "ready_in_xero"], name="xero_stmt_org_queue_idx"),
            models.Index(fields=["organization", "transaction_date"], name="xero_stmt_org_date_idx"),
        ]


class XeroStatementSuggestion(models.Model):
    """Guarded proposal for prefilling one Xero bank-reconciliation row."""

    ACTION_CREATE_BANK_TRANSACTION = "create_bank_transaction"
    ACTION_PAY_EXISTING_BILL = "pay_existing_bill"
    # Legacy values remain readable while older Valley runs and browser
    # backfills drain from the queue. New runs use the explicit API actions.
    ACTION_PREFILL_CREATE = "prefill_create"
    ACTION_MATCH_BILL = "match_existing_bill"
    ACTION_NEEDS_REVIEW = "needs_review"
    ACTION_CHOICES = [
        (ACTION_CREATE_BANK_TRANSACTION, "Create Bank Transaction"),
        (ACTION_PAY_EXISTING_BILL, "Pay Existing Bill"),
        (ACTION_PREFILL_CREATE, "Prefill Create"),
        (ACTION_MATCH_BILL, "Match Existing Bill"),
        (ACTION_NEEDS_REVIEW, "Needs Review"),
    ]
    STATUS_PROPOSED = "proposed"
    STATUS_APPLIED = "applied"
    STATUS_REJECTED = "rejected"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Proposed"),
        (STATUS_APPLIED, "Applied to Xero form"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUPERSEDED, "Superseded"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="xero_statement_suggestions",
    )
    statement_line = models.ForeignKey(
        XeroStatementLineSnapshot,
        on_delete=models.CASCADE,
        related_name="suggestions",
    )
    run_id = models.CharField(max_length=255, db_index=True)
    proposed_action = models.CharField(max_length=32, choices=ACTION_CHOICES, default=ACTION_NEEDS_REVIEW)
    contact_name = models.CharField(max_length=255, blank=True, default="")
    account_code = models.CharField(max_length=64, blank=True, default="")
    account_name = models.CharField(max_length=255, blank=True, default="")
    tax_type = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    event_source_id = models.CharField(max_length=255, blank=True, default="")
    event_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    project_source_id = models.CharField(max_length=255, blank=True, default="")
    project_tracking_option_name = models.CharField(max_length=255, blank=True, default="")
    matched_xero_bill_id = models.CharField(max_length=255, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    rationale = models.TextField(blank=True, default="")
    review_note = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=list, blank=True)
    source_hash = models.CharField(max_length=64)
    model_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PROPOSED, db_index=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "xero_statement_suggestion"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "statement_line", "run_id"],
                name="xero_stmt_suggest_org_line_run_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="xero_stmt_suggest_status_idx"),
        ]


class XeroStatementPosting(models.Model):
    """Idempotent Xero write for one observed bank-statement line.

    The public Xero API can create the matching accounting object but cannot
    reconcile a bank-statement line. ``match_ready`` therefore means the API
    write succeeded and the row is ready for the founder's final Xero "OK".
    """

    OPERATION_BANK_TRANSACTION = "bank_transaction"
    OPERATION_BILL_PAYMENT = "bill_payment"
    OPERATION_CHOICES = [
        (OPERATION_BANK_TRANSACTION, "Bank Transaction"),
        (OPERATION_BILL_PAYMENT, "Bill Payment"),
    ]
    STATUS_PREVIEWED = "previewed"
    STATUS_READY = "ready"
    STATUS_POSTING = "posting"
    STATUS_MATCH_READY = "match_ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PREVIEWED, "Previewed"),
        (STATUS_READY, "Ready"),
        (STATUS_POSTING, "Posting"),
        (STATUS_MATCH_READY, "Ready to Match"),
        (STATUS_FAILED, "Failed"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="xero_statement_postings",
    )
    statement_line = models.ForeignKey(
        XeroStatementLineSnapshot,
        on_delete=models.PROTECT,
        related_name="postings",
    )
    suggestion = models.ForeignKey(
        XeroStatementSuggestion,
        on_delete=models.PROTECT,
        related_name="postings",
    )
    operation = models.CharField(max_length=32, choices=OPERATION_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PREVIEWED, db_index=True)
    source_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=128, unique=True)
    preview_payload = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    requested_by_slack_id = models.CharField(max_length=100, blank=True, default="")
    automatic = models.BooleanField(default=False)
    xero_bank_transaction_id = models.CharField(max_length=255, blank=True, default="")
    xero_payment_id = models.CharField(max_length=255, blank=True, default="")
    xero_bill_id = models.CharField(max_length=255, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "xero_statement_posting"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "statement_line", "source_hash"],
                name="xero_stmt_post_org_line_hash_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="xero_stmt_post_status_idx"),
        ]


class LinearIssueCreationReceipt(models.Model):
    """Durable idempotency receipt for Roo-created Linear issues."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    idempotency_key = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    request_payload = models.JSONField(default=dict, blank=True)
    linear_issue_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "linear_issue_creation_receipt"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.idempotency_key}:{self.status}"
