"""Personal calendar credentials, separate from startup/Gmail integrations."""
from django.conf import settings
from django.db import models

from integrations.fields import EncryptedTextField


class MentorCalendarConnection(models.Model):
    """One verified Google identity authorised by a mentor for scheduling."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mentor_calendar_connections')
    provider = models.CharField(max_length=20, default='google')
    provider_subject = models.CharField(max_length=255)
    email = models.EmailField()
    refresh_token = EncryptedTextField(blank=True, default='')
    access_token = EncryptedTextField(blank=True, default='')
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list)
    status = models.CharField(max_length=24, default='connected')
    conflict_calendar_ids = models.JSONField(default=list)
    booking_calendar_id = models.CharField(max_length=1024, blank=True, default='')
    calendar_metadata = models.JSONField(default=list)
    checked_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default='')
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('user', 'provider', 'provider_subject'), name='mentor_calendar_identity_unique')]
        indexes = [models.Index(fields=('user', 'status'), name='mentor_calendar_user_state')]
