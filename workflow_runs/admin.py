from django.contrib import admin

from .models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt


def _fact_owned(obj):
    from content_factory.run_ownership import owns_backend_facts
    if isinstance(obj, ContentFactoryRun):
        return owns_backend_facts(obj)
    if isinstance(obj, ContentFactoryRunStep):
        return owns_backend_facts(obj.run)
    if isinstance(obj, ContentFactoryRunStepAttempt):
        return owns_backend_facts(obj.step.run)
    return False


class _RunOwnerAdmin:
    """Administrative forms cannot bypass admitted fact/event ownership."""

    def _assert_legacy(self, obj):
        from django.core.exceptions import PermissionDenied
        saved = self.model.objects.filter(pk=obj.pk).first() if obj.pk else None
        if _fact_owned(obj) or (saved is not None and _fact_owned(saved)):
            raise PermissionDenied("Admitted execution history requires its authorized controller.")

    def has_change_permission(self, request, obj=None):
        return False if obj is not None and _fact_owned(obj) else super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False if obj is not None and _fact_owned(obj) else super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        self._assert_legacy(obj)
        return super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        self._assert_legacy(obj)
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        from django.db import transaction
        with transaction.atomic():
            for obj in queryset:
                self._assert_legacy(obj)
            return super().delete_queryset(request, queryset)


class ContentFactoryRunStepInline(admin.TabularInline):
    model = ContentFactoryRunStep
    extra = 0
    fields = ('display_order', 'step_key', 'required', 'status', 'attempts', 'started_at', 'completed_at')
    readonly_fields = ('started_at', 'completed_at')
    ordering = ('display_order', 'id')

    def has_add_permission(self, request, obj=None):
        return False if obj is not None and _fact_owned(obj) else super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return False if obj is not None and _fact_owned(obj) else super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False if obj is not None and _fact_owned(obj) else super().has_delete_permission(request, obj)


@admin.register(ContentFactoryRun)
class ContentFactoryRunAdmin(_RunOwnerAdmin, admin.ModelAdmin):
    list_display = ('run_id', 'workflow', 'domain', 'status', 'current_step', 'approval_state', 'resume_available', 'updated_at')
    list_filter = ('workflow', 'status', 'approval_state', 'resume_available', 'updated_at')
    search_fields = ('run_id', 'domain', 'github_repo', 'slack_user_id')
    ordering = ('-updated_at',)
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields = ('billing_ledger',)
    inlines = [ContentFactoryRunStepInline]


@admin.register(ContentFactoryRunStep)
class ContentFactoryRunStepAdmin(_RunOwnerAdmin, admin.ModelAdmin):
    list_display = ('run', 'step_key', 'display_order', 'required', 'status', 'attempts', 'updated_run_at')
    list_filter = ('status', 'required')
    search_fields = ('run__run_id', 'step_key')
    ordering = ('run', 'display_order')

    def updated_run_at(self, obj):
        return obj.run.updated_at
    updated_run_at.admin_order_field = 'run__updated_at'


@admin.register(ContentFactoryRunStepAttempt)
class ContentFactoryRunStepAttemptAdmin(_RunOwnerAdmin, admin.ModelAdmin):
    list_display = ('step', 'attempt', 'status', 'started_at', 'completed_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('step__run__run_id', 'step__step_key')
    ordering = ('step', 'attempt')
