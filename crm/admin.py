from django.contrib import admin

from .models import (
    Blast,
    BlastDelivery,
    Contact,
    ContactConsentEvent,
    ContactImport,
    CrmAuditEvent,
    CrmEvent,
    EventRegistration,
    InboundMessage,
    MessageTemplate,
    Segment,
    SenderMailbox,
)


class EventRegistrationInline(admin.TabularInline):
    model = EventRegistration
    extra = 0
    fields = ("event", "approval_status", "registered_at", "checked_in_at", "removed_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True


class ContactConsentEventInline(admin.TabularInline):
    model = ContactConsentEvent
    extra = 0
    fields = ("channel", "kind", "source", "occurred_at", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "display_name",
        "company",
        "email_status",
        "marketing_consent",
        "sms_status",
        "whatsapp_opt_in",
        "events_attended_count",
        "last_attended_at",
    )
    list_filter = ("email_status", "marketing_consent", "sms_status", "whatsapp_opt_in", "source")
    search_fields = ("email", "display_name", "first_name", "last_name", "company")
    readonly_fields = (
        "source",
        "first_seen_at",
        "last_event_at",
        "last_attended_at",
        "events_registered_count",
        "events_attended_count",
        "marketing_consent",
        "marketing_consent_source",
        "marketing_consent_at",
        "phone_e164",
        "sms_status",
        "sms_status_changed_at",
        "whatsapp_opt_in",
        "whatsapp_opt_in_source",
        "whatsapp_opt_in_at",
        "listmonk_subscriber_id",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("user",)
    inlines = (EventRegistrationInline, ContactConsentEventInline)


@admin.register(CrmEvent)
class CrmEventAdmin(admin.ModelAdmin):
    list_display = ("name", "start_at", "guest_count", "approved_count", "checked_in_count", "guests_synced_at")
    search_fields = ("name", "luma_event_id")
    readonly_fields = [field.name for field in CrmEvent._meta.fields]


@admin.register(EventRegistration)
class EventRegistrationAdmin(admin.ModelAdmin):
    list_display = ("contact", "event", "approval_status", "checked_in_at", "removed_at")
    list_filter = ("approval_status",)
    search_fields = ("contact__email", "event__name", "luma_guest_id")
    readonly_fields = [field.name for field in EventRegistration._meta.fields]


@admin.register(ContactImport)
class ContactImportAdmin(admin.ModelAdmin):
    list_display = ("file_name", "status", "consent", "dry_run", "created_at", "finished_at")
    readonly_fields = [field.name for field in ContactImport._meta.fields]


@admin.register(CrmAuditEvent)
class CrmAuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "target_type", "target_id", "actor", "created_at")
    list_filter = ("action",)
    readonly_fields = [field.name for field in CrmAuditEvent._meta.fields]


@admin.register(SenderMailbox)
class SenderMailboxAdmin(admin.ModelAdmin):
    list_display = ("email", "display_name", "route", "daily_limit", "enabled")
    list_filter = ("route", "enabled")


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "channel", "category", "approved", "enabled", "last_synced_at")
    list_filter = ("channel", "category", "approved", "enabled")
    search_fields = ("name", "content_sid")


@admin.register(Blast)
class BlastAdmin(admin.ModelAdmin):
    list_display = ("subject", "purpose", "status", "event", "recipient_count", "queued_at", "sent_at")
    list_filter = ("status", "purpose")
    readonly_fields = [field.name for field in Blast._meta.fields]


@admin.register(BlastDelivery)
class BlastDeliveryAdmin(admin.ModelAdmin):
    list_display = ("blast", "contact", "channel", "status", "error_code", "updated_at")
    list_filter = ("channel", "status")
    search_fields = ("contact__email", "provider_id")
    readonly_fields = [field.name for field in BlastDelivery._meta.fields]


@admin.register(InboundMessage)
class InboundMessageAdmin(admin.ModelAdmin):
    list_display = ("from_address", "channel", "contact", "blast", "received_at")
    list_filter = ("channel",)
    readonly_fields = [field.name for field in InboundMessage._meta.fields]


admin.site.register(Segment)
