"""Durable draft completion signal; notification delivery is separately gated."""
from django.db import transaction
from django.utils import timezone

from startup_updates.models import MonthlyUpdateDraft
from startup_updates.services import STARTUP_UPDATE_WORKFLOW
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


COMPLETION_KEY = "startup_update_completion"


@transaction.atomic
def record_completion(run):
    """Record one privacy-safe completion receipt after the generated draft exists.

    A future attested push-gateway consumer can use eventId as its dedupe key.
    This receipt is not a claim that a push was sent or queued for delivery.
    """
    if run.workflow != STARTUP_UPDATE_WORKFLOW or run.status != ContentFactoryRunStatus.COMPLETED:
        return None
    locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
    if locked.status != ContentFactoryRunStatus.COMPLETED:
        return None
    result = dict(locked.result or {})
    if result.get(COMPLETION_KEY):
        return result[COMPLETION_KEY]
    draft = MonthlyUpdateDraft.objects.filter(run=locked).order_by("-updated_at", "-pk").first()
    if draft is None or not draft.current_revision_id:
        return None
    receipt = {
        "eventId": f"startup-update-ready:{locked.run_id}",
        "type": "startup_update_ready", "runId": locked.run_id,
        "updateId": draft.pk, "revisionId": draft.current_revision_id,
        "completedAt": timezone.now().isoformat(),
        "deliveryState": "unavailable",
    }
    result[COMPLETION_KEY] = receipt
    locked.result = result
    locked.save(update_fields=["result", "updated_at"])
    return receipt
