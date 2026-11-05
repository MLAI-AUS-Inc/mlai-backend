from datetime import timedelta

from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from .models import (  # noqa: F401  (invite_link is re-exported for callers of this module)
    INVITE_TTL_DAYS, MAX_SESSION_MINUTES, MIN_SESSION_MINUTES, MentorAvailabilityRule, MentorInvite,
    MentorProfile, MentorSession, invite_link,
)
from .service import MAX_PRICE_ROO  # the bound the mentor API enforces



class AvailabilityInline(admin.TabularInline):
    model = MentorAvailabilityRule
    extra = 0


class MentorProfileAdminForm(forms.ModelForm):
    """Applies the API's session length and price rules to staff edits.

    Checked in the form (not on the model fields) so no migration is needed.
    """

    class Meta:
        model = MentorProfile
        fields = '__all__'

    def clean_session_minutes(self):
        minutes = self.cleaned_data['session_minutes']
        if not MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES or minutes % 5:
            raise forms.ValidationError(
                f'Session length must be a multiple of 5 from {MIN_SESSION_MINUTES} to {MAX_SESSION_MINUTES} minutes.'
            )
        return minutes

    def clean_price_roo(self):
        price = self.cleaned_data['price_roo']
        if not 1 <= price <= MAX_PRICE_ROO:
            raise forms.ValidationError(f'Price must be a whole number from 1 to {MAX_PRICE_ROO} Roo Points.')
        return price


@admin.register(MentorProfile)
class MentorProfileAdmin(admin.ModelAdmin):
    """Only staff here can make someone a mentor (is_active)."""

    form = MentorProfileAdminForm

    list_display = ('user', 'is_active', 'session_minutes', 'price_roo', 'location_type')
    list_editable = ('is_active', 'price_roo')
    list_filter = ('is_active', 'location_type')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    raw_id_fields = ('user',)
    inlines = [AvailabilityInline]

    def get_changelist_form(self, request, **kwargs):
        # The inline-editable price on the list page must follow the same rules.
        kwargs.setdefault('form', MentorProfileAdminForm)
        return super().get_changelist_form(request, **kwargs)


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
        count = queryset.filter(used_at__isnull=True, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_by=request.user,
        )
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
