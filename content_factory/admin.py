from django.contrib import admin
from django.utils.html import format_html

from .models import (
    AISaturation,
    ClusterMembership,
    ComponentMapping,
    ContentFactoryHealingRecord,
    ContentFactoryJob,
    GeneratedComponent,
    KeywordVelocity,
    OrganizationContentConfig,
    PAQuestion,
    ResearchSession,
    ResearchedKeyword,
    ScheduledDiscoveryDispatch,
    SemanticCluster,
    TopicMap,
    WrittenArticle,
)


@admin.register(OrganizationContentConfig)
class OrganizationContentConfigAdmin(admin.ModelAdmin):
    list_display = ('organization', 'connected_slack_user_id', 'brand_name', 'github_repo', 'has_scan', 'articles_scaffolded', 'updated_at')
    search_fields = ('organization__name', 'organization__domain', 'connected_slack_user_id', 'brand_name', 'github_repo')
    list_select_related = ('organization',)
    list_filter = ('updated_at',)
    readonly_fields = ('created_at', 'updated_at', 'tech_stack_display', 'installed_packages_display', 'pillar_strategy_display', 'article_template_preview', 'design_guide_preview')

    fieldsets = (
        ('Organization', {
            'fields': ('organization', 'connected_slack_user_id', 'brand_name')
        }),
        ('GitHub Integration', {
            'fields': ('github_repo', 'github_token_encrypted', 'article_path_pattern', 'registry_path')
        }),
        ('Scan Results', {
            'fields': ('scan_summary', 'tech_stack_display', 'installed_packages_display'),
            'description': 'Data returned from the content-factory scanner agent'
        }),
        ('SEO Content Pillars', {
            'fields': ('pillar_strategy_display',),
            'description': 'Content pillars derived from company context for SEO strategy'
        }),
        ('Templates (LLM Generated)', {
            'fields': ('article_template_preview', 'design_guide_preview'),
            'classes': ('collapse',),  # Collapsible since these are large
        }),
        ('Raw Template Data', {
            'fields': ('article_template', 'design_guide', 'company_context'),
            'classes': ('collapse',),
        }),
        ('Article Scaffolding', {
            'fields': ('articles_scaffolded', 'articles_scaffold_pr_url'),
        }),
        ('Raw Data (Editable)', {
            'fields': ('installed_packages', 'pillar_strategy'),
            'classes': ('collapse',),
            'description': 'Edit raw JSON data directly'
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

    def installed_packages_display(self, obj):
        """Pretty display of installed_packages JSON."""
        if not obj.installed_packages:
            return "No package data (run a scan to populate)"
        import json
        # Sort packages alphabetically for readability
        sorted_packages = dict(sorted(obj.installed_packages.items()))
        formatted = json.dumps(sorted_packages, indent=2)
        count = len(obj.installed_packages)
        return format_html(
            '<div style="margin-bottom: 8px;"><strong>{} packages installed</strong></div>'
            '<pre style="background: #f4f4f4; padding: 10px; border-radius: 4px; max-height: 300px; overflow: auto;">{}</pre>',
            count, formatted
        )
    installed_packages_display.short_description = 'Installed Packages (from package.json)'

    def pillar_strategy_display(self, obj):
        """Pretty display of pillar_strategy JSON."""
        if not obj.pillar_strategy:
            return "No pillar data (run a scan to generate)"
        import json

        pillars = obj.pillar_strategy.get('pillars', [])
        if not pillars:
            return format_html('<pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{}</pre>',
                             json.dumps(obj.pillar_strategy, indent=2))

        # Build a nice HTML display
        html = '<div style="display: flex; flex-wrap: wrap; gap: 12px;">'
        for pillar in pillars:
            name = pillar.get('name', 'Unknown')
            slug = pillar.get('slug', '')
            description = pillar.get('description', '')[:100]
            topics = pillar.get('topics', [])[:5]  # Show first 5 topics

            html += f'''
            <div style="background: #e8f4fd; border: 1px solid #1976d2; border-radius: 8px; padding: 12px; min-width: 250px; max-width: 300px;">
                <div style="font-weight: bold; color: #1565c0; margin-bottom: 4px;">{name}</div>
                <div style="font-size: 11px; color: #666; margin-bottom: 8px;">/{slug}/</div>
                <div style="font-size: 12px; color: #333; margin-bottom: 8px;">{description}...</div>
                <div style="font-size: 11px; color: #555;">
                    <strong>Topics:</strong> {', '.join(topics[:3])}{'...' if len(topics) > 3 else ''}
                </div>
            </div>
            '''
        html += '</div>'

        return format_html(html)
    pillar_strategy_display.short_description = 'Content Pillars'

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


# =============================================================================
# SEO Research Admin Classes
# =============================================================================

@admin.register(ContentFactoryJob)
class ContentFactoryJobAdmin(admin.ModelAdmin):
    """Admin for content factory job tracking."""
    list_display = ('job_id', 'domain', 'status', 'selected_keyword', 'slack_user_id', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('job_id', 'domain', 'selected_keyword', 'slack_user_id')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')


class KeywordVelocityInline(admin.TabularInline):
    """Inline for velocity snapshots on keyword detail."""
    model = KeywordVelocity
    extra = 0
    readonly_fields = ('velocity_score', 'trend_status', 'absolute_volume', 'captured_at')
    can_delete = False
    max_num = 5
    ordering = ('-captured_at',)


class AISaturationInline(admin.TabularInline):
    """Inline for AI saturation snapshots on keyword detail."""
    model = AISaturation
    extra = 0
    readonly_fields = ('saturation_score', 'ai_overview_present', 'hostility_score', 'captured_at')
    can_delete = False
    max_num = 5
    ordering = ('-captured_at',)


class PAQuestionInline(admin.TabularInline):
    """Inline for PAA questions on keyword detail."""
    model = PAQuestion
    extra = 0
    readonly_fields = ('question', 'depth', 'has_ai_overview')
    can_delete = False
    max_num = 10


@admin.register(ResearchedKeyword)
class ResearchedKeywordAdmin(admin.ModelAdmin):
    """Admin for researched keywords - the core SEO research table."""
    list_display = (
        'keyword', 'organization_domain', 'volume', 'difficulty',
        'tier_badge', 'opportunity_index', 'status_badge', 'source', 'discovered_at'
    )
    list_filter = ('status', 'tier', 'source', 'organization', 'discovered_at')
    search_fields = ('keyword', 'organization__domain', 'organization__name')
    list_select_related = ('organization', 'written_article')
    ordering = ('-opportunity_index',)
    readonly_fields = ('keyword_normalized', 'discovered_at', 'metrics_updated_at', 'status_changed_at')
    inlines = [KeywordVelocityInline, AISaturationInline, PAQuestionInline]

    fieldsets = (
        ('Keyword', {
            'fields': ('organization', 'keyword', 'keyword_normalized')
        }),
        ('Metrics', {
            'fields': ('volume', 'difficulty', 'intent', 'tier', 'opportunity_index')
        }),
        ('Provenance', {
            'fields': ('source', 'source_detail', 'competitor_urls')
        }),
        ('Status', {
            'fields': ('status', 'written_article', 'status_changed_at')
        }),
        ('Timestamps', {
            'fields': ('discovered_at', 'metrics_updated_at')
        }),
    )

    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'
    organization_domain.admin_order_field = 'organization__domain'

    def tier_badge(self, obj):
        """Display tier as colored badge."""
        colors = {
            'tier_1_blue_ocean': '#0066cc',
            'tier_2_authority': '#ff9900',
            'tier_3_long_tail': '#666666',
            'tier_4_discard': '#cc0000',
        }
        labels = {
            'tier_1_blue_ocean': 'Blue Ocean',
            'tier_2_authority': 'Authority',
            'tier_3_long_tail': 'Long Tail',
            'tier_4_discard': 'Discard',
        }
        color = colors.get(obj.tier, '#999')
        label = labels.get(obj.tier, obj.tier)
        return format_html(
            '<span style="background: {}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;">{}</span>',
            color, label
        )
    tier_badge.short_description = 'Tier'

    def status_badge(self, obj):
        """Display status as colored badge."""
        colors = {
            'pending': '#999',
            'approved': '#0066cc',
            'in_progress': '#ff9900',
            'written': '#00cc00',
            'skipped': '#cc0000',
        }
        color = colors.get(obj.status, '#999')
        return format_html(
            '<span style="background: {}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;">{}</span>',
            color, obj.status.upper()
        )
    status_badge.short_description = 'Status'


@admin.register(WrittenArticle)
class WrittenArticleAdmin(admin.ModelAdmin):
    """Admin for tracking written articles."""
    list_display = ('title', 'organization_domain', 'slug', 'primary_keyword', 'published_at', 'created_at')
    list_filter = ('organization', 'published_at', 'created_at')
    search_fields = ('title', 'slug', 'primary_keyword', 'organization__domain')
    list_select_related = ('organization', 'job')
    ordering = ('-created_at',)
    readonly_fields = ('created_at',)

    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'


@admin.register(SemanticCluster)
class SemanticClusterAdmin(admin.ModelAdmin):
    """Admin for semantic clusters (pillar topics)."""
    list_display = ('pillar_keyword', 'organization_domain', 'cluster_id', 'total_volume', 'avg_difficulty', 'topic_tier', 'member_count')
    list_filter = ('topic_tier', 'organization')
    search_fields = ('pillar_keyword', 'organization__domain')
    list_select_related = ('organization',)
    ordering = ('-total_volume',)

    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'

    def member_count(self, obj):
        return obj.member_keywords.count()
    member_count.short_description = 'Members'


@admin.register(TopicMap)
class TopicMapAdmin(admin.ModelAdmin):
    """Admin for topic map snapshots."""
    list_display = ('organization_domain', 'total_keywords', 'clustering_threshold', 'created_at')
    list_filter = ('organization', 'created_at')
    list_select_related = ('organization',)
    ordering = ('-created_at',)

    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'


@admin.register(ResearchSession)
class ResearchSessionAdmin(admin.ModelAdmin):
    """Admin for research session tracking."""
    list_display = ('organization_domain', 'keywords_discovered', 'keywords_updated', 'clusters_created', 'started_at', 'completed_at')
    list_filter = ('organization', 'started_at')
    list_select_related = ('organization',)
    ordering = ('-started_at',)
    readonly_fields = ('started_at',)

    def organization_domain(self, obj):
        return obj.organization.domain
    organization_domain.short_description = 'Domain'


# Register remaining models with basic admin
@admin.register(ClusterMembership)
class ClusterMembershipAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'cluster', 'is_pillar', 'similarity_score')
    list_filter = ('is_pillar', 'cluster__organization')
    search_fields = ('keyword__keyword', 'cluster__pillar_keyword')


@admin.register(KeywordVelocity)
class KeywordVelocityAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'velocity_score', 'trend_status', 'absolute_volume', 'captured_at')
    list_filter = ('trend_status', 'captured_at')
    search_fields = ('keyword__keyword',)
    ordering = ('-captured_at',)


@admin.register(AISaturation)
class AISaturationAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'domain', 'saturation_score', 'ai_overview_present', 'hostility_score', 'captured_at')
    list_filter = ('domain', 'ai_overview_present', 'captured_at')
    search_fields = ('keyword__keyword', 'domain')
    ordering = ('-captured_at',)


@admin.register(PAQuestion)
class PAQuestionAdmin(admin.ModelAdmin):
    list_display = ('question_truncated', 'keyword', 'domain', 'depth', 'has_ai_overview', 'discovered_at')
    list_filter = ('domain', 'depth', 'has_ai_overview', 'discovered_at')
    search_fields = ('question', 'keyword__keyword', 'domain')
    ordering = ('-discovered_at',)

    def question_truncated(self, obj):
        return obj.question[:80] + '...' if len(obj.question) > 80 else obj.question
    question_truncated.short_description = 'Question'


@admin.register(ScheduledDiscoveryDispatch)
class ScheduledDiscoveryDispatchAdmin(admin.ModelAdmin):
    list_display = ('domain', 'slack_user_id', 'local_date', 'state', 'scheduled_for_at', 'content_factory_job_id', 'updated_at')
    list_filter = ('state', 'trigger_source', 'local_date')
    search_fields = ('domain', 'slack_user_id', 'content_factory_job_id')
    ordering = ('-local_date', '-updated_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ContentFactoryHealingRecord)
class ContentFactoryHealingRecordAdmin(admin.ModelAdmin):
    list_display = ('domain', 'github_repo', 'failure_kind', 'failure_family_key', 'promotion_state', 'latest_run_id', 'updated_at')
    list_filter = ('failure_kind', 'promotion_state', 'updated_at')
    search_fields = ('domain', 'github_repo', 'failure_family_key', 'summary', 'latest_run_id')
    ordering = ('-updated_at',)
    readonly_fields = ('created_at', 'updated_at')
