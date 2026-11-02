"""Fenced execution and safe recovery using existing locked run JSON."""

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from startup_updates.revisions import RevisionConflict


def validate_execution(run, token):
    """Reject expired workers, including legacy writes once a run is fenced."""
    current = (run.result or {}).get("_pulse_execution", {}).get("token")
    if current is not None and str(token or "") != str(current):
        raise RevisionConflict("This worker execution was superseded.")


def claim_execution(run, *, owner, resume=False):
    """Advance an execution token under the caller's run-row transaction lock."""
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
        raise ValidationError({"owner": "A worker identity is required."})
    if run.status in {"completed", "cancelled", "denied"}:
        raise RevisionConflict("This run cannot resume.")
    if run.status in {"failed", "blocked"}:
        if not resume or not run.resume_available:
            raise RevisionConflict("Explicit safe recovery is required.")
        from startup_updates.models import MonthlyUpdateDraft

        request = run.run_request or {}
        saved = request.get("saved_revisions") or {}
        if request.get("update_id") and not saved:
            draft = MonthlyUpdateDraft.objects.filter(
                pk=request["update_id"], organization_id=run.organization_id
            ).first()
            if draft is None or draft.current_revision_id != request.get(
                "base_revision"
            ):
                raise RevisionConflict(
                    "The draft changed; recovery cannot overwrite it."
                )
        for month, pin in saved.items():
            if not MonthlyUpdateDraft.objects.filter(
                organization_id=run.organization_id,
                month=month,
                current_revision_id=pin["revision_id"],
            ).exists():
                raise RevisionConflict(
                    "The saved draft changed; recovery cannot overwrite it."
                )
        run.status, run.error, run.resume_available = "queued", "", False
    result = dict(run.result or {})
    previous = result.get("_pulse_execution") or {}
    token = int(previous.get("token", 0)) + 1
    result["_pulse_execution"] = {
        "token": token,
        "owner": owner,
        "claimed_at": timezone.now().isoformat(),
    }
    meta = dict(result.get("_valley_meta") or {})
    if resume:
        automatic = owner == "dead-letter-recovery"
        count = int(meta.get("automatic_recovery_count", 0))
        if automatic and count >= 5:
            raise RevisionConflict(
                "Automatic recovery reached its limit. Review this saved run before resuming."
            )
        if automatic:
            meta["automatic_recovery_count"] = count + 1
        meta["dead_letters"] = [
            {**item, "resolved": True} for item in meta.get("dead_letters", [])
        ]
        meta.update(
            retry_count=0, last_error="", lease_owner=None, lease_expires_at=None
        )
        result.pop("source_preflight", None)
        # Existing completed collection and pinned evidence remain authoritative.
        run.run_request = {**(run.run_request or {}), "source_preflight_version": 1}
    result["_valley_meta"] = meta
    run.result = result
    run.save(
        update_fields=[
            "result",
            "status",
            "error",
            "resume_available",
            "run_request",
            "updated_at",
        ]
    )
    return {"token": token, "run_id": run.run_id, "status": run.status}
