"""Durable synchronization metadata; private event payloads remain encrypted."""

from django.db import models
from django.utils import timezone

from .fields import EncryptedTextField


class BridgeSyncInbox(models.Model):
    app_id = models.CharField(max_length=100)
    workspace_id = models.CharField(max_length=100)
    source_event_id = models.CharField(max_length=255)
    encrypted_payload = EncryptedTextField()
    status = models.CharField(max_length=20, default="pending")
    available_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        db_table = "bridge_sync_inbox"
        constraints = [models.UniqueConstraint(
            fields=["app_id", "workspace_id", "source_event_id"], name="bridge_inbox_event_unique",
        )]
        indexes = [models.Index(fields=["status", "available_at"], name="bridge_inbox_due_idx")]


class BridgeSyncState(models.Model):
    public_channel = models.OneToOneField(
        "integrations.CommunityBridgeChannel", null=True, blank=True,
        on_delete=models.CASCADE, related_name="sync_state",
    )
    private_conversation = models.OneToOneField(
        "integrations.SlackDmMirrorConversation", null=True, blank=True,
        on_delete=models.CASCADE, related_name="sync_state",
    )
    workspace_id = models.CharField(max_length=100)
    source_channel_id = models.CharField(max_length=100)
    authority_generation = models.PositiveBigIntegerField(default=1)
    archive_cursor = models.JSONField(default=dict, blank=True)
    head_cursor = models.JSONField(default=dict, blank=True)
    verified_ranges = models.JSONField(default=dict, blank=True)
    latest_source_activity = models.CharField(max_length=32, blank=True, default="")
    last_successful_scan_at = models.DateTimeField(null=True, blank=True)
    last_successful_delivery_at = models.DateTimeField(null=True, blank=True)
    next_due_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, default="pending")
    last_error_code = models.CharField(max_length=100, blank=True, default="")
    last_served_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "integrations"
        db_table = "bridge_sync_state"
        constraints = [models.CheckConstraint(
            check=(models.Q(public_channel__isnull=False, private_conversation__isnull=True)
                       | models.Q(public_channel__isnull=True, private_conversation__isnull=False)),
            name="bridge_sync_exactly_one_owner",
        )]
        indexes = [models.Index(fields=["workspace_id", "last_served_at"], name="bridge_sync_fair_idx")]


class BridgeSyncJob(models.Model):
    state = models.ForeignKey(BridgeSyncState, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=20, choices=[(v, v) for v in ("head", "archive", "thread", "authority")])
    source_object_key = models.CharField(max_length=100, blank=True, default="")
    checkpoint = models.JSONField(default=dict, blank=True)
    due_at = models.DateTimeField(default=timezone.now)
    priority_lane = models.CharField(max_length=20, default="background")
    last_served_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    backoff_seconds = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "integrations"
        db_table = "bridge_sync_job"
        constraints = [models.UniqueConstraint(
            fields=["state", "kind", "source_object_key"], name="bridge_sync_job_unique",
        )]
        indexes = [
            models.Index(fields=["kind", "due_at"], name="bridge_sync_job_due_idx"),
            models.Index(fields=["lease_expires_at"], name="bridge_sync_job_lease_idx"),
        ]


class BridgeApiBudget(models.Model):
    app_id = models.CharField(max_length=100)
    workspace_id = models.CharField(max_length=100)
    method = models.CharField(max_length=100)
    next_admitted_at = models.DateTimeField(default=timezone.now)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        db_table = "bridge_api_budget"
        constraints = [models.UniqueConstraint(
            fields=["app_id", "workspace_id", "method"], name="bridge_api_budget_unique",
        )]


class BridgeWorkerHeartbeat(models.Model):
    worker_id = models.CharField(max_length=100)
    lane = models.CharField(max_length=40)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    progressed_at = models.DateTimeField(null=True, blank=True)
    completed_count = models.PositiveBigIntegerField(default=0)
    failed_count = models.PositiveBigIntegerField(default=0)
    last_error_code = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        app_label = "integrations"
        db_table = "bridge_worker_heartbeat"
        constraints = [models.UniqueConstraint(
            fields=["worker_id", "lane"], name="bridge_heartbeat_worker_unique",
        )]
