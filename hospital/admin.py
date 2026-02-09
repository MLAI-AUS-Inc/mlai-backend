import json
from django.contrib import admin
from django.utils.html import format_html
from .models import Team, Submission, Announcement, MedHackCase, MedHackGuess, MedHackWinner

class TeamAdmin(admin.ModelAdmin):
    list_display = ('team_id', 'team_name', 'member_list', 'member_count')
    search_fields = ('team_id', 'team_name')
    ordering = ('team_id',)
    filter_horizontal = ('members',)  # Easy widget for editing members


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
    ordering = ('-created_at',)
    # Optionally, restrict the fields that can be edited in the change form.
    fields = ('user', 'team', 'participant_name', 'score', 'accuracy')


@admin.register(MedHackCase)
class MedHackCaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'case_id', 'is_active', 'solved', 'hint_level', 'started_by_slack_id', 'started_at', 'closed_at')
    list_filter = ('is_active', 'solved')
    search_fields = ('case_id', 'started_by_slack_id')
    readonly_fields = ('started_at',)
    ordering = ('-started_at',)


@admin.register(MedHackGuess)
class MedHackGuessAdmin(admin.ModelAdmin):
    list_display = ('id', 'case', 'slack_user_id', 'guess_short', 'correct', 'is_pending', 'created_at')
    list_filter = ('correct', 'is_pending')
    search_fields = ('slack_user_id', 'guess')
    readonly_fields = ('created_at', 'confirmed_at')
    ordering = ('-created_at',)

    def guess_short(self, obj):
        if len(obj.guess) > 50:
            return obj.guess[:50] + '...'
        return obj.guess
    guess_short.short_description = 'Guess'


@admin.register(MedHackWinner)
class MedHackWinnerAdmin(admin.ModelAdmin):
    list_display = ('id', 'case', 'slack_user_id', 'is_first_solver', 'won_at')
    list_filter = ('is_first_solver',)
    search_fields = ('slack_user_id',)
    readonly_fields = ('won_at',)
    ordering = ('-won_at',)
