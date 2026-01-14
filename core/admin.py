from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User, GlobalSettings, Organization, OrganizationContentConfig, GeneratedComponent, ComponentMapping
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
    list_display = ('name', 'domain', 'competitors', 'created_at')
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
            'fields': ('article_template', 'design_guide', 'company_context'),
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


@admin.register(GeneratedComponent)
class GeneratedComponentAdmin(admin.ModelAdmin):
    """Admin for viewing/managing generated components."""
    list_display = ('name', 'organization_domain', 'source', 'similarity_score_display', 'matched_component', 'updated_at')
    list_filter = ('source', 'organization', 'updated_at')
    search_fields = ('name', 'organization__domain', 'organization__name', 'matched_component')
    list_select_related = ('organization',)
    ordering = ('organization', 'name')
    readonly_fields = ('created_at', 'updated_at', 'content_preview')
    
    fieldsets = (
        ('Component Identity', {
            'fields': ('organization', 'name', 'source')
        }),
        ('Matching Info', {
            'fields': ('matched_component', 'original_path', 'similarity_score', 'adaptation_notes')
        }),
        ('Content Preview', {
            'fields': ('content_preview',),
        }),
        ('Full Content', {
            'fields': ('content',),
            'classes': ('collapse',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    
    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'
    organization_domain.admin_order_field = 'organization__domain'
    
    def similarity_score_display(self, obj):
        """Display similarity score as percentage with color coding."""
        score = obj.similarity_score or 0
        percentage = int(score * 100)
        if score >= 0.8:
            color = 'green'
        elif score >= 0.5:
            color = 'orange'
        else:
            color = 'gray'
        return format_html('<span style="color: {}; font-weight: bold;">{}%</span>', color, percentage)
    similarity_score_display.short_description = 'Match %'
    similarity_score_display.admin_order_field = 'similarity_score'
    
    def content_preview(self, obj):
        """Preview of component code (first 1000 chars)."""
        if not obj.content:
            return "No content"
        preview = obj.content[:1000] + "..." if len(obj.content) > 1000 else obj.content
        return format_html('<pre style="background: #1e1e1e; color: #d4d4d4; padding: 10px; border-radius: 4px; max-height: 400px; overflow: auto; white-space: pre-wrap; font-family: monospace; font-size: 12px;">{}</pre>', preview)
    content_preview.short_description = 'Component Preview (TSX)'


@admin.register(ComponentMapping)
class ComponentMappingAdmin(admin.ModelAdmin):
    """Admin for viewing component mapping summaries."""
    list_display = ('organization_domain', 'status_badge', 'matched_count', 'generated_count', 'total_components', 'last_scan_at')
    list_filter = ('generation_status', 'last_scan_at')
    search_fields = ('organization__domain', 'organization__name')
    list_select_related = ('organization',)
    readonly_fields = ('last_scan_at', 'mapping_data_display', 'failed_components_display')
    
    fieldsets = (
        ('Organization', {
            'fields': ('organization',)
        }),
        ('Stats', {
            'fields': ('total_components', 'matched_count', 'generated_count')
        }),
        ('Generation Info', {
            'fields': ('generation_status', 'design_guide_path', 'storage_local_path', 'storage_pr_url', 'storage_branch_url')
        }),
        ('Failed Components', {
            'fields': ('failed_components_display',),
        }),
        ('Mapping Data', {
            'fields': ('mapping_data_display',),
            'classes': ('collapse',),
        }),
        ('Scan Info', {
            'fields': ('last_scan_commit', 'last_scan_at'),
        }),
    )
    
    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'
    organization_domain.admin_order_field = 'organization__domain'
    
    def status_badge(self, obj):
        """Show generation status as a colored badge."""
        status = obj.generation_status or 'unknown'
        colors = {
            'success': 'green',
            'partial': 'orange',
            'failed': 'red',
            'unknown': 'gray'
        }
        color = colors.get(status, 'gray')
        return format_html('<span style="background: {}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px;">{}</span>', color, status.upper())
    status_badge.short_description = 'Status'
    
    def mapping_data_display(self, obj):
        """Pretty display of mapping_data JSON."""
        if not obj.mapping_data:
            return "No mapping data"
        import json
        formatted = json.dumps(obj.mapping_data, indent=2)
        return format_html('<pre style="background: #f4f4f4; padding: 10px; border-radius: 4px; max-height: 400px; overflow: auto;">{}</pre>', formatted)
    mapping_data_display.short_description = 'Mapping Data (JSON)'
    
    def failed_components_display(self, obj):
        """Display failed components list."""
        if not obj.failed_components:
            return format_html('<span style="color: green;">None ✓</span>')
        return format_html('<span style="color: red;">{}</span>', ', '.join(obj.failed_components))
    failed_components_display.short_description = 'Failed Components'

