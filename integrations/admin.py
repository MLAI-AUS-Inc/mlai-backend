from django.contrib import admin
from .models import UserIntegration, GoogleConnection

@admin.register(UserIntegration)
class UserIntegrationAdmin(admin.ModelAdmin):
    list_display = ('slack_user_id', 'github_user_name', 'github_repo', 'project_scanned', 'last_scanned_at', 'updated_at')
    search_fields = ('slack_user_id', 'github_user_name', 'github_repo')
    list_filter = ('project_scanned', 'updated_at')
    readonly_fields = ('updated_at', 'last_scanned_at', 'last_scanned_sha')

@admin.register(GoogleConnection)
class GoogleConnectionAdmin(admin.ModelAdmin):
    list_display = ('user', 'google_email', 'updated_at')
    search_fields = ('user__email', 'google_email')
