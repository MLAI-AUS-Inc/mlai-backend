"""Agent control-plane state; no provider secrets or private-memory authority.

Schema changes require a separately approved migration. Runtime services never
create tables or apply migrations at startup.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class AgentInstallation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    organization = models.ForeignKey("organizations.Organization", null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    runtime = models.CharField(max_length=20, default="hermes", choices=[("hermes", "Hermes"), ("external", "External")])
    public_key = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # Empty fields preserve the existing Slack/Hermes installation contract.
    community_id = models.UUIDField(null=True, blank=True)
    app_id = models.CharField(max_length=80, blank=True, default="")
    state = models.CharField(max_length=24, default="active", choices=[
        (x, x) for x in ("awaiting_owner_proof", "provisioning", "active", "paused", "revoked")])
    owner_device = models.ForeignKey("community_chat.CommunityChatDevice", null=True, blank=True,
                                    on_delete=models.PROTECT)
    owner_attestation = models.JSONField(default=list)
    sealed_signing_key = models.TextField(blank=True, default="")
    native_cursor = models.JSONField(default=dict)
    profile_events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(AgentInstallation, on_delete=models.PROTECT, related_name="grants")
    scopes = models.JSONField(default=list)
    resources = models.JSONField(default=list)
    generation = models.PositiveIntegerField(default=1)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    max_concurrent_tasks = models.PositiveIntegerField(default=1)
    daily_task_limit = models.PositiveIntegerField(default=20)
    daily_cost_limit_microunits = models.PositiveBigIntegerField(default=1000000)
    max_task_seconds = models.PositiveIntegerField(default=300)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(check=Q(generation__gte=1), name="agent_grant_generation_positive")]


class AgentResponsibility(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(AgentGrant, on_delete=models.PROTECT, related_name="responsibilities")
    generation = models.PositiveIntegerField()
    name = models.CharField(max_length=120)
    instruction = models.TextField()
    resource = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)
    interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    next_run_at = models.DateTimeField(db_index=True)
    timezone_name = models.CharField(max_length=64, default="Australia/Melbourne")
    quiet_start = models.TimeField(null=True, blank=True)
    quiet_end = models.TimeField(null=True, blank=True)
    limits = models.JSONField(default=dict)
    delivery = models.JSONField(default=dict)
    last_scheduled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentTask(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        RETRY = "retry", "Retry"
        WAITING_APPROVAL = "waiting_approval", "Waiting for approval"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(AgentGrant, on_delete=models.PROTECT, related_name="tasks")
    generation = models.PositiveIntegerField()
    responsibility = models.ForeignKey(AgentResponsibility, null=True, blank=True, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=160)
    input_hash = models.CharField(max_length=64)
    instruction = models.TextField()
    resource = models.CharField(max_length=300, blank=True, default="")
    inputs = models.JSONField(default=list)
    limits = models.JSONField(default=dict)
    delivery = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    available_at = models.DateTimeField(db_index=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_epoch = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    worker_id = models.CharField(max_length=120, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    reserved_cost_microunits = models.PositiveBigIntegerField(default=0)
    actual_cost_microunits = models.PositiveBigIntegerField(default=0)
    model_calls = models.PositiveIntegerField(default=0)
    reserved_model_tokens = models.PositiveBigIntegerField(default=0)
    model_tokens_used = models.PositiveBigIntegerField(default=0)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("grant", "idempotency_key"), name="agent_task_idempotency_unique")]
        indexes = [models.Index(fields=("status", "available_at"), name="agent_task_due"), models.Index(fields=("status", "lease_expires_at"), name="agent_task_lease")]


class AgentCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(AgentGrant, on_delete=models.PROTECT, related_name="credentials")
    kind = models.CharField(max_length=20, choices=[(x, x) for x in ("access", "refresh", "task_tool", "task_model")])
    token_hash = models.CharField(max_length=64, unique=True)
    generation = models.PositiveIntegerField()
    audience = models.CharField(max_length=500)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    task = models.ForeignKey(AgentTask, null=True, blank=True, on_delete=models.PROTECT)
    lease_epoch = models.PositiveIntegerField(null=True, blank=True)
    refresh_family = models.UUIDField(null=True, blank=True)
    replacement = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    source = models.CharField(max_length=40)
    source_event_id = models.CharField(max_length=200)
    resource = models.CharField(max_length=300)
    event_type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    actor_kind = models.CharField(max_length=16, default="human")
    chain_id = models.UUIDField(default=uuid.uuid4)
    chain_depth = models.PositiveIntegerField(default=0)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("source", "source_event_id"), name="agent_event_source_unique")]
        indexes = [models.Index(fields=("resource", "id"), name="agent_event_resource")]


class AgentSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(AgentGrant, on_delete=models.PROTECT, related_name="subscriptions")
    generation = models.PositiveIntegerField()
    resource = models.CharField(max_length=300)
    event_types = models.JSONField(default=list)
    instruction = models.TextField(blank=True, default="")
    cursor = models.PositiveBigIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    allow_agent_events = models.BooleanField(default=False)
    max_chain_depth = models.PositiveIntegerField(default=1)
    limits = models.JSONField(default=dict)
    delivery = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AgentArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(AgentTask, on_delete=models.PROTECT, related_name="artifacts")
    name = models.CharField(max_length=160)
    storage_key = models.CharField(max_length=500, unique=True)
    media_type = models.CharField(max_length=120)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)


class AgentActionProposal(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        APPROVED = "approved", "Approved"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        UNKNOWN = "unknown", "Outcome unknown"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(AgentTask, on_delete=models.PROTECT, related_name="actions")
    generation = models.PositiveIntegerField()
    kind = models.CharField(max_length=80)
    resource = models.CharField(max_length=300)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=160)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_hash = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField()
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    receipt = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("task", "idempotency_key"), name="agent_action_idempotency_unique")]
        indexes = [models.Index(fields=("status", "lease_expires_at"), name="agent_action_lease")]


class AgentAuditEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    grant = models.ForeignKey(AgentGrant, null=True, blank=True, on_delete=models.PROTECT)
    task = models.ForeignKey(AgentTask, null=True, blank=True, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=80)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentEnrollment(models.Model):
    """Owner-bound, expiring reservation; never contains a bearer credential."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(AgentInstallation, on_delete=models.PROTECT, related_name="enrollments")
    idempotency_key = models.CharField(max_length=160)
    resource = models.CharField(max_length=300)
    consent_version = models.CharField(max_length=80)
    nonce = models.CharField(max_length=128)
    auth_conditions = models.CharField(max_length=80)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("installation", "idempotency_key"),
                                               name="agent_enrollment_retry_unique")]


