"""Durable article delivery independent of the web request lifetime."""
from django.db import models
from django.utils import timezone


class ContentFactoryDispatchOutbox(models.Model):
    """An idempotent, charged article start with a bounded delivery budget."""
    client_request_id = models.CharField(max_length=255, unique=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="content_dispatch_outbox")
    payload = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=20, default="pending")
    run_id = models.CharField(max_length=100, blank=True, default="")
    last_error = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_dispatch_outbox"
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="cf_dispatch_outbox_due_idx")]
