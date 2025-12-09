from django.db import models
from django.conf import settings

class ArticleGeneration(models.Model):
    """
    Stores the result of a Content Factory generation job.
    """
    STATUS_CHOICES = (
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='generated_articles')
    job_id = models.UUIDField(unique=True, help_text="ID from Content Factory")
    
    # Input params (snapshot)
    domain = models.CharField(max_length=255)
    
    # Result data (populated when complete)
    topic = models.CharField(max_length=255, blank=True, null=True)
    slug = models.SlugField(max_length=255, blank=True, null=True)
    category = models.CharField(max_length=100, blank=True, null=True)
    title = models.CharField(max_length=255, blank=True, null=True)
    meta_title = models.CharField(max_length=60, blank=True, null=True)
    meta_description = models.CharField(max_length=160, blank=True, null=True)
    keywords = models.JSONField(default=list, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Optional: store full result/logs if needed
    error_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.domain} - {self.topic or 'Generating...'}"
