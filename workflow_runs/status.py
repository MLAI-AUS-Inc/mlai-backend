"""Wire-status normalisation owned by the workflow domain."""

from .models import ContentFactoryRunStatus, ContentFactoryStepStatus


def normalize_run_status(value):
    normalized = str(value or "").strip().lower()
    mapping = {
        "processing": ContentFactoryRunStatus.RUNNING,
        "in_progress": ContentFactoryRunStatus.RUNNING,
        "blocked_verification": ContentFactoryRunStatus.BLOCKED,
        "precondition_failed": ContentFactoryRunStatus.BLOCKED,
        "preview_failed": ContentFactoryRunStatus.BLOCKED,
        "fallback_ready": ContentFactoryRunStatus.BLOCKED,
        "setup_pr_created": ContentFactoryRunStatus.COMPLETED,
        "pr_created": ContentFactoryRunStatus.COMPLETED,
        "merged": ContentFactoryRunStatus.COMPLETED,
        "merged_verifying": ContentFactoryRunStatus.COMPLETED,
        "error": ContentFactoryRunStatus.FAILED,
    }
    normalized = mapping.get(normalized, normalized)
    allowed = {choice[0] for choice in ContentFactoryRunStatus.choices}
    return normalized if normalized in allowed else ContentFactoryRunStatus.QUEUED


def normalize_step_status(value):
    normalized = str(value or "").strip().lower()
    mapping = {
        "processing": ContentFactoryStepStatus.RUNNING,
        "in_progress": ContentFactoryStepStatus.RUNNING,
        "blocked_verification": ContentFactoryStepStatus.BLOCKED,
        "error": ContentFactoryStepStatus.FAILED,
    }
    normalized = mapping.get(normalized, normalized)
    allowed = {choice[0] for choice in ContentFactoryStepStatus.choices}
    return normalized if normalized in allowed else ContentFactoryStepStatus.PENDING
