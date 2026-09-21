"""Durable deletion work and deadlines; scheduling never implies erasure."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import AccountDeletionRequest, AccountDeletionTask

CHAT_TARGETS = (
    "chat_access", "relay_messages_and_media", "bridge_copies_and_credentials",
    "chat_profile_and_preferences", "backups_and_processor_copies",
)
ACCOUNT_TARGETS = CHAT_TARGETS + ("shared_account_and_dependencies",)


def targets_for(scope):
    """Return the complete required cleanup set for a confirmed request scope."""
    if scope == AccountDeletionRequest.Scope.CHAT:
        return CHAT_TARGETS
    if scope == AccountDeletionRequest.Scope.ACCOUNT:
        return ACCOUNT_TARGETS
    raise ValueError("Unknown account deletion scope")


@transaction.atomic
def schedule_deletion(record):
    """Create each cleanup target once without resetting its original deadline."""
    record = AccountDeletionRequest.objects.select_for_update().get(pk=record.pk)
    if record.status == AccountDeletionRequest.Status.COMPLETED:
        return
    for target in targets_for(record.scope):
        AccountDeletionTask.objects.get_or_create(request=record, target=target)


def deletion_deadline(record):
    """The owner commitment is 30 calendar days from the original request."""
    return record.requested_at + timedelta(days=30)


@transaction.atomic
def claim_task(task_id):
    """Lease one cleanup target, permitting recovery after an interrupted worker."""
    # Always request -> task, matching completion and retry paths.
    snapshot = AccountDeletionTask.objects.get(pk=task_id)
    request = AccountDeletionRequest.objects.select_for_update().get(pk=snapshot.request_id)
    task = AccountDeletionTask.objects.select_for_update().get(pk=task_id)
    now = timezone.now()
    if request.status == AccountDeletionRequest.Status.COMPLETED or task.status == AccountDeletionTask.Status.COMPLETED:
        return None
    if task.next_attempt_at and task.next_attempt_at > now:
        return None
    task.status = AccountDeletionTask.Status.PROCESSING
    task.attempts += 1
    task.started_at = now
    task.next_attempt_at = now + timedelta(minutes=15)
    task.error_code = ""
    task.save()
    request.status = (
        AccountDeletionRequest.Status.NEEDS_ATTENTION
        if request.tasks.exclude(pk=task.pk).filter(status=AccountDeletionTask.Status.NEEDS_ATTENTION).exists()
        else AccountDeletionRequest.Status.PROCESSING
    )
    request.save(update_fields=("status", "updated_at"))
    return task


@transaction.atomic
def fail_task(task_id, *, attempt, error_code):
    """Retry transient cleanup failures without logging content or credentials."""
    if error_code not in {"provider_unavailable", "verification_failed", "operator_review_required"}:
        raise ValueError("Use a bounded deletion failure code")
    snapshot = AccountDeletionTask.objects.get(pk=task_id)
    request = AccountDeletionRequest.objects.select_for_update().get(pk=snapshot.request_id)
    task = AccountDeletionTask.objects.select_for_update().get(pk=task_id)
    if task.status != AccountDeletionTask.Status.PROCESSING or task.attempts != attempt:
        return False
    task.status = AccountDeletionTask.Status.NEEDS_ATTENTION
    task.error_code = error_code
    task.next_attempt_at = timezone.now() + timedelta(minutes=min(2 ** min(task.attempts, 10), 1440))
    task.save()
    request.status = AccountDeletionRequest.Status.NEEDS_ATTENTION
    request.save(update_fields=("status", "updated_at"))
    return True


@transaction.atomic
def complete_task(task_id, *, attempt, verified_counts):
    """Record an executor's verified cleanup counts, rejecting stale workers.

    Only real cleanup executors may call this after read-after-delete checks.
    No admin or client endpoint may set completion. Request-level completion
    remains separate until every required storage boundary is verified.
    """
    if (not isinstance(verified_counts, dict) or not verified_counts
            or len(verified_counts) > 20
            or any(key not in {"deleted_rows", "remaining_rows", "revoked_credentials", "removed_objects", "verified_targets"}
                   or type(value) is not int or not 0 <= value < 2 ** 63 for key, value in verified_counts.items())):
        raise ValueError("Deletion evidence must contain bounded non-sensitive counts")
    if verified_counts.get("remaining_rows", 0) != 0:
        raise ValueError("Cleanup with remaining rows is not complete")
    snapshot = AccountDeletionTask.objects.get(pk=task_id)
    AccountDeletionRequest.objects.select_for_update().get(pk=snapshot.request_id)
    task = AccountDeletionTask.objects.select_for_update().get(pk=task_id)
    if task.status != AccountDeletionTask.Status.PROCESSING or task.attempts != attempt:
        return False
    task.status = AccountDeletionTask.Status.COMPLETED
    task.completed_at = timezone.now()
    task.next_attempt_at = None
    task.error_code = ""
    task.evidence = verified_counts
    task.save()
    return True
