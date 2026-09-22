from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import (
    AccountBan,
    GlobalSettings,
    PasswordResetChallenge,
    PasswordResetEmailDelivery,
    User,
)
from .forms import CustomUserCreationForm, CustomUserChangeForm


class UserAdmin(BaseUserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    actions = ('ban_mlai_accounts',)

    @admin.action(description='Ban selected MLAI accounts (retain email)')
    def ban_mlai_accounts(self, request, queryset):
        from community_chat.account_bans import ban_account
        from rest_framework.exceptions import APIException
        for user in queryset:
            try:
                ban = ban_account(actor=request.user, user_id=user.pk, reason='Administrator action')
                self.message_user(request, f'Account {user.pk} banned' + ('; chat revocation pending' if ban.revocation_pending else ''))
            except APIException as exc:
                self.message_user(request, str(exc), level='error')

    model = User
    list_display = ('email', 'first_name', 'last_name', 'slack_id', 'is_staff', 'email_verified_at', 'date_joined', 'updated', 'avatar_preview')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups')
    search_fields = ('email', 'first_name', 'last_name', 'slack_id')
    ordering = ('email',)
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'avatar_url', 'avatar_preview')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined', 'updated_at', 'email_verified_at', 'password_set_at')}),
        ('Authentication', {'fields': ('auth_version',)}),
        ('Other', {'fields': ('slack_id',)}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2'),
        }),
    )
    
    # Slack identity changes must pass through the transactional linking
    # services so pending capabilities are invalidated and explicit account
    # boundaries cannot be bypassed from Django Admin.
    readonly_fields = (
        'avatar_preview',
        'updated_at',
        'password_set_at',
        'community_chat_profile_id',
        'slack_id',
    )

    def avatar_preview(self, obj):
        if obj.avatar_url:
            return format_html('<img src="{}" style="width: 50px; height: 50px; border-radius: 50%; object-fit: cover;" />', obj.avatar_url)
        return "No Avatar"

    avatar_preview.short_description = 'Avatar'

    def updated(self, obj):
        return obj.updated_at

    updated.short_description = 'updated'
    updated.admin_order_field = 'updated_at'

@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_obscured')
    # Prevent creating more than one instance if possible, though singleton logic is in model save()
    def has_add_permission(self, request):
        if GlobalSettings.objects.exists():
            return False
        return super().has_add_permission(request)

admin.site.register(User, UserAdmin)


@admin.register(PasswordResetChallenge)
class PasswordResetChallengeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'expires_at', 'consumed_at', 'created_at')
    readonly_fields = tuple(field.name for field in PasswordResetChallenge._meta.fields)


@admin.register(PasswordResetEmailDelivery)
class PasswordResetEmailDeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'challenge_id', 'status', 'attempts', 'available_at', 'sent_at')
    list_filter = ('status',)
    search_fields = ('challenge__user__email', 'challenge_id')
    readonly_fields = tuple(field.name for field in PasswordResetEmailDelivery._meta.fields)


@admin.register(AccountBan)
class AccountBanAdmin(admin.ModelAdmin):
    """Retained email records; changes go through the audited ban service."""
    list_display = ('email', 'banned_by', 'created_at', 'revoked_at', 'revocation_pending')
    search_fields = ('email', 'user__first_name', 'user__last_name')
    list_filter = ('revocation_pending', 'revoked_at')
    readonly_fields = tuple(field.name for field in AccountBan._meta.fields)
    actions = ('lift_bans',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Lift selected MLAI account bans')
    def lift_bans(self, request, queryset):
        from community_chat.account_bans import lift_account_ban
        from rest_framework.exceptions import APIException
        for ban in queryset:
            try:
                lift_account_ban(actor=request.user, ban_id=ban.pk)
                self.message_user(request, f'Ban {ban.pk} lifted; fresh sign-in required')
            except APIException as exc:
                self.message_user(request, str(exc), level='error')
