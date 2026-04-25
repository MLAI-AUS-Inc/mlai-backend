from django.contrib import admin

from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'domain', 'competitors', 'seed_keywords', 'created_at')
    search_fields = ('name', 'domain')
    ordering = ('name',)
