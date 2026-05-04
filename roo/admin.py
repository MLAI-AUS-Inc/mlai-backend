from django.contrib import admin
from django.utils.html import format_html
from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskSubmission, CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate, QuestProgress,
)


@admin.register(PointsAdmin)
class PointsAdminAdmin(admin.ModelAdmin):
    list_display = ('admin_name', 'slack_user_id', 'role', 'portfolio', 'is_active', 'created_at')
    list_display_links = ('admin_name',)
    list_select_related = ('user',)
    list_filter = ('role', 'portfolio', 'is_active')
    search_fields = ('slack_user_id', 'user__email', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at',)

    def admin_name(self, obj):
        if obj.user:
            return obj.user.full_name or obj.user.email
        return obj.slack_user_id
    admin_name.short_description = 'Admin Name'
    admin_name.admin_order_field = 'user__first_name'


@admin.register(PointsAccount)
class PointsAccountAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'balance',
        'earned_balance',
        'purchased_topup_balance',
        'lifetime_earned',
        'lifetime_purchased_topup',
        'lifetime_spent',
        'updated_at',
    )
    list_filter = ('updated_at',)
    search_fields = ('user__email', 'user__slack_id')
    readonly_fields = ('user', 'created_at', 'updated_at')
    ordering = ('-balance',)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'points', 'portfolio', 'status', 'assigned_to_display', 'created_at')
    list_filter = ('status', 'portfolio', 'created_at')
    search_fields = ('title', 'description', 'created_by_user_id', 'assigned_to_user_id')
    readonly_fields = ('id', 'created_at', 'updated_at', 'closed_at')
    ordering = ('-created_at',)
    
    def assigned_to_display(self, obj):
        if obj.assigned_user:
            return obj.assigned_user.email
        return obj.assigned_to_user_id or '-'
    assigned_to_display.short_description = 'Assigned To'


@admin.register(TaskSubmission)
class TaskSubmissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'user', 'status', 'created_at', 'approved_at')
    list_filter = ('status', 'created_at')
    search_fields = ('task__title', 'user__email', 'submission_text')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)


@admin.register(Ledger)
class LedgerAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'delta_display', 'kind', 'source', 'description_short', 'created_at')
    list_filter = ('kind', 'source', 'created_at')
    search_fields = ('user__email', 'description', 'idempotency_key')
    readonly_fields = ('id', 'user', 'delta', 'kind', 'source', 'reference_type', 
                       'reference_id', 'description', 'created_by_slack_id', 
                       'idempotency_key', 'created_at')
    ordering = ('-created_at',)
    
    def delta_display(self, obj):
        if obj.delta > 0:
            return format_html('<span style="color: green;">+{}</span>', obj.delta)
        return format_html('<span style="color: red;">{}</span>', obj.delta)
    delta_display.short_description = 'Delta'
    
    def description_short(self, obj):
        if len(obj.description) > 50:
            return obj.description[:50] + '...'
        return obj.description
    description_short.short_description = 'Description'
    
    def has_add_permission(self, request):
        return False  # Ledger entries should only be created via services
    
    def has_delete_permission(self, request, obj=None):
        return False  # Ledger is append-only


@admin.register(CoworkingDayCapacity)
class CoworkingDayCapacityAdmin(admin.ModelAdmin):
    list_display = ('date', 'capacity', 'notes', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('notes',)
    ordering = ('date',)


@admin.register(CoworkingBooking)
class CoworkingBookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'date', 'status', 'points_cost', 'created_at', 'cancelled_at')
    list_filter = ('status', 'date', 'created_at')
    search_fields = ('user__email', 'user__slack_id')
    readonly_fields = ('id', 'created_at', 'ledger_entry', 'refund_ledger_entry')
    ordering = ('-date',)


@admin.register(TaskTemplate)
class TaskTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'alias', 'points', 'is_active', 'description')
    list_editable = ('points', 'is_active')
    search_fields = ('name', 'alias', 'description')
    ordering = ('name',)


@admin.register(RewardsCatalog)
class RewardsCatalogAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'cost_points', 'stock_remaining', 'fulfillment', 'is_active', 'max_per_user')
    list_editable = ('cost_points', 'stock_remaining', 'is_active')
    list_filter = ('fulfillment', 'is_active')
    search_fields = ('code', 'name', 'description')
    ordering = ('cost_points',)


@admin.register(RewardRedemption)
class RewardRedemptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'reward', 'quantity', 'status', 'requested_at', 'approved_at')
    list_filter = ('status', 'reward', 'requested_at')
    search_fields = ('user__email', 'reward__name', 'notes')
    readonly_fields = ('id', 'requested_at', 'ledger_entry')
    ordering = ('-requested_at',)


@admin.register(QuestProgress)
class QuestProgressAdmin(admin.ModelAdmin):
    list_display = ('slack_user_id', 'quest_id', 'current_count', 'completed', 'completed_at', 'first_progress_at')
    list_filter = ('quest_id', 'completed')
    search_fields = ('slack_user_id', 'quest_id')
    readonly_fields = ('first_progress_at', 'created_at', 'updated_at')
    ordering = ('-updated_at',)
