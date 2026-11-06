"""Administrator offer configuration and read-only fulfilment diagnostics."""

from django.contrib import admin
from .models import EventDiscountOffer, EventDiscountQuote, EventDiscountRedemption


@admin.register(EventDiscountOffer)
class EventDiscountOfferAdmin(admin.ModelAdmin):
    """Configure approved event terms without mutating historical quote snapshots."""

    list_display = ("reward", "event_id", "event_ticket_type_id", "discount_type", "is_active", "valid_end_at")
    list_filter = ("is_active", "discount_type")
    search_fields = ("event_id", "reward__name", "reward__code")
    readonly_fields = ("created_at", "updated_at")
    actions = None

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyEventReceiptAdmin(admin.ModelAdmin):
    """Keep operation state and quote/ledger lineage immutable in the admin UI."""

    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EventDiscountQuote)
class EventDiscountQuoteAdmin(ReadOnlyEventReceiptAdmin):
    """Inspect saved member quotes without changing consent or terms."""

    list_display = ("id", "user", "offer", "expires_at", "created_at")
    list_select_related = ("user", "offer")


@admin.register(EventDiscountRedemption)
class EventDiscountRedemptionAdmin(ReadOnlyEventReceiptAdmin):
    """Expose pending/review state for operational reconciliation."""

    list_display = ("id", "user", "event_id", "status", "needs_review", "last_error_code", "attempts", "next_attempt_at")
    list_filter = ("status", "needs_review")
    list_select_related = ("user", "offer")
    search_fields = ("id", "event_id")
