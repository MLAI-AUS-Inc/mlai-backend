from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from community_chat.admin_authority import MemberAudienceAuthorityAdminMixin
from .models import (
    CommunityBridgeChannel,
    CommunityBridgeMirrorDecision,
    CommunityBridgePlatform,
    CommunityBridgeDeletionRequest,
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
class SlackDmMirrorGrantAdmin(MemberAudienceAuthorityAdminMixin, admin.ModelAdmin):
    list_display = ("user", "slack_workspace_id", "slack_user_id", "status", "last_synced_at")
    search_fields = ("user__email", "slack_workspace_id", "slack_user_id")
    list_filter = ("status", "consent_version")
    readonly_fields = (
        "user", "connection", "slack_workspace_id", "slack_user_id", "status",
        "consent_version", "history_days", "consented_at", "paused_at", "revoked_at",
        "created_at", "updated_at",
    )

    actions = ("pause_slack_mirroring",)

    @admin.action(description="Pause selected Slack mirrors")
    def pause_slack_mirroring(self, request, queryset):
        from integrations.services.slack_dm_mirror import pause_grant

        for grant in queryset.order_by("user_id", "id"):
            pause_grant(grant)

    def has_add_permission(self, request):
        """Slack consent is granted by its owner through the linking flow."""
        return False


@admin.register(SlackDmMirrorConversation)
class SlackDmMirrorConversationAdmin(admin.ModelAdmin):
    list_display = ("slack_conversation_id", "grant", "slack_workspace_id", "mlai_channel_id", "status")
    search_fields = (
        "slack_conversation_id",
        "slack_workspace_id",
        "mlai_channel_id",
        "grant__user__email",
    )
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
        "audience",
        "mirror_decision",
        "hold_reason",
        "destination_platform",
        "destination_channel_id",
        "destination_channel_name",
        "enabled",
        "room_sync_error",
        "updated_at",
    )
    actions = ("mirror_channels", "hold_channels", "exclude_channels")
    search_fields = (
        "slack_workspace_id",
        "slack_channel_id",
        "slack_channel_name",
        "destination_channel_id",
        "destination_channel_name",
        "destination_workspace_id",
    )
    list_filter = (
        "audience",
        "mirror_decision",
        "lifecycle_source",
        "slack_visibility",
        "destination_platform",
        "enabled",
        "room_sync_pending",
        "sync_edits",
        "sync_deletes",
        "sync_replies",
        "updated_at",
    )

    @admin.action(description="Mirror selected public channels into MLAI Chat")
    def mirror_channels(self, request, queryset):
        from integrations.services.community_bridge.member_channels import queue_refresh, valid_slack_channel_identity

        # This records intent only. Member-authorized processing is introduced
        # separately; an admin action never creates or widens a room audience.
        public = queryset.filter(
            destination_platform=CommunityBridgePlatform.BUZZ,
            slack_visibility="public", slack_deleted=False,
        )
        public = public.filter(pk__in=[
            channel.pk for channel in public
            if valid_slack_channel_identity(channel.slack_workspace_id, channel.slack_channel_id)
        ])
        updated = public.update(mirror_decision=CommunityBridgeMirrorDecision.MIRROR, hold_reason="", room_sync_pending=True)
        for channel in public:
            queue_refresh(channel.slack_workspace_id, channel.slack_channel_id, reason="admin_mirror")
        self.message_user(request, f"{updated} channel(s) queued for authorized channel processing.")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "audience") if obj and obj.destination_channel_id else fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.destination_platform == CommunityBridgePlatform.BUZZ:
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        if obj.destination_platform == CommunityBridgePlatform.BUZZ:
            raise ValidationError("Retain MLAI Chat source identities; hold or exclude this channel instead.")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if queryset.filter(destination_platform=CommunityBridgePlatform.BUZZ).exists():
            raise ValidationError("Retain MLAI Chat source identities; hold or exclude these channels instead.")
        super().delete_queryset(request, queryset)

    def save_model(self, request, obj, form, change):
        from integrations.services.community_bridge.member_channels import _members_mapping, _schedule_audience_reductions

        with transaction.atomic():
            if not isinstance(obj.room_state, dict):
                raise ValidationError("Bridge operational state must be a JSON object.")
            if change:
                previous = CommunityBridgeChannel.objects.select_for_update().get(pk=obj.pk)
                if previous.destination_channel_id and any(getattr(previous, field) != getattr(obj, field) for field in (
                    "audience", "slack_workspace_id", "slack_channel_id", "destination_platform", "destination_channel_id",
                )):
                    raise ValidationError("A mapped bridge room's source, destination and audience are immutable.")
            if _members_mapping(obj):
                obj.audience_sync_pending = True
                obj.enabled = False
            super().save_model(request, obj, form, change)
            if _members_mapping(obj):
                _schedule_audience_reductions([obj.pk])

    def _stop_channels(self, queryset, *, decision):
        from integrations.services.community_bridge.member_channels import _members_mapping, _schedule_audience_reductions

        with transaction.atomic():
            rows = list(queryset.select_for_update().order_by("pk"))
            targets = []
            for row in rows:
                row.mirror_decision = decision
                row.hold_reason = "admin"
                row.enabled = False
                if _members_mapping(row):
                    row.audience_sync_pending = True
                    targets.append(row.pk)
                row.save(update_fields=("mirror_decision", "hold_reason", "enabled", "audience_sync_pending", "updated_at"))
            _schedule_audience_reductions(targets)
            return len(rows)

    @admin.action(description="Hold selected channels for review (stop mirroring)")
    def hold_channels(self, request, queryset):
        updated = self._stop_channels(queryset, decision=CommunityBridgeMirrorDecision.HOLD)
        self.message_user(request, f"{updated} channel(s) held.")

    @admin.action(description="Never mirror selected channels")
    def exclude_channels(self, request, queryset):
        updated = self._stop_channels(queryset, decision=CommunityBridgeMirrorDecision.EXCLUDE)
        self.message_user(request, f"{updated} channel(s) excluded.")


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

@admin.register(CommunityBridgeDeletionRequest)
class CommunityBridgeDeletionRequestAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "slack_workspace_id",
        "slack_channel_id",
        "slack_message_id",
        "status",
        "created_at",
    )
    search_fields = (
        "user__email",
        "slack_workspace_id",
        "slack_channel_id",
        "slack_message_id",
        "buzz_event_id",
    )
    list_filter = ("status", "created_at")
    readonly_fields = tuple(
        field.name for field in CommunityBridgeDeletionRequest._meta.fields
    )

@admin.register(GoogleConnection)
class GoogleConnectionAdmin(admin.ModelAdmin):
    list_display = ('user', 'google_email', 'updated_at')
    search_fields = ('user__email', 'google_email')


@admin.register(ExternalServiceConnection)
class ExternalServiceConnectionAdmin(MemberAudienceAuthorityAdminMixin, admin.ModelAdmin):
    list_display = ("provider", "user", "organization", "account_label", "status", "last_synced_at", "updated_at")
    search_fields = ("user__email", "organization__domain", "external_account_id", "account_label")
    list_filter = ("provider", "status", "updated_at")
    readonly_fields = ("created_at", "updated_at", "last_synced_at")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None and (obj.provider == "slack" or SlackDmMirrorGrant.objects.filter(connection_id=obj.pk).exists()):
            return (*fields, "user", "provider", "external_account_id", "scopes", "provider_metadata",
                    "sync_cursor", "access_token", "refresh_token")
        return fields


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
