"""Read current admitted execution authority without adding a recovery writer."""
from datetime import timedelta

def event_owned(run):
    """An existing event owner keeps authority when new-admission flags change."""
    result = getattr(run, "result", None)
    if not isinstance(result, dict) or "run_owner_v1" not in result:
        return False
    from .run_ownership import owner_receipt
    return (owner_receipt(run) or {}).get("authority", {}).get("event_protocol") == 1


def execution_request(run):
    """Read original inputs with only the backend-issued current scope applied."""
    if event_owned(run):
        from .run_execution_control import current_execution_request
        return current_execution_request(run)
    return getattr(run, "run_request", None) or {}


def owned_goal_child(op, *, include_completed=True):
    """Find a bounded saved child; a legacy goal cannot repair its execution."""
    from workflow_runs.models import ContentFactoryRun
    receipt = op.receipt or {}
    identifiers = list(dict.fromkeys(value for key in (
        "first_article_publish_run_id", "first_article_run_id", "child_run_id",
        "setup_run_id", "scan_run_id", "hosted_verification_run_id")
        if isinstance(value := receipt.get(key), str) and value))
    if not identifiers:
        return None
    runs = {run.run_id: run for run in ContentFactoryRun.objects.filter(
        organization_id=op.connection.organization_id, run_id__in=identifiers,
        result__run_owner_v1__authority__event_protocol=1)}
    return next((runs[key] for key in identifiers if key in runs and event_owned(runs[key])
        and (include_completed or runs[key].status != "completed")), None)


def wait_for_execution(op, run, *, now, source_changed=False):
    """Preserve the child identity and expose its owning controller to the user."""
    stopped = run.status in {"completed", "failed", "blocked", "cancelled", "denied"}
    op.state = "needs_user" if stopped else "pending"
    op.next_attempt_at = None if stopped else now + timedelta(minutes=1)
    op.receipt = {**op.receipt, "status": "needs_you" if stopped else "working",
        "reason_code": "run_execution_owned", "retryable": False,
        "message": ("Review the saved run before continuing this goal." if stopped else
            "Waiting for the saved run to verify the current website source." if source_changed else
            "Waiting for the saved run to report its outcome."),
        "user_action": {"id": "open_run", "label": "Open run", "run_id": run.run_id} if stopped else None}
    op._goal_wait = True
