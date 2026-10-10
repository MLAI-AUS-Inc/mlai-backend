from django.db import models
from django.conf import settings


class Organization(models.Model):
    """Organization that uses content factory."""
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, db_index=True)
    company_linkedin_url = models.URLField(max_length=512, blank=True, default="")
    competitors = models.JSONField(default=list, blank=True)
    seed_keywords = models.JSONField(default=list, blank=True, help_text="Seed keywords for content research")
    billing_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="content_billing_organizations", help_text="Founder who explicitly opted in to pay for company content.")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'content_factory_organization'
