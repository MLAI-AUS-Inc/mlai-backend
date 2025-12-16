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
