"""Admission for observing new scheduler refreshes, never paid island research."""
from django.db.models import Q

from .services.island_refresh_domain import validate_refresh_domain

RECOVERY_KEY = "island_refresh_recovery_version"
REQUEST_FIELDS = frozenset({"run_id", "workflow", "domain", "request_source",
                            "include_expansion", RECOVERY_KEY, "client_request_id"})


def nightly_refresh_candidates(since):
    """Narrow the database cohort before the bounded, strict request check."""
    return Q(workflow="island_refresh", created_at__gte=since,
             run_request__island_refresh_recovery_version=1,
             run_request__request_source="mlai_backend_scheduler")


def admitted_nightly_refresh(run):
    """Require the original versioned organization-only request and identity.

    Closed request fields keep unknown website, paid-research and goal authority
    out even when present with a null value. New producer fields require review.
    """
    request = run.run_request
    if (run.workflow != "island_refresh" or run.organization_id is None or run.github_repo or not isinstance(request, dict)
            or not set(request) <= REQUEST_FIELDS
            or type(request.get(RECOVERY_KEY)) is not int or request[RECOVERY_KEY] != 1
            or request.get("workflow") != "island_refresh"
            or request.get("request_source") != "mlai_backend_scheduler"
            or request.get("run_id") != run.run_id or request.get("domain") != run.domain
            or type(request.get("include_expansion")) is not bool):
        return False
    try:
        return validate_refresh_domain(request["domain"]) == request["domain"]
    except ValueError:
        return False


def preview_nightly_refresh_reconciliation(*, created_since, limit=10, now=None):
    """Inspect at most 100 candidates without providers, writes or other repairs."""
    from django.utils import timezone
    from .reconciliation import _stale_run_candidates, discovery_reconciliation_since

    since = discovery_reconciliation_since(created_since)
    if since is None or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("An aware ISO creation boundary and a limit from 1 to 100 are required.")
    candidates = _stale_run_candidates(now or timezone.now()).filter(nightly_refresh_candidates(since))
    inspected = list(candidates[:limit])
    selected = [run for run in inspected if admitted_nightly_refresh(run)]
    return {"mode": "read_only_candidate_preview", "created_since": since.isoformat(), "limit": limit,
            "candidate_rows": candidates.count(), "inspected": len(inspected), "selected": len(selected),
            "refused_scope": len(inspected) - len(selected), "remote_actions": "not_probed"}
