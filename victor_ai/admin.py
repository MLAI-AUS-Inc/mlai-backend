from django.contrib import admin

from .models import VictorApplication, VictorApplicationAccessAudit, VictorRooRequestReceipt


@admin.register(VictorApplication)
class VictorApplicationAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'email', 'stage', 'role', 'startup_stage', 'industry_sector', 'team_name', 'team_size', 'created_at')
    list_filter = ('stage', 'role', 'startup_stage', 'industry_sector', 'created_at')
    search_fields = ('first_name', 'last_name', 'email', 'linkedin', 'team_name', 'location')
    readonly_fields = ('client_ref', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VictorApplicationAccessAudit)
class VictorApplicationAccessAuditAdmin(ReadOnlyAuditAdmin):
    list_display = (
        'created_at', 'action', 'acting_slack_user_id', 'slack_channel_id',
        'target_application_id', 'row_count', 'outcome', 'request_id',
    )
    list_filter = ('action', 'outcome', 'created_at')
    search_fields = ('acting_slack_user_id', 'slack_channel_id', 'request_id')
    ordering = ('-created_at',)


@admin.register(VictorRooRequestReceipt)
class VictorRooRequestReceiptAdmin(ReadOnlyAuditAdmin):
    list_display = ('created_at', 'request_id', 'event_id', 'expires_at')
    search_fields = ('request_id', 'event_id')
    ordering = ('-created_at',)
