from django.contrib import admin
from .models import Team, Submission, Announcement

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('team_name', 'team_id')
    search_fields = ('team_name',)
    ordering = ('team_id',)

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'team', 'score', 'submitted_at')
    list_filter = ('team', 'submitted_at')
    search_fields = ('user__email', 'team__team_name')

@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'author', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('title', 'body')
