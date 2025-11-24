import json
from django.contrib import admin
from django.utils.html import format_html
from .models import Team, Submission

class TeamAdmin(admin.ModelAdmin):
    list_display = ('team_id', 'team_name', 'member_list', 'member_count')
    search_fields = ('team_id', 'team_name')
    ordering = ('team_id',)
    filter_horizontal = ('members',)  # Easy widget for editing members

    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = 'Number of Members'

    def member_list(self, obj):
        return ", ".join([f"{member.full_name} (ID: {member.id})" for member in obj.members.all()])
    member_list.short_description = "Members"

admin.site.register(Team, TeamAdmin)

class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'participant_name', 'team', 'score', 'accuracy', 'submitted_at')
    list_editable = ('participant_name', 'team',)
    list_filter = ('team', 'submitted_at')
    search_fields = ('participant_name', 'team__team_name')
    ordering = ('-submitted_at',)
    # Optionally, restrict the fields that can be edited in the change form.
    fields = ('user', 'team', 'participant_name', 'score', 'accuracy')

admin.site.register(Submission, SubmissionAdmin)