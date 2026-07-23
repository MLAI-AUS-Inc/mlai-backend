from django.contrib import admin

from .models import (
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailSyncCursor,
    GmailThreadArtifact,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    MonthlyUpdateDraft,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupDataDeletionRequest,
    StartupEvent,
    StartupManualDocument,
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
    list_display = (
        "user",
        "organization",
        "google_connection",
        "role",
        "is_default_for_gmail",
        "coworking_discount_eligible",
        "updated_at",
    )
    search_fields = ("user__email", "organization__domain", "role")
    list_filter = ("is_default_for_gmail", "coworking_discount_eligible", "updated_at")


@admin.register(StartupManualDocument)
class StartupManualDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "company",
        "original_filename",
        "content_type",
        "file_size_bytes",
        "extraction_status",
        "created_by",
        "created_at",
    )
    search_fields = ("organization__domain", "company__name", "original_filename", "created_by__email")
    list_filter = ("extraction_status", "content_type", "created_at")
    readonly_fields = ("storage_path", "parse_notes", "last_error", "metadata")


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
    readonly_fields = ("last_error",)


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
    list_display = (
        "organization",
        "channel_name",
        "thread_ts",
        "source_message_count",
        "relevance_label",
        "heuristic_score",
        "relevance_score",
        "extraction_status",
        "latest_message_at",
    )
    search_fields = ("organization__domain", "channel_name", "channel_id", "thread_ts", "cleaned_text")
    list_filter = ("relevance_label", "needs_extraction", "extraction_status", "channel_name", "latest_message_at")


@admin.register(LinearProjectSelection)
class LinearProjectSelectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "connection", "project_name", "linear_project_id", "selected", "last_synced_at")
    search_fields = ("organization__domain", "project_name", "linear_project_id", "connection__account_label")
    list_filter = ("selected", "project_status", "project_health", "last_synced_at", "updated_at")


@admin.register(LinearProjectArtifact)
class LinearProjectArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "name",
        "status_name",
        "health",
        "relevance_label",
        "relevance_score",
        "extraction_status",
        "target_date",
    )
    search_fields = ("organization__domain", "name", "linear_project_id", "description")
    list_filter = ("status_type", "health", "relevance_label", "needs_extraction", "extraction_status")


@admin.register(LinearIssueArtifact)
class LinearIssueArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "identifier", "title", "state_name", "priority_label", "updated_at_linear")
    search_fields = ("organization__domain", "identifier", "linear_issue_id", "title", "description")
    list_filter = ("state_type", "priority_label", "team_name", "updated_at_linear")


@admin.register(LinearProjectUpdateArtifact)
class LinearProjectUpdateArtifactAdmin(admin.ModelAdmin):
    list_display = ("organization", "project", "health", "author_name", "updated_at_linear")
    search_fields = ("organization__domain", "linear_project_update_id", "body", "author_name")
    list_filter = ("health", "updated_at_linear")


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


@admin.register(StartupDataDeletionRequest)
class StartupDataDeletionRequestAdmin(admin.ModelAdmin):
    list_display = ("organization", "provider", "status", "delete_derived_data", "google_account", "completed_at")
    search_fields = ("organization__domain", "request_id", "google_account", "reason")
    list_filter = ("provider", "status", "delete_derived_data", "created_at")
    readonly_fields = ("request_id", "deleted_counts", "warnings", "metadata", "started_at", "completed_at")
