from django.contrib import admin

from .models import (
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatInviteAudit,
)


@admin.register(CommunityChatDevice)
class CommunityChatDeviceAdmin(admin.ModelAdmin):
    list_display = ("user_id", "public_key_prefix", "status", "verified_at", "revoked_at")
    list_filter = ("status",)
    search_fields = ("user__email", "public_key")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "verified_at",
        "last_verified_membership_at",
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
