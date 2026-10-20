"""Confirm worker cancellation before settling a founder's paid research run."""
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response

from workflow_runs.models import ContentFactoryRun, ContentFactoryStepStatus
from .island_research import refund_empty_or_failed_research


TERMINAL = {"completed", "failed", "blocked", "denied", "cancelled"}


def is_island_research_run(run):
    """Distinguish paid brief-led research from scheduled island refreshes."""
    return run.workflow == "island_refresh" and bool((run.run_request or {}).get("island_research_brief"))


def cancel_island_research(*, run, context, payload):
    """Stop the owned worker run, retaining its brief and idempotent refund."""
    from . import vibe_marketing_views as views

    if (run.run_request or {}).get("dispatch_pending_resolution"):
        return Response({"detail": "Research dispatch is still being confirmed. Please try cancelling again shortly."}, status=409)
    if run.status in TERMINAL:
        refund_empty_or_failed_research(run)
        return Response(views._serialize_run(run, context=context))

    remote = views._call_content_factory_run_action(
        run_id=run.run_id, action="cancel", payload=payload, workflow=run.workflow, timeout=(3, 15),
    )
    remote_status = remote.get("status")
    remote_id = remote.get("run_id") or remote.get("job_id")
    if (remote.get("error") or remote_status not in TERMINAL or remote_id != run.run_id
            or remote.get("workflow", run.workflow) != run.workflow):
        return Response({"detail": remote.get("error") or "Worker cancellation could not be confirmed. Research status will keep updating.",
                         "retryable": True}, status=409 if remote.get("content_factory_status_code") == 409 else 502)

    if remote_status != "cancelled":
        # Completion can win the race with cancellation. Keep the measured result.
        run = views._sync_local_run_from_remote(run, remote)
    else:
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if run.status not in TERMINAL:
                now = timezone.now()
                run.status, run.current_step = "cancelled", "cancelled"
                run.resume_available, run.error = False, ""
                run.approval_state = "not_required"
                run.result = {**(run.result or {}), "status": "cancelled", "cancelled": True,
                              "cancelled_at": now.isoformat(), "message": "Research cancelled.",
                              "island_research": True}
                run.steps.exclude(status=ContentFactoryStepStatus.COMPLETED).update(
                    status=ContentFactoryStepStatus.CANCELLED, message="Cancelled by user.", error="", completed_at=now,
                )
                run.save(update_fields=["status", "current_step", "resume_available", "error", "approval_state", "result", "updated_at"])
    refund_empty_or_failed_research(run)
    return Response(views._serialize_run(run, context=context))
