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
    list_display = ("name", "profile", "domain", "abn", "registered", "updated_at")
    list_filter = ("registered",)
    search_fields = ("name", "domain", "abn", "profile__user__email")
    ordering = ("name",)
