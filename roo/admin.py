from django.contrib import admin
from .models import ArticleGeneration

@admin.register(ArticleGeneration)
class ArticleGenerationAdmin(admin.ModelAdmin):
    list_display = ('domain', 'topic', 'user', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('domain', 'topic', 'slug', 'user__email', 'user__slack_id', 'job_id')
    readonly_fields = ('job_id', 'created_at', 'updated_at')
    
    fieldsets = (
        (None, {
            'fields': ('user', 'job_id', 'status', 'created_at', 'updated_at')
        }),
        ('Content Details', {
            'fields': ('domain', 'topic', 'slug', 'category', 'title')
        }),
        ('SEO Metadata', {
            'fields': ('meta_title', 'meta_description', 'keywords')
        }),
        ('Debug', {
            'fields': ('error_message',)
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')
