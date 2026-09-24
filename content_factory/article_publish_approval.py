"""Durable founder approval identity for article publish handoff retries."""

from __future__ import annotations

from datetime import datetime, timezone
import re


RECEIPT_KEY = "article_publish_approval_receipt"
ACCEPTED_QUALITY_STATUSES = {"passed", "passed_no_baseline", "advisory_findings"}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _quality_status(run):
    result = _mapping(getattr(run, "result", None))
    quality = _mapping(result.get("article_preview_quality"))
    return str(quality.get("status") or "").strip().lower()


def _first_present(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _generation(value):
    """Normalize the hosted attempt number without accepting booleans or fractions."""
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def article_review_identity(run):
    """Identify the exact hosted review represented by a local article run."""
    result = _mapping(getattr(run, "result", None))
    live = _mapping(result.get("livePreview") or result.get("live_preview"))
    proof = _mapping(live.get("proof"))
    quality = _mapping(result.get("article_preview_quality"))
    live_generation = _first_present(live, "resume_generation", "resumeGeneration", "attempt_number", "attemptNumber")
    if live_generation is None:
        live_generation = _first_present(result, "resume_generation", "resumeGeneration")
    return {
        "run_id": str(getattr(run, "run_id", "") or "").strip(),
        "run_generation": _generation(result.get("generation")),
        "preview_url": str(
            live.get("previewUrl")
            or live.get("preview_url")
            or ""
        ).strip(),
        "commit_sha": str(
            proof.get("commitSha")
            or proof.get("commit_sha")
            or ""
        ).strip(),
        "resume_generation": _generation(0 if live_generation is None else live_generation),
        "quality_inputs_sha256": str(quality.get("inputs_sha256") or "").strip(),
    }


def article_review_identity_is_complete(run):
    """Require the exact hosted render and current quality for this attempt."""
    identity = article_review_identity(run)
    if not identity["run_id"] or not identity["preview_url"]:
        return False
    result = _mapping(getattr(run, "result", None))
    live = _mapping(result.get("livePreview") or result.get("live_preview"))
    quality = _mapping(result.get("article_preview_quality"))
    if "generation" in result and identity["run_generation"] is None:
        return False
    if _first_present(live, "exactRender", "exact_render") is not True:
        return False
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", identity["commit_sha"]):
        return False
    if _quality_status(run) not in ACCEPTED_QUALITY_STATUSES:
        return False
    quality_url = str(quality.get("preview_url") or quality.get("previewUrl") or "").strip()
    quality_generation = _generation(_first_present(quality, "resume_generation", "resumeGeneration"))
    if (
        not quality_url
        or quality_url != identity["preview_url"]
        or identity["resume_generation"] is None
        or quality_generation is None
        or quality_generation != identity["resume_generation"]
    ):
        return False
    quality_hash = identity["quality_inputs_sha256"]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", quality_hash):
        return False
    return True


def make_article_publish_approval_receipt(run, *, actor_id):
    """Record an explicit successful approve action, not preview readiness alone."""
    identity = article_review_identity(run)
    if not article_review_identity_is_complete(run) or not actor_id:
        return None
    return {
        **identity,
        "action": "approve",
        "actor_id": str(actor_id),
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }


def article_publish_approval_receipt_matches(run):
    """A later promote retry may use only the same run and hosted review."""
    if str(getattr(run, "approval_state", "") or "").strip() != "approved":
        return False
    if _quality_status(run) not in ACCEPTED_QUALITY_STATUSES:
        return False
    request = _mapping(getattr(run, "run_request", None))
    receipt = _mapping(request.get(RECEIPT_KEY))
    if receipt.get("action") != "approve" or not receipt.get("actor_id") or not receipt.get("approved_at"):
        return False
    identity = article_review_identity(run)
    if not article_review_identity_is_complete(run):
        return False
    return all(receipt.get(key) == value for key, value in identity.items())
