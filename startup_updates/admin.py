from django.contrib import admin

from .models import (
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailSyncCursor,
    GmailThreadArtifact,
    MonthlyUpdateDraft,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupEvent,
    StartupMetricObservation,
    StartupProfile,
    UserStartupBinding,
)


@admin.register(StartupProfile)
class StartupProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "default_currency", "stage", "updated_at")
    search_fields = ("organization__name", "organization__domain", "stage")
    list_filter = ("default_currency", "updated_at")


@admin.register(UserStartupBinding)
class UserStartupBindingAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "google_connection", "role", "is_default_for_gmail", "updated_at")
    search_fields = ("user__email", "organization__domain", "role")
    list_filter = ("is_default_for_gmail", "updated_at")


@admin.register(GmailSyncCursor)
class GmailSyncCursorAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "google_connection",
        "last_history_id",
        "last_message_internal_date",
        "backfill_completed_at",
    )
    search_fields = ("organization__domain", "google_connection__google_email", "last_history_id")
    list_filter = ("backfill_completed_at", "updated_at")


@admin.register(GmailMessageArtifact)
class GmailMessageArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "gmail_message_id",
        "gmail_thread_id",
        "internal_date",
        "relevance_label",
        "heuristic_score",
    )
    search_fields = ("organization__domain", "gmail_message_id", "gmail_thread_id", "subject", "from_address")
    list_filter = ("relevance_label", "has_attachments", "internal_date")


@admin.register(GmailThreadArtifact)
class GmailThreadArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "gmail_thread_id",
        "source_message_count",
        "hydration_status",
        "extraction_status",
        "updated_at",
    )
    search_fields = ("organization__domain", "gmail_thread_id")
    list_filter = ("hydration_status", "extraction_status")


@admin.register(GmailAttachmentArtifact)
class GmailAttachmentArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "filename", "mime_type", "size_bytes", "extraction_status", "updated_at")
    search_fields = ("organization__domain", "filename", "mime_type", "gmail_attachment_id")
    list_filter = ("extraction_status", "mime_type")


@admin.register(SlackChannelSelection)
class SlackChannelSelectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "connection", "channel_name", "channel_id", "selected", "last_synced_at")
    search_fields = ("organization__domain", "channel_name", "channel_id", "connection__account_label")
    list_filter = ("selected", "is_private", "last_synced_at", "updated_at")


@admin.register(SlackMessageArtifact)
class SlackMessageArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "channel_name", "slack_message_ts", "author_name", "posted_at")
    search_fields = ("organization__domain", "channel_name", "channel_id", "slack_message_ts", "author_name", "text")
    list_filter = ("channel_name", "posted_at")


@admin.register(SlackThreadArtifact)
class SlackThreadArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "channel_name", "thread_ts", "source_message_count", "extraction_status", "latest_message_at")
    search_fields = ("organization__domain", "channel_name", "channel_id", "thread_ts")
    list_filter = ("extraction_status", "channel_name", "latest_message_at")


@admin.register(StartupMetricObservation)
class StartupMetricObservationAdmin(admin.ModelAdmin):
    list_display = ("organization", "metric_key", "source_provider", "period_month", "value_text", "confidence", "updated_at")
    search_fields = ("organization__domain", "metric_key", "metric_name", "value_text")
    list_filter = ("source_provider", "period_month")


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
