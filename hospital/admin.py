import json
from django.contrib import admin
from django.utils.html import format_html
from .models import (
    HospitalCompetitionRound, Team, Submission, Announcement,
    SimDiagnosisGuess, SimCaseWinner, SimParticipant, SimConversation,
    SimConversationTurn,
)

@admin.register(HospitalCompetitionRound)
class HospitalCompetitionRoundAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'slug', 'status', 'team_count', 'submission_count',
        'announcement_count', 'opened_at', 'archived_at', 'archived_by',
    )
    list_filter = ('status',)
    search_fields = ('name', 'slug', 'notes')
    ordering = ('-opened_at',)
    readonly_fields = (
        'slug', 'name', 'status', 'opened_at', 'archived_at', 'archived_by',
        'notes',
    )

    @admin.display(description='Teams')
    def team_count(self, obj):
        return obj.teams.count()

    @admin.display(description='Submissions')
    def submission_count(self, obj):
        return obj.submissions.count()

    @admin.display(description='Announcements')
    def announcement_count(self, obj):
        return obj.announcements.count()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('team_id', 'team_name', 'round', 'member_list', 'member_count')
    list_filter = ('round__status', 'round')
    search_fields = ('team_id', 'team_name')
    ordering = ('-round__opened_at', 'team_id')
    readonly_fields = ('round', 'team_id')
    filter_horizontal = ('members',)  # Easy widget for editing members

    @admin.display(description='Members')
    def member_list(self, obj):
        return ', '.join(
            member.full_name or member.email for member in obj.members.all()
        )

    @admin.display(description='Member count')
    def member_count(self, obj):
        return obj.members.count()


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'team', 'round', 'score', 'submitted_at')
    list_filter = ('round__status', 'round', 'team', 'submitted_at')
    search_fields = ('user__email', 'team__team_name')
    readonly_fields = ('round',)

@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'round', 'author', 'requester', 'source_channel_id', 'created_at')
    list_filter = ('round__status', 'round', 'created_at')
    search_fields = ('title', 'body', 'source_channel_id', 'source_message_ts')
    ordering = ('-created_at',)
    fields = (
        'round',
        'title',
        'body',
        'author',
        'requester',
        'source_channel_id',
        'source_message_ts',
    )
    readonly_fields = ('round',)


@admin.register(SimDiagnosisGuess)
class SimDiagnosisGuessAdmin(admin.ModelAdmin):
    """Organizer export surface for the web ward contest (emails + outcomes)."""
    list_display = ('id', 'case_id', 'case_title', 'client_id', 'guess_text', 'is_correct', 'prize_kind', 'outcome', 'email', 'created_at', 'claimed_at')
    list_filter = ('case_id', 'prize_kind', 'outcome', 'is_correct')
    search_fields = ('email', 'case_title', 'client_id', 'guess_text')
    readonly_fields = ('created_at', 'claimed_at')
    ordering = ('-created_at',)


@admin.register(SimCaseWinner)
class SimCaseWinnerAdmin(admin.ModelAdmin):
    list_display = ('id', 'case_id', 'guess', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)


@admin.register(SimParticipant)
class SimParticipantAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'last_seen_at')
    search_fields = ('id',)
    readonly_fields = ('created_at', 'last_seen_at')
    ordering = ('-last_seen_at',)


class SimConversationTurnInline(admin.TabularInline):
    model = SimConversationTurn
    extra = 0
    can_delete = False
    fields = (
        'created_at', 'player_text', 'npc_text', 'response_source',
        'model_name', 'tool_calls', 'suggested_action', 'latency_ms', 'error_code',
    )
    readonly_fields = fields


@admin.register(SimConversation)
class SimConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'participant', 'case_id', 'role', 'created_at', 'last_turn_at')
    list_filter = ('case_id', 'role')
    search_fields = ('id', 'participant__id', 'turns__player_text', 'turns__npc_text')
    readonly_fields = ('created_at', 'last_turn_at')
    ordering = ('-last_turn_at',)
    inlines = [SimConversationTurnInline]


@admin.register(SimConversationTurn)
class SimConversationTurnAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'response_source', 'model_name', 'latency_ms', 'created_at')
    list_filter = ('response_source', 'conversation__role', 'conversation__case_id')
    search_fields = ('player_text', 'npc_text', 'conversation__participant__id')
    readonly_fields = ('created_at', 'completed_at')
    ordering = ('-created_at',)
