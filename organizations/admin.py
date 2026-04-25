from django.contrib import admin

from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'domain', 'company_linkedin_url', 'competitors', 'seed_keywords', 'created_at')
    search_fields = ('name', 'domain', 'company_linkedin_url')
    ordering = ('name',)
