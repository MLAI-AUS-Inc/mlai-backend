from django.contrib import admin

from .models import Announcement, Team, VideoSubmission


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("team_id", "team_name")
    search_fields = ("team_name",)
    ordering = ("team_id",)
    filter_horizontal = ("members",)


@admin.register(VideoSubmission)
class VideoSubmissionAdmin(admin.ModelAdmin):
    list_display = ("title", "participant_name", "team", "content_type", "file_size_bytes", "submitted_at")
    list_filter = ("team", "submitted_at")
    search_fields = ("title", "participant_name", "user__email", "team__team_name")
    ordering = ("-submitted_at",)


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "created_at")
    list_filter = ("created_at",)
    search_fields = ("title", "body")
    ordering = ("-created_at",)
