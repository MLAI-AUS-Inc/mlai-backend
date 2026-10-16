from django.contrib import admin

from .models import MentorAvailabilityRule, MentorProfile, MentorSession


class AvailabilityInline(admin.TabularInline):
    model = MentorAvailabilityRule
    extra = 0


@admin.register(MentorProfile)
class MentorProfileAdmin(admin.ModelAdmin):
    """Only staff here can make someone a mentor (is_active)."""

    list_display = ('user', 'is_active', 'session_minutes', 'price_roo', 'location_type')
    list_editable = ('is_active', 'price_roo')
    list_filter = ('is_active', 'location_type')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    raw_id_fields = ('user',)
    inlines = [AvailabilityInline]


@admin.register(MentorSession)
class MentorSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'mentor', 'founder', 'status', 'starts_at', 'price_roo', 'notification_status', 'meeting_status')
    list_filter = ('status', 'notification_status', 'meeting_status')
    readonly_fields = [f.name for f in MentorSession._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
