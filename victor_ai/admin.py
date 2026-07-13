from django.contrib import admin

from .models import VictorApplication


@admin.register(VictorApplication)
class VictorApplicationAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'email', 'stage', 'role', 'startup_stage', 'team_name', 'created_at')
    list_filter = ('stage', 'role', 'startup_stage', 'created_at')
    search_fields = ('first_name', 'last_name', 'email', 'team_name', 'location')
    readonly_fields = ('client_ref', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
