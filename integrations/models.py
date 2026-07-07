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
    RECORD_XERO_PAYMENT = "xero_payment"

    RECORD_TYPE_CHOICES = [
        (RECORD_BANK_TRANSACTION, "Bank Transaction"),
        (RECORD_XERO_REPEATING_INVOICE, "Xero Repeating Invoice"),
        (RECORD_XERO_INVOICE, "Xero Invoice"),
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
