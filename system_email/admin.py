from django.contrib import admin
from django.utils import timezone

from .models import SystemEmail, SystemEmailSuppression


@admin.register(SystemEmail)
class SystemEmailAdmin(admin.ModelAdmin):
    list_display = ("created_at", "message_key", "status", "provider", "attempts", "error_code")
    list_filter = ("status", "message_key", "category", "provider")
    search_fields = ("to_email", "provider_message_id", "idempotency_key")
    readonly_fields = [field.name for field in SystemEmail._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SystemEmailSuppression)
class SystemEmailSuppressionAdmin(admin.ModelAdmin):
    list_display = ("email", "reason", "created_at", "cleared_at", "cleared_by")
    list_filter = ("reason",)
    search_fields = ("email",)
    readonly_fields = ("email", "reason", "source_event_id", "created_at", "cleared_at", "cleared_by")
    actions = ("clear_suppression",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Clear suppression (allow email to this address again)")
    def clear_suppression(self, request, queryset):
        cleared = queryset.filter(cleared_at__isnull=True).update(
            cleared_at=timezone.now(), cleared_by=str(request.user.get_username())[:255]
        )
        self.message_user(
            request,
            f"Cleared {cleared} suppression(s). If Resend also suppressed the address, remove it there too.",
        )
