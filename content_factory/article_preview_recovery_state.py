"""Join saved review and deployment evidence without treating stale worker flags as a lease."""

from datetime import timedelta
import re
from django.utils import timezone
from django.utils.dateparse import parse_datetime

TERMINAL = {"denied", "failed", "blocked", "cancelled", "canceled", "deleted", "superseded"}
ACTIVE_QUALITY = {"self_healing", "running", "queued", "transient_findings"}
REASONS = {
    "preview_expired",
    "preview_unavailable",
    "source_changed",
    "destination_changed",
    "access_changed",
    "provider_unavailable",
    "wrong_article",
    "route_missing",
    "lease_expired",
    "revision_changed",
    "build_failed",
    "recovery_exhausted",
    "article_cancelled",
    "pending_validation",
    "preview_verified",
    "queued",
    "building",
    "draft_saved",
    "recovery_disabled",
}


def mapping(value):
    return value if isinstance(value, dict) else {}


def instant(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    return parsed if parsed and timezone.is_aware(parsed) else None


def terminal(run):
    return str(getattr(run, "status", "")).lower() in TERMINAL


def effective(run, snapshot, *, operation=None, refusal=None, now=None):
    now = now or timezone.now()
    snapshot = mapping(snapshot)
    raw = mapping(snapshot.get("previewRecovery"))
    if not all(isinstance(raw.get(k, ""), str) for k in ("reason", "state", "revision", "runId")):
        raw = {}
    revision = snapshot.get("revision")
    revision = revision if isinstance(revision, str) and 0 < len(revision) <= 200 else None
    view = {
        "version": 1,
        "draftRevision": revision,
        "previewType": "draft",
        "previewRevision": None,
        "state": "draft_ready",
        "reason": "draft_saved",
        "actions": ["review_draft"] if revision else [],
        "job": None,
        "pollAfterMs": None,
    }
    if not revision:
        return view
    wire = mapping(mapping(getattr(operation, "payload", None)).get("wire"))
    if wire.get("revision") != revision:
        wire = {}
        operation = None
    matches = raw.get("revision") == revision and raw.get("runId") == run.run_id
    identity = (
        matches
        and isinstance(raw.get("sourceSha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", raw["sourceSha"])
        and isinstance(raw.get("operationId"), str)
        and 0 < len(raw["operationId"]) <= 100
        and type(raw.get("generation")) is int
        and raw["generation"] >= 0
        and type(raw.get("attempt")) is int
        and raw["attempt"] > 0
    )
    bound = bool(
        identity
        and wire
        and raw["operationId"] == wire["operation_id"]
        and raw["generation"] == wire["generation"]
        and raw["attempt"] == wire["attempt"]
        and raw.get("sourceSha") == wire["source_sha"]
    )
    if run.status in {"cancelled", "canceled", "deleted", "superseded"} or refusal == "article_cancelled":
        view["reason"] = "article_cancelled"
        return view
    if refusal in {"destination_changed", "access_changed"}:
        view.update(
            state="destination_required" if refusal == "destination_changed" else "access_required",
            reason=refusal,
        )
        view["actions"].append("choose_destination" if refusal == "destination_changed" else "restore_access")
        return view
    if raw.get("reason") in REASONS and matches:
        view["reason"] = raw["reason"]
    if operation:
        state = operation.receipt.get("status")
        due = instant(wire.get("expires_at"))
        if state in {"queued", "building"} and due and due > now:
            view.update(
                state="building",
                reason=state,
                job={
                    "id": wire["operation_id"],
                    "attempt": wire["attempt"],
                    "generation": wire["generation"],
                    "deadlineAt": wire["expires_at"],
                },
                pollAfterMs=5000,
            )
            return view
        if state != "ready":
            view["reason"] = (
                operation.receipt.get("reason")
                if operation.receipt.get("reason") in REASONS
                else "lease_expired"
            )
            return view
    admissible = bound if operation else matches and not terminal(run)
    valid_until = instant(raw.get("verifiedUntil"))
    if admissible and raw.get("state") == "ready" and valid_until and valid_until > now:
        view.update(
            state="site_ready", reason="preview_verified", previewType="site", previewRevision=revision
        )
    elif (
        (
            not operation
            and (
                not terminal(run)
                or raw.get("state") == "validation_pending"
                and re.fullmatch(r"probe-[a-f0-9]{24}", str(raw.get("operationId") or ""))
            )
            or bound
            and raw.get("state") == "validation_pending"
        )
        and identity
        and raw.get("state") in {"building", "validation_pending"}
    ):
        deadline = instant(raw.get("leaseExpiresAt"))
        if deadline and now < deadline <= now + (
            timedelta(minutes=2) if raw.get("state") == "validation_pending" else timedelta(hours=2)
        ):
            view.update(
                state="building",
                reason="building",
                job={
                    "id": raw["operationId"],
                    "attempt": raw["attempt"],
                    "generation": raw["generation"],
                    "deadlineAt": raw["leaseExpiresAt"],
                },
                pollAfterMs=5000,
            )
    return view


def settle_stale_artifacts(result):
    """Neutralize historical worker activity in founder projections only."""
    if not isinstance(result, dict):
        return result
    view = dict(result)
    for key in ("article_preview_quality", "articlePreviewQuality"):
        quality = mapping(view.get(key))
        if quality.get("status") in ACTIVE_QUALITY:
            view[key] = {"status": "unavailable", "retryable": False}
    for key in ("live_preview", "livePreview"):
        if isinstance(view.get(key), dict):
            view[key] = {
                **view[key],
                "available": False,
                "status": "unavailable",
                "platformStatus": "unavailable",
            }
    for key in ("result", "latest_control_response"):
        if isinstance(view.get(key), dict):
            view[key] = settle_stale_artifacts(view[key])
    return view
