from django.contrib import admin
from .models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    ExternalFinancialRecord,
    FinancialAccount,
    FinancialConnection,
    CommunityBridgeMessageLink,
    CommunityBridgeReceipt,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailSyncCursor,
    GmailThreadArtifact,
    GoogleConnection,
    MonthlyUpdateDraft,
    MonthlyRevenueSnapshot,
    StartupEvent,
    StartupMetricObservation,
    StartupProfile,
    UserIntegration,
    UserStartupBinding,
)

@admin.register(UserIntegration)
class UserIntegrationAdmin(admin.ModelAdmin):
    list_display = ('slack_user_id', 'github_user_name', 'github_repo', 'project_scanned', 'last_scanned_at', 'updated_at')
    search_fields = ('slack_user_id', 'github_user_name', 'github_repo')
    list_filter = ('project_scanned', 'updated_at')
    readonly_fields = ('updated_at', 'last_scanned_at', 'last_scanned_sha')


@admin.register(CommunityBridgeChannel)
class CommunityBridgeChannelAdmin(admin.ModelAdmin):
    list_display = (
        "slack_channel_id",
        "slack_channel_name",
        "discord_channel_id",
        "discord_channel_name",
        "enabled",
        "updated_at",
    )
    search_fields = (
        "slack_channel_id",
        "slack_channel_name",
        "discord_channel_id",
        "discord_channel_name",
        "discord_guild_id",
    )
    list_filter = ("enabled", "sync_edits", "sync_deletes", "sync_replies", "updated_at")


@admin.register(CommunityBridgeReceipt)
class CommunityBridgeReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "platform",
        "receipt_key",
        "event_type",
        "source_channel_id",
        "source_message_id",
        "status",
        "queued_delivery_count",
        "created_at",
    )
    search_fields = ("receipt_key", "source_channel_id", "source_message_id", "event_type")
    list_filter = ("platform", "status", "event_type", "created_at")
    readonly_fields = ("created_at", "updated_at", "processed_at")


@admin.register(CommunityBridgeMessageLink)
class CommunityBridgeMessageLinkAdmin(admin.ModelAdmin):
    list_display = (
        "channel",
        "source_platform",
        "source_message_id",
        "destination_platform",
        "destination_message_id",
        "source_parent_message_id",
        "destination_parent_message_id",
        "updated_at",
    )
    search_fields = (
        "source_message_id",
        "destination_message_id",
        "source_channel_id",
        "destination_channel_id",
    )
    list_filter = ("source_platform", "destination_platform", "updated_at")
    readonly_fields = ("created_at", "updated_at", "source_deleted_at", "destination_deleted_at")


@admin.register(CommunityBridgeDelivery)
class CommunityBridgeDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "delivery_type",
        "source_platform",
        "target_platform",
        "source_message_id",
        "target_channel_id",
        "status",
        "attempts",
        "available_at",
        "updated_at",
    )
    search_fields = ("source_event_key", "source_message_id", "target_channel_id", "last_error")
    list_filter = ("delivery_type", "source_platform", "target_platform", "status", "updated_at")
    readonly_fields = ("created_at", "updated_at", "locked_at", "completed_at")

@admin.register(GoogleConnection)
class GoogleConnectionAdmin(admin.ModelAdmin):
    list_display = ('user', 'google_email', 'updated_at')
    search_fields = ('user__email', 'google_email')


@admin.register(StartupProfile)
class StartupProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "default_currency", "updated_at")
    search_fields = ("organization__name", "organization__domain")


@admin.register(FinancialConnection)
class FinancialConnectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "provider", "display_name", "status", "last_synced_at", "updated_at")
    search_fields = ("organization__domain", "display_name", "external_account_id", "user__email")
    list_filter = ("provider", "status", "updated_at")
    readonly_fields = ("connected_at", "created_at", "updated_at", "last_synced_at")


@admin.register(FinancialAccount)
class FinancialAccountAdmin(admin.ModelAdmin):
    list_display = ("organization", "provider", "display_name", "currency", "account_type", "selected_for_revenue")
    search_fields = ("organization__domain", "display_name", "external_account_id")
    list_filter = ("provider", "selected_for_revenue", "account_type")


@admin.register(ExternalFinancialRecord)
class ExternalFinancialRecordAdmin(admin.ModelAdmin):
    list_display = ("organization", "provider", "object_type", "external_id", "source_status", "amount", "currency", "period_start")
    search_fields = ("organization__domain", "external_id", "customer_ref")
    list_filter = ("provider", "object_type", "source_status", "currency")
    readonly_fields = ("raw_hash", "created_at", "updated_at")


@admin.register(MonthlyRevenueSnapshot)
class MonthlyRevenueSnapshotAdmin(admin.ModelAdmin):
    list_display = ("organization", "month", "currency", "mrr_amount", "mrr_growth_rate", "cash_collected_amount", "confidence")
    search_fields = ("organization__domain",)
    list_filter = ("currency", "month", "calculated_at")


@admin.register(UserStartupBinding)
class UserStartupBindingAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "google_connection", "role", "is_default_for_gmail", "updated_at")
    search_fields = ("user__email", "organization__domain", "role")
    list_filter = ("is_default_for_gmail", "updated_at")


@admin.register(GmailSyncCursor)
class GmailSyncCursorAdmin(admin.ModelAdmin):
    list_display = ("organization", "google_connection", "last_history_id", "last_message_internal_date", "backfill_completed_at")
    search_fields = ("organization__domain", "google_connection__google_email")


@admin.register(GmailMessageArtifact)
class GmailMessageArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "gmail_message_id", "gmail_thread_id", "internal_date", "relevance_label", "heuristic_score")
    search_fields = ("organization__domain", "gmail_message_id", "gmail_thread_id", "subject", "from_address")
    list_filter = ("relevance_label", "has_attachments", "internal_date")


@admin.register(GmailThreadArtifact)
class GmailThreadArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "gmail_thread_id", "source_message_count", "hydration_status", "extraction_status", "updated_at")
    search_fields = ("organization__domain", "gmail_thread_id")
    list_filter = ("hydration_status", "extraction_status")


@admin.register(GmailAttachmentArtifact)
class GmailAttachmentArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "filename", "mime_type", "size_bytes", "extraction_status", "updated_at")
    search_fields = ("organization__domain", "filename", "mime_type", "gmail_attachment_id")
    list_filter = ("extraction_status", "mime_type")


@admin.register(StartupMetricObservation)
class StartupMetricObservationAdmin(admin.ModelAdmin):
    list_display = ("organization", "metric_key", "period_month", "value_text", "confidence", "updated_at")
    search_fields = ("organization__domain", "metric_key", "metric_name", "value_text")
    list_filter = ("period_month",)


@admin.register(StartupEvent)
class StartupEventAdmin(admin.ModelAdmin):
    list_display = ("organization", "canonical_key", "event_type", "month_bucket", "investor_importance", "needs_review")
    search_fields = ("organization__domain", "canonical_key", "title", "summary")
    list_filter = ("event_type", "month_bucket", "needs_review")


@admin.register(MonthlyUpdateDraft)
class MonthlyUpdateDraftAdmin(admin.ModelAdmin):
    list_display = ("organization", "month", "status", "groundedness_status", "model_name", "updated_at")
    search_fields = ("organization__domain", "title", "model_name")
    list_filter = ("status", "groundedness_status", "month")
