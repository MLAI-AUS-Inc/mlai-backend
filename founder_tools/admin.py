from django.contrib import admin

from .models import VibeRaisingCompany, VibeRaisingProfile


@admin.register(VibeRaisingProfile)
class VibeRaisingProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "organization_name", "active_company", "updated_at")
    list_filter = ("role",)
    search_fields = ("user__email", "organization_name")
    ordering = ("user__email",)


@admin.register(VibeRaisingCompany)
class VibeRaisingCompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name", "profile", "organization", "domain", "abn", "acn",
        "registered", "is_nonprofit", "abr_verified_at", "updated_at",
    )
    list_filter = ("registered", "is_nonprofit")
    # Let admins flag a registered not-for-profit straight from the list view so it is
    # exempt from the ACN requirement.
    list_editable = ("is_nonprofit",)
    search_fields = (
        "name",
        "domain",
        "abn",
        "profile__user__email",
        "organization__domain",
        "organization__name",
    )
    ordering = ("name",)
