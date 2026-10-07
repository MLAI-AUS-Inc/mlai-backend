"""Durable website authority, evidence, and reversible repository operations."""

import uuid

from django.conf import settings
from django.db import models


class WebsiteConnection(models.Model):
    """An explicitly authorized organisation/site/repository binding.

    Generation is a consent fence, independent of a worker's retry generation.
    Credentials deliberately do not live in this record or its evidence JSON.
    """

    class State(models.TextChoices):
        CONNECTED = "connected", "Connected"
        PAUSED = "paused", "Publishing paused"
        DISCONNECTED = "disconnected", "Disconnected"
        REVOKED = "revoked", "Provider access revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="website_connections")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="authorized_websites")
    repository_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    github_repo = models.CharField(max_length=255)
    installation_id = models.CharField(max_length=50, blank=True, default="", db_index=True)
    app_root = models.CharField(max_length=500, blank=True, default="")
    branch = models.CharField(max_length=255, blank=True, default="")
    site_url = models.CharField(max_length=512, blank=True, default="")
    state = models.CharField(max_length=20, choices=State.choices, default=State.CONNECTED)
    generation = models.PositiveBigIntegerField(default=1)
    configuration_version = models.PositiveBigIntegerField(default=1)
    capabilities = models.JSONField(default=dict, blank=True)
    blockers = models.JSONField(default=list, blank=True)
    verified_sha = models.CharField(max_length=64, blank=True, default="")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_website_connection"
        indexes = [models.Index(fields=["organization", "state"], name="cf_web_org_state_idx")]
        constraints = [models.CheckConstraint(check=models.Q(generation__gte=1), name="cf_web_generation_positive")]


class WebsiteConnectionTarget(models.Model):
    """A versioned native publication contract, verified for one connection."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(WebsiteConnection, on_delete=models.CASCADE, related_name="targets")
    target_key = models.CharField(max_length=255)
    generation = models.PositiveBigIntegerField()
    version = models.PositiveBigIntegerField(default=1)
    adapter = models.CharField(max_length=100, blank=True, default="")
    adapter_version = models.CharField(max_length=100, blank=True, default="")
    source_sha = models.CharField(max_length=64, blank=True, default="")
    contract = models.JSONField(default=dict, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_website_target"
        constraints = [models.UniqueConstraint(fields=["connection", "generation", "target_key"], name="cf_web_target_generation_unique")]


class WebsiteScanSnapshot(models.Model):
    """Immutable inventory evidence independent of optional template preparation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(WebsiteConnection, on_delete=models.CASCADE, related_name="scan_snapshots")
    generation = models.PositiveBigIntegerField()
    run_id = models.CharField(max_length=100)
    source_sha = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    detector_version = models.CharField(max_length=100, blank=True, default="")
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_factory_website_scan"
        constraints = [models.UniqueConstraint(fields=["connection", "generation", "run_id", "fingerprint"], name="cf_web_scan_identity_unique")]


class WebsiteTemplateRevision(models.Model):
    """Validated template revisions and restricted legacy quarantine evidence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(WebsiteConnection, on_delete=models.CASCADE, related_name="template_revisions")
    generation = models.PositiveBigIntegerField()
    purpose = models.CharField(max_length=64)
    digest = models.CharField(max_length=64)
    source_sha = models.CharField(max_length=64, blank=True, default="")
    provenance = models.CharField(max_length=40)
    status = models.CharField(max_length=20, default="validated")
    body = models.TextField()
    validation = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_factory_website_template"
        constraints = [models.UniqueConstraint(fields=["connection", "purpose", "digest"], name="cf_web_template_digest_unique")]


class WebsiteRepositoryMutation(models.Model):
    """Ownership evidence for a repository write and conservative cleanup."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(WebsiteConnection, on_delete=models.CASCADE, related_name="repository_mutations")
    generation = models.PositiveBigIntegerField()
    operation_id = models.CharField(max_length=200, unique=True)
    run_id = models.CharField(max_length=100, blank=True, default="")
    base_sha = models.CharField(max_length=64)
    head_sha = models.CharField(max_length=64, blank=True, default="")
    branch = models.CharField(max_length=255, blank=True, default="")
    pr_url = models.URLField(max_length=1000, blank=True, default="")
    patch_digest = models.CharField(max_length=64)
    files = models.JSONField(default=list)
    status = models.CharField(max_length=32, default="proposed")
    cleanup = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_website_mutation"
        indexes = [models.Index(fields=["connection", "status"], name="cf_web_mutation_state_idx")]


class WebsiteConnectionOperation(models.Model):
    """Idempotent lifecycle receipt and retryable cleanup/cancellation outbox."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(WebsiteConnection, on_delete=models.CASCADE, related_name="operations")
    generation = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=255, unique=True)
    action = models.CharField(max_length=32)
    state = models.CharField(max_length=20, default="pending")
    payload = models.JSONField(default=dict)
    receipt = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_website_operation"
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="cf_web_operation_due_idx")]
