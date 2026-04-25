from django.contrib import admin

from .models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt


class ContentFactoryRunStepInline(admin.TabularInline):
    model = ContentFactoryRunStep
    extra = 0
    fields = ('display_order', 'step_key', 'required', 'status', 'attempts', 'started_at', 'completed_at')
    readonly_fields = ('started_at', 'completed_at')
    ordering = ('display_order', 'id')


@admin.register(ContentFactoryRun)
class ContentFactoryRunAdmin(admin.ModelAdmin):
    list_display = ('run_id', 'workflow', 'domain', 'status', 'current_step', 'approval_state', 'resume_available', 'updated_at')
    list_filter = ('workflow', 'status', 'approval_state', 'resume_available', 'updated_at')
    search_fields = ('run_id', 'domain', 'github_repo', 'slack_user_id')
    ordering = ('-updated_at',)
    readonly_fields = ('created_at', 'updated_at')
    inlines = [ContentFactoryRunStepInline]


@admin.register(ContentFactoryRunStep)
class ContentFactoryRunStepAdmin(admin.ModelAdmin):
    list_display = ('run', 'step_key', 'display_order', 'required', 'status', 'attempts', 'updated_run_at')
    list_filter = ('status', 'required')
    search_fields = ('run__run_id', 'step_key')
    ordering = ('run', 'display_order')

    def updated_run_at(self, obj):
        return obj.run.updated_at
    updated_run_at.admin_order_field = 'run__updated_at'


@admin.register(ContentFactoryRunStepAttempt)
class ContentFactoryRunStepAttemptAdmin(admin.ModelAdmin):
    list_display = ('step', 'attempt', 'status', 'started_at', 'completed_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('step__run__run_id', 'step__step_key')
    ordering = ('step', 'attempt')
