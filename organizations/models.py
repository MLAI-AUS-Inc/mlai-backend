from django.db import models


class Organization(models.Model):
    """Organization that uses content factory."""
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, db_index=True)
    company_linkedin_url = models.URLField(max_length=512, blank=True, default="")
    competitors = models.JSONField(default=list, blank=True)
    seed_keywords = models.JSONField(default=list, blank=True, help_text="Seed keywords for content research")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'content_factory_organization'
