from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User, GlobalSettings, Organization, OrganizationContentConfig
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
            'fields': ('email', 'password', 'password_2'),
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


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'domain', 'created_at')
    search_fields = ('name', 'domain')
    ordering = ('name',)


@admin.register(OrganizationContentConfig)
class OrganizationContentConfigAdmin(admin.ModelAdmin):
    list_display = ('organization', 'brand_name', 'github_repo', 'has_scan', 'updated_at')
    search_fields = ('organization__name', 'organization__domain', 'brand_name', 'github_repo')
    list_select_related = ('organization',)
    list_filter = ('updated_at',)
    readonly_fields = ('created_at', 'updated_at', 'tech_stack_display', 'article_template_preview', 'design_guide_preview')
    
    fieldsets = (
        ('Organization', {
            'fields': ('organization', 'brand_name')
        }),
        ('GitHub Integration', {
            'fields': ('github_repo', 'github_token_encrypted', 'article_path_pattern', 'registry_path')
        }),
        ('Scan Results', {
            'fields': ('scan_summary', 'tech_stack_display'),
            'description': 'Data returned from the content-factory scanner agent'
        }),
        ('Templates (LLM Generated)', {
            'fields': ('article_template_preview', 'design_guide_preview'),
            'classes': ('collapse',),  # Collapsible since these are large
        }),
        ('Raw Template Data', {
            'fields': ('article_template', 'design_guide'),
            'classes': ('collapse',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    
    def has_scan(self, obj):
        """Check if scan has been run (has scan_summary or tech_stack)."""
        if obj.scan_summary or obj.tech_stack:
            return format_html('<span style="color: green;">✅ Yes</span>')
        return format_html('<span style="color: gray;">❌ No</span>')
    has_scan.short_description = 'Scanned'
    
    def tech_stack_display(self, obj):
        """Pretty display of tech_stack JSON."""
        if not obj.tech_stack:
            return "No tech stack data"
        import json
        formatted = json.dumps(obj.tech_stack, indent=2)
        return format_html('<pre style="background: #f4f4f4; padding: 10px; border-radius: 4px; max-height: 200px; overflow: auto;">{}</pre>', formatted)
    tech_stack_display.short_description = 'Tech Stack'
    
    def article_template_preview(self, obj):
        """Preview of article template (truncated)."""
        if not obj.article_template:
            return "No template"
        preview = obj.article_template[:500] + "..." if len(obj.article_template) > 500 else obj.article_template
        return format_html('<pre style="background: #f9f9f9; padding: 10px; border-radius: 4px; max-height: 300px; overflow: auto; white-space: pre-wrap;">{}</pre>', preview)
    article_template_preview.short_description = 'Article Template Preview'
    
    def design_guide_preview(self, obj):
        """Preview of design guide (truncated)."""
        if not obj.design_guide:
            return "No design guide"
        preview = obj.design_guide[:500] + "..." if len(obj.design_guide) > 500 else obj.design_guide
        return format_html('<pre style="background: #f9f9f9; padding: 10px; border-radius: 4px; max-height: 300px; overflow: auto; white-space: pre-wrap;">{}</pre>', preview)
    design_guide_preview.short_description = 'Design Guide Preview'
