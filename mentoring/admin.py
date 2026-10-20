from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.utils import timezone

from .models import INVITE_TTL_DAYS, MentorAvailabilityRule, MentorInvite, MentorProfile, MentorSession


def invite_link(token):
    base = str(getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', '')).rstrip('/')
    return f'{base}/#/mentoring/apply?invite={token}'


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


@admin.register(MentorInvite)
class MentorInviteAdmin(admin.ModelAdmin):
    """Staff invite one existing user to apply as a mentor.

    Adding an invite shows the single-use link once; only its hash is stored.
    The applicant then appears under Mentor profiles as inactive; tick
    is_active there to approve.
    """

    list_display = ('user', 'created_at', 'expires_at', 'status', 'created_by')
    raw_id_fields = ('user',)
    readonly_fields = ('created_at', 'used_at', 'revoked_at', 'created_by')
    fields = ('user', 'expires_at', 'created_at', 'used_at', 'revoked_at', 'created_by')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    actions = ('revoke_invites',)

    @admin.display(description='Status')
    def status(self, obj):
        if obj.revoked_at:
            return 'revoked'
        if obj.used_at:
            return 'used'
        return 'expired' if obj.expires_at <= timezone.now() else 'open'

    def get_changeform_initial_data(self, request):
        return {'expires_at': timezone.now() + timedelta(days=INVITE_TTL_DAYS)}

    def save_model(self, request, obj, form, change):
        if change:
            return  # invites are never edited, only revoked
        if MentorProfile.objects.filter(user=obj.user, is_active=True).exists():
            self.message_user(request, 'That user is already an active mentor; no invite created.', messages.ERROR)
            return
        ttl_days = max((obj.expires_at - timezone.now()).total_seconds() / 86400, 0.01)
        invite, token = MentorInvite.issue(obj.user, created_by=request.user, ttl_days=ttl_days)
        obj.pk = invite.pk
        obj.token_hash = invite.token_hash
        obj.created_by = invite.created_by
        self.message_user(
            request,
            f'Invite link (shown once, copy it now): {invite_link(token)}',
            messages.WARNING,
        )

    @admin.action(description='Revoke selected unused invites')
    def revoke_invites(self, request, queryset):
        count = queryset.filter(used_at__isnull=True, revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f'{count} invite(s) revoked.')


@admin.register(MentorSession)
class MentorSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'mentor', 'founder', 'status', 'starts_at', 'price_roo', 'notification_status', 'meeting_status')
    list_filter = ('status', 'notification_status', 'meeting_status')
    readonly_fields = [f.name for f in MentorSession._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
