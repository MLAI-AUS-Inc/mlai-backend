from django.contrib import admin

from .models import (
    CommunityChatBootstrapToken,
    CommunityChatAccountSession,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    CommunityChatInviteAudit,
)


@admin.register(CommunityChatDevice)
class CommunityChatDeviceAdmin(admin.ModelAdmin):
    list_display = ("user_id", "name", "platform", "public_key_prefix", "status", "verified_at", "revoked_at")
    list_filter = ("status", "client_id", "platform")
    search_fields = ("user__email", "public_key", "installation_id", "name")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "verified_at",
        "last_verified_membership_at",
        "last_seen_at",
        "revoked_at",
    )

    @admin.display(description="Public key")
    def public_key_prefix(self, obj):
        return obj.public_key[:16]


@admin.register(CommunityChatChallenge)
class CommunityChatChallengeAdmin(admin.ModelAdmin):
    list_display = ("id", "user_id", "public_key_prefix", "origin", "expires_at", "used_at")
    readonly_fields = tuple(field.name for field in CommunityChatChallenge._meta.fields)

    @admin.display(description="Public key")
    def public_key_prefix(self, obj):
        return obj.public_key[:16]


@admin.register(CommunityChatInviteAudit)
class CommunityChatInviteAuditAdmin(admin.ModelAdmin):
    list_display = ("id", "device_id", "adapter_invite_id", "issued_at", "confirmed_at")
    readonly_fields = tuple(field.name for field in CommunityChatInviteAudit._meta.fields)


@admin.register(CommunityChatDeviceAuthRequest)
class CommunityChatDeviceAuthRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "public_key", "user_id", "origin", "expires_at", "consumed_at")
    readonly_fields = tuple(field.name for field in CommunityChatDeviceAuthRequest._meta.fields)


@admin.register(CommunityChatBootstrapToken)
class CommunityChatBootstrapTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user_id", "public_key", "expires_at", "revoked_at")
    readonly_fields = tuple(field.name for field in CommunityChatBootstrapToken._meta.fields)


@admin.register(CommunityChatEmailCodeChallenge)
class CommunityChatEmailCodeChallengeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user_id",
        "client_id",
        "platform",
        "attempt_count",
        "expires_at",
        "consumed_at",
        "invalidated_at",
    )
    list_filter = ("client_id", "platform")
    readonly_fields = tuple(field.name for field in CommunityChatEmailCodeChallenge._meta.fields)


@admin.register(CommunityChatEmailCodeDelivery)
class CommunityChatEmailCodeDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "challenge_id",
        "status",
        "attempts",
        "available_at",
        "sent_at",
    )
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in CommunityChatEmailCodeDelivery._meta.fields)


@admin.register(CommunityChatAccountSession)
class CommunityChatAccountSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user_id",
        "client_id",
        "platform",
        "installation_id",
        "access_expires_at",
        "expires_at",
        "revoked_at",
    )
    list_filter = ("client_id", "platform")
    search_fields = ("user__email", "installation_id", "name")
    readonly_fields = tuple(field.name for field in CommunityChatAccountSession._meta.fields)
