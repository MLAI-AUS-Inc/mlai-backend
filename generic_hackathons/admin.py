from django.contrib import admin

from .models import (
    GenericHackathonAnnouncement,
    GenericHackathonResource,
    GenericHackathonSubmission,
    GenericHackathonTeam,
)


@admin.register(GenericHackathonTeam)
class GenericHackathonTeamAdmin(admin.ModelAdmin):
    list_display = ('team_name', 'team_id', 'hackathon', 'created_at')
    list_filter = ('hackathon',)
    search_fields = ('team_name',)
    filter_horizontal = ('members',)


@admin.register(GenericHackathonSubmission)
class GenericHackathonSubmissionAdmin(admin.ModelAdmin):
    list_display = ('title', 'team', 'hackathon', 'user', 'created_at')
    list_filter = ('hackathon', 'created_at')
    search_fields = ('title', 'summary', 'team__team_name', 'user__email')


@admin.register(GenericHackathonAnnouncement)
class GenericHackathonAnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'hackathon', 'author', 'created_at')
    list_filter = ('hackathon', 'created_at')
    search_fields = ('title', 'body')


@admin.register(GenericHackathonResource)
class GenericHackathonResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'hackathon', 'category', 'order')
    list_filter = ('hackathon', 'category')
    search_fields = ('title', 'summary', 'body')
