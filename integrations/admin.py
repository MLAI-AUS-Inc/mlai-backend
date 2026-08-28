from django.contrib import admin
from .models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeIdentityLink,
    CommunityBridgeMessageLink,
    CommunityBridgeReceipt,
    ExternalFinancialRecord,
    ExternalServiceConnection,
    FinancialAccount,
    GoogleConnection,
    HumanitixEvent,
    HumanitixEventFinancialSummary,
    HumanitixPayout,
    HumanitixPayoutLine,
    SlackDmMirrorConversation,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    UserIntegration,
)


@admin.register(SlackDmMirrorGrant)
class SlackDmMirrorGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "slack_workspace_id", "slack_user_id", "status", "last_synced_at")
    search_fields = ("user__email", "slack_workspace_id", "slack_user_id")
    list_filter = ("status", "consent_version")
    readonly_fields = ("consented_at", "created_at", "updated_at")


@admin.register(SlackDmMirrorConversation)
class SlackDmMirrorConversationAdmin(admin.ModelAdmin):
    list_display = ("slack_conversation_id", "slack_workspace_id", "mlai_channel_id", "status")
    search_fields = ("slack_conversation_id", "slack_workspace_id", "mlai_channel_id")
    list_filter = ("status",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(SlackDmMirrorDelivery)
class SlackDmMirrorDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "source_platform", "operation", "status", "created_at")
    search_fields = ("source_message_id", "source_author_id")
    list_filter = ("source_platform", "operation", "status")
    exclude = ("encrypted_text",)
    readonly_fields = ("created_at", "updated_at", "completed_at")

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
        "destination_platform",
        "destination_channel_id",
        "destination_channel_name",
        "enabled",
        "updated_at",
    )
    search_fields = (
        "slack_workspace_id",
        "slack_channel_id",
        "slack_channel_name",
        "destination_channel_id",
        "destination_channel_name",
        "destination_workspace_id",
    )
    list_filter = (
        "destination_platform",
        "enabled",
        "sync_edits",
        "sync_deletes",
        "sync_replies",
        "updated_at",
    )


@admin.register(CommunityBridgeIdentityLink)
class CommunityBridgeIdentityLinkAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "slack_workspace_id",
        "slack_user_id",
        "display_name",
        "buzz_pubkey",
        "verification_method",
        "verified_at",
        "revoked_at",
    )
    search_fields = (
        "user__email",
        "user__community_chat_profile_id",
        "slack_workspace_id",
        "slack_user_id",
        "display_name",
        "buzz_pubkey",
        "verification_reference",
    )
    list_filter = ("verification_method", "verified_at", "revoked_at")
    readonly_fields = ("created_at", "updated_at", "verified_at")


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


@admin.register(ExternalServiceConnection)
class ExternalServiceConnectionAdmin(admin.ModelAdmin):
    list_display = ("provider", "user", "organization", "account_label", "status", "last_synced_at", "updated_at")
    search_fields = ("user__email", "organization__domain", "external_account_id", "account_label")
    list_filter = ("provider", "status", "updated_at")
    readonly_fields = ("created_at", "updated_at", "last_synced_at")


@admin.register(FinancialAccount)
class FinancialAccountAdmin(admin.ModelAdmin):
    list_display = ("provider", "account_label", "organization", "currency", "balance", "available_funds", "last_synced_at")
    search_fields = ("external_account_id", "account_label", "institution_name", "organization__domain", "user__email")
    list_filter = ("provider", "currency", "status", "updated_at")
    readonly_fields = ("created_at", "updated_at", "last_synced_at")


@admin.register(ExternalFinancialRecord)
class ExternalFinancialRecordAdmin(admin.ModelAdmin):
    list_display = ("provider", "record_type", "organization", "transaction_date", "amount", "currency", "status")
    search_fields = ("external_record_id", "external_account_id", "description", "merchant_name", "organization__domain")
    list_filter = ("provider", "record_type", "status", "transaction_date")
    readonly_fields = ("created_at", "updated_at")


@admin.register(HumanitixEvent)
class HumanitixEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_name",
        "organization",
        "start_at",
        "currency",
        "published",
        "archived",
        "last_synced_at",
    )
    search_fields = ("event_name", "external_event_id", "organization__domain")
    list_filter = ("currency", "published", "archived", "start_at")
    readonly_fields = ("source_hash", "source_payload", "created_at", "updated_at", "last_synced_at")


@admin.register(HumanitixEventFinancialSummary)
class HumanitixEventFinancialSummaryAdmin(admin.ModelAdmin):
    list_display = (
        "event",
        "order_count",
        "ticket_count",
        "gross_sales",
        "net_sales",
        "refunds",
        "last_synced_at",
    )
    search_fields = ("event__event_name", "event__external_event_id")
    readonly_fields = (
        "gateway_breakdown",
        "ticket_type_breakdown",
        "source_hash",
        "created_at",
        "updated_at",
        "last_synced_at",
    )


@admin.register(HumanitixPayout)
class HumanitixPayoutAdmin(admin.ModelAdmin):
    list_display = (
        "payout_reference",
        "organization",
        "payout_date",
        "payout_amount",
        "currency",
        "status",
    )
    search_fields = ("payout_reference", "organization__domain")
    list_filter = ("status", "currency", "payout_date")
    readonly_fields = (
        "source_hash",
        "source_payload",
        "preview_payload",
        "warnings",
        "approved_at",
        "created_at",
        "updated_at",
        "posted_at",
    )


@admin.register(HumanitixPayoutLine)
class HumanitixPayoutLineAdmin(admin.ModelAdmin):
    list_display = ("payout", "event_name", "component", "amount", "tax_amount")
    search_fields = ("payout__payout_reference", "event_name", "external_event_id")
    list_filter = ("component",)
    readonly_fields = ("metadata", "created_at", "updated_at")
