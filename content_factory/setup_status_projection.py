"""Pure presentation of terminal setup revisions over stale preview phases."""
from copy import copy, deepcopy

ACTIVE_PHASES = frozenset({"revision_preview_building", "preview_building", "repair_preview_building", "preview_verifying"})


def terminal_setup_projection(run):
    """Expose a saved setup failure without rewriting durable run history."""
    if run.workflow != "article_system_setup" or run.status not in {"blocked", "failed", "cancelled", "denied"}:
        return run
    result = run.result if isinstance(run.result, dict) else {}
    setup = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    if run.current_step not in ACTIVE_PHASES and result.get("status") not in ACTIVE_PHASES and setup.get("status") not in ACTIVE_PHASES:
        return run
    projected = copy(run)
    projected.result = deepcopy(result)
    if run.current_step in ACTIVE_PHASES:
        projected.current_step = result.get("failed_step") or setup.get("failed_step") or run.status
    if result.get("status") in ACTIVE_PHASES:
        projected.result["status"] = run.status
    if setup.get("status") in ACTIVE_PHASES:
        projected.result["article_system_setup"]["status"] = run.status
    return projected
