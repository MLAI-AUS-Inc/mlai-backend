from django.contrib import admin

from .models import StudioApplication


@admin.register(StudioApplication)
class StudioApplicationAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'stage', 'location', 'availability', 'start_date', 'created_at')
    list_filter = ('stage', 'legal_work', 'availability', 'created_at')
    search_fields = ('full_name', 'email', 'location', 'github', 'linkedin', 'portfolio')
    readonly_fields = ('client_ref', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
