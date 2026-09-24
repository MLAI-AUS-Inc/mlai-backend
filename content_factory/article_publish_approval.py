"""Durable founder approval identity for article publish handoff retries."""

from __future__ import annotations

from datetime import datetime, timezone


RECEIPT_KEY = "article_publish_approval_receipt"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def article_review_identity(run):
    """Identify the exact hosted review represented by a local article run."""
    result = _mapping(getattr(run, "result", None))
    live = _mapping(result.get("livePreview") or result.get("live_preview"))
    proof = _mapping(live.get("proof"))
    quality = _mapping(result.get("article_preview_quality"))
    return {
        "run_id": str(getattr(run, "run_id", "") or "").strip(),
        "preview_url": str(
            live.get("previewUrl")
            or live.get("preview_url")
            or result.get("preview_url")
            or result.get("previewUrl")
            or ""
        ).strip(),
        "commit_sha": str(
            proof.get("commitSha")
            or proof.get("commit_sha")
            or live.get("commitSha")
            or live.get("commit_sha")
            or result.get("branch_commit_sha")
            or ""
        ).strip(),
        "quality_inputs_sha256": str(quality.get("inputs_sha256") or "").strip(),
    }


def make_article_publish_approval_receipt(run, *, actor_id):
    """Record an explicit successful approve action, not preview readiness alone."""
    identity = article_review_identity(run)
    if not identity["run_id"] or not identity["preview_url"] or not actor_id:
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
    request = _mapping(getattr(run, "run_request", None))
    receipt = _mapping(request.get(RECEIPT_KEY))
    if receipt.get("action") != "approve" or not receipt.get("actor_id") or not receipt.get("approved_at"):
        return False
    identity = article_review_identity(run)
    if not identity["run_id"] or not identity["preview_url"]:
        return False
    return all(receipt.get(key) == value for key, value in identity.items())
