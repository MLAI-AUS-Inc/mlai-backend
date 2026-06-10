from django.db import models


class ContentFactoryRunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    BLOCKED = "blocked", "Blocked"
    AWAITING_DELIVERY_MODE = "awaiting_delivery_mode", "Awaiting Delivery Mode"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting Confirmation"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting Approval"
    APPROVAL_REQUIRED = "approval_required", "Approval Required"
    DENIED = "denied", "Denied"


class ContentFactoryStepStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    BLOCKED = "blocked", "Blocked"
    SKIPPED = "skipped", "Skipped"


class ContentFactoryApprovalState(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not Required"
    APPROVAL_REQUIRED = "approval_required", "Approval Required"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
class ContentFactoryRun(models.Model):
    """
    Canonical durable run state for Content Factory harness workflows.
    """

    run_id = models.CharField(max_length=100, unique=True, db_index=True)
    workflow = models.CharField(max_length=50, db_index=True)
    domain = models.CharField(max_length=255, blank=True, default="", db_index=True)
    github_repo = models.CharField(max_length=255, blank=True, default="")
    slack_user_id = models.CharField(max_length=50, blank=True, default="")
    status = models.CharField(
        max_length=40,
        choices=ContentFactoryRunStatus.choices,
        default=ContentFactoryRunStatus.QUEUED,
        db_index=True,
    )
    current_step = models.CharField(max_length=120, blank=True, default="")
    approval_state = models.CharField(
        max_length=30,
        choices=ContentFactoryApprovalState.choices,
        default=ContentFactoryApprovalState.NOT_REQUIRED,
    )
    artifact_root = models.CharField(max_length=500, blank=True, default="")
    step_order = models.JSONField(default=list, blank=True)
    acceptance_summary = models.JSONField(default=dict, blank=True)
    verification_summary = models.JSONField(default=dict, blank=True)
    run_request = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    resume_available = models.BooleanField(default=False)
    # emitted_at of the newest content-factory callback synced into this run.
    # Callback sync paths skip mutation when an incoming event is older, so a
    # late-arriving retry cannot overwrite newer run state. Null until a
    # callback stamped with emitted_at arrives (older content-factory versions
    # do not send the field).
    last_event_emitted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_factory_run"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["workflow", "status"], name="cf_run_workflow_status_idx"),
            models.Index(fields=["domain", "status"], name="cf_run_domain_status_idx"),
            # Covers the hot org-scoped recency queries (recent article drafts,
            # latest runs, discovery runs) which all filter domain + workflow and
            # order by -updated_at. Without this Postgres filters then sorts every
            # matching row for the domain on each bootstrap call.
            models.Index(fields=["domain", "workflow", "-updated_at"], name="cf_run_domain_wf_updated_idx"),
        ]

    def __str__(self):
        return f"{self.run_id} ({self.workflow}/{self.status})"


class ContentFactoryRunStep(models.Model):
    """
    Durable per-step state for a Content Factory run.
    """

    run = models.ForeignKey(
        ContentFactoryRun,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    step_key = models.CharField(max_length=120)
    display_order = models.IntegerField(default=0)
    required = models.BooleanField(default=True)
    status = models.CharField(
        max_length=20,
        choices=ContentFactoryStepStatus.choices,
        default=ContentFactoryStepStatus.PENDING,
        db_index=True,
    )
    attempts = models.IntegerField(default=0)
    message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    error = models.TextField(blank=True, default="")
    latest_attempt_path = models.CharField(max_length=500, blank=True, default="")
    artifacts = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "content_factory_run_step"
        unique_together = ["run", "step_key"]
        ordering = ["display_order", "id"]
        indexes = [
            models.Index(fields=["run", "display_order"], name="cf_step_run_order_idx"),
            models.Index(fields=["run", "status"], name="cf_step_run_status_idx"),
        ]

    def __str__(self):
        return f"{self.run.run_id}:{self.step_key} ({self.status})"


class ContentFactoryRunStepAttempt(models.Model):
    """
    Immutable attempt records for a run step.
    """

    step = models.ForeignKey(
        ContentFactoryRunStep,
        on_delete=models.CASCADE,
        related_name="attempt_history",
    )
    attempt = models.IntegerField()
    status = models.CharField(
        max_length=20,
        choices=ContentFactoryStepStatus.choices,
        default=ContentFactoryStepStatus.PENDING,
    )
    message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    artifacts = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    input_path = models.CharField(max_length=500, blank=True, default="")
    output_path = models.CharField(max_length=500, blank=True, default="")
    notes_path = models.CharField(max_length=500, blank=True, default="")
    status_path = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_factory_run_step_attempt"
        unique_together = ["step", "attempt"]
        ordering = ["step_id", "attempt"]

    def __str__(self):
        return f"{self.step.run.run_id}:{self.step.step_key}:attempt-{self.attempt}"
