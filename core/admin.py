from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User, GlobalSettings
from .forms import CustomUserCreationForm, CustomUserChangeForm


class UserAdmin(BaseUserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = User
    list_display = ('email', 'first_name', 'last_name', 'role', 'slack_id', 'is_staff', 'avatar_preview')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups')
    search_fields = ('email', 'first_name', 'last_name', 'slack_id')
    ordering = ('email',)
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'avatar_url', 'avatar_preview')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
        ('Other', {'fields': ('role', 'has_team', 'slack_id')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2'),
        }),
    )
    
    readonly_fields = ('avatar_preview',)

    def avatar_preview(self, obj):
        if obj.avatar_url:
            return format_html('<img src="{}" style="width: 50px; height: 50px; border-radius: 50%; object-fit: cover;" />', obj.avatar_url)
        return "No Avatar"
    
    avatar_preview.short_description = 'Avatar'

@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_obscured')
    # Prevent creating more than one instance if possible, though singleton logic is in model save()
    def has_add_permission(self, request):
        if GlobalSettings.objects.exists():
            return False
        return super().has_add_permission(request)

admin.site.register(User, UserAdmin)