class AgentInvocation(models.Model):
    """One owner mention and its frozen native destination/authority."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(AgentInstallation, on_delete=models.PROTECT, related_name="invocations")
    grant = models.ForeignKey(AgentGrant, on_delete=models.PROTECT, related_name="invocations")
    generation = models.PositiveIntegerField()
    resource = models.CharField(max_length=300)
    source_event_id = models.CharField(max_length=64)
    thread_id = models.CharField(max_length=64)
    owner_public_key = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=24, default="pending", choices=[
        (x, x) for x in ("pending", "replied", "cancelled", "expired")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("installation", "source_event_id"),
                                               name="agent_invocation_source_unique")]
        indexes = [models.Index(fields=("status", "expires_at"), name="agent_invocation_expiry")]


class AgentInvocationReply(models.Model):
    """One durable signed event; uncertain sends retry the same event ID."""

    invocation = models.OneToOneField(AgentInvocation, primary_key=True, on_delete=models.PROTECT,
                                     related_name="reply")
    idempotency_key = models.CharField(max_length=160)
    payload_hash = models.CharField(max_length=64)
    event = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="pending", choices=[
        (x, x) for x in ("pending", "sent", "unknown", "stopped_unknown", "cancelled", "failed")])
    receipt = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


# Django discovers these models through the application models module.
from .oauth_models import AgentOAuthBinding, AgentOAuthCode  # noqa: E402,F401
