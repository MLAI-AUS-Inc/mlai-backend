from django.conf import settings
from django.db import models
from .fields import EncryptedTextField

class GoogleConnection(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='google_connection')
    google_email = models.EmailField()
    refresh_token = EncryptedTextField()  # encrypted at rest
    scope = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Google Connection for {self.user.email} ({self.google_email})"


class UserIntegration(models.Model):
    slack_user_id = models.TextField(primary_key=True, unique=True)
    github_access_token = EncryptedTextField(null=True, blank=True)
    github_user_name = models.TextField(null=True, blank=True)
    github_scopes = models.JSONField(default=list, blank=True)
    project_scanned = models.BooleanField(default=False)
    pending_intent = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_integrations'

    def __str__(self):
        return f"UserIntegration ({self.slack_user_id})"
