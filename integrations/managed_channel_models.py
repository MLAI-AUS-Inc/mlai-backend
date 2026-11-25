"""Community-scoped identity, inbound work and independent destination receipts."""
import uuid

from django.db import models
from django.utils import timezone


class ManagedChannelScope(models.Model):
    community = models.CharField(max_length=255)
    provider = models.CharField(max_length=16)
    scope = models.CharField(max_length=255)
    scanned_at = models.DateTimeField(null=True)
    retry_at = models.DateTimeField(default=timezone.now)
    error = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [('community', 'provider', 'scope')]


class ManagedEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    community = models.CharField(max_length=255)
    authority = models.ForeignKey(ManagedChannelScope, on_delete=models.PROTECT)
    section = models.CharField(max_length=16)
    snapshot = models.JSONField(default=dict)
    revision = models.PositiveBigIntegerField(default=0)


class ManagedReference(models.Model):
    scope = models.ForeignKey(ManagedChannelScope, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=255)
    entity = models.ForeignKey(ManagedEntity, on_delete=models.PROTECT)
    # The last confirmed observation from this source, including non-authoritative mirrors.
    snapshot = models.JSONField(default=dict)
    canceled = models.BooleanField(default=False)
    seen_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = [('scope', 'external_id')]


class ManagedRoom(models.Model):
    entity = models.OneToOneField(ManagedEntity, on_delete=models.PROTECT)
    channel_id = models.UUIDField(default=uuid.uuid4)
    community = models.CharField(max_length=255)
    adopted = models.BooleanField(default=False)
    section_override = models.CharField(max_length=16, blank=True)
    visibility = models.CharField(max_length=16)
    members = models.JSONField(default=list)

    class Meta:
        unique_together = [('community', 'channel_id')]


class ManagedReceipt(models.Model):
    scope = models.ForeignKey(ManagedChannelScope, on_delete=models.PROTECT)
    delivery_id = models.CharField(max_length=255)
    external_id = models.CharField(max_length=255)
    action = models.CharField(max_length=32)
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [('scope', 'delivery_id')]


class ManagedDelivery(models.Model):
    """A single desired-state queue per room and destination; versioned by entity."""
    entity = models.ForeignKey(ManagedEntity, on_delete=models.PROTECT)
    target = models.CharField(max_length=20)
    applied_revision = models.PositiveBigIntegerField(default=0)
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [('entity', 'target')]


class ManagedSlackEventChannel(models.Model):
    """Durable intent is reserved before Slack writes; bridge discovery honors it."""
    room = models.OneToOneField(ManagedRoom, on_delete=models.PROTECT)
    workspace = models.CharField(max_length=100)
    handle = models.CharField(max_length=80)
    slack_channel_id = models.CharField(max_length=100, null=True)
    create_attempted = models.BooleanField(default=False)

    class Meta:
        unique_together = [('workspace', 'handle'), ('workspace', 'slack_channel_id')]
