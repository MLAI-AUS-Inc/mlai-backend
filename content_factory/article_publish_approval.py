"""Durable founder approval identity for article publish handoff retries."""

from __future__ import annotations

from datetime import datetime, timezone
import re


RECEIPT_KEY = "article_publish_approval_receipt"
RECEIPT_REQUIRED_KEY = "article_publish_approval_receipt_required"
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


STAND_IN_FLAG = "ARTICLES_STAND_IN_PREVIEW_APPROVAL_ENABLED"
APPROVAL_BASIS_STAND_IN = "stand_in_preview"
APPROVAL_BASIS_ARTICLE_TEXT = "article_text"
STAND_IN_RUNGS = frozenset({"snapshot", "template"})


def _stand_in_approval_enabled():
    from django.conf import settings
    return bool(getattr(settings, STAND_IN_FLAG, True))


def approval_basis(run):
    """The non-exact basis Content Factory offered for approval, or "".

    Content Factory approves a website-bound article whose own site build
    could not be previewed from its stand-in preview (a snapshot or template
    rendering of the site, still reviewed in a browser) or, with no rendering
    at all, from its text. It re-checks the basis on approve; this only lets
    the founder's approval reach it. ``ARTICLES_STAND_IN_PREVIEW_APPROVAL_ENABLED``
    (default on) is the kill switch.
    """
    if not _stand_in_approval_enabled():
        return ""
    result = _mapping(getattr(run, "result", None))
    basis = str(result.get("approval_basis") or "").strip()
    if basis == APPROVAL_BASIS_STAND_IN:
        live = _mapping(result.get("livePreview") or result.get("live_preview"))
        proof = _mapping(live.get("proof"))
        if proof.get("repoBoundSnapshot") is True and str(proof.get("previewRung") or "") in STAND_IN_RUNGS:
            return basis
        return ""
    if basis == APPROVAL_BASIS_ARTICLE_TEXT:
        return basis
    return ""


def article_review_identity_is_complete(run):
    """Require the exact hosted render (or its stand-in, or the article text) and current quality."""
    from .run_reconciliation import authoritative_run
    if authoritative_run(run):
        from .run_review_quality import current_review_verified
        if not current_review_verified(run):
            return False
    identity = article_review_identity(run)
    basis = approval_basis(run)
    result = _mapping(getattr(run, "result", None))
    if "generation" in result and identity["run_generation"] is None:
        return False
    if basis == APPROVAL_BASIS_ARTICLE_TEXT:
        # No rendering of the site could be produced; Content Factory checks
        # the saved article itself before it opens the pull request.
        return bool(identity["run_id"])
    if not identity["run_id"] or not identity["preview_url"]:
        return False
    live = _mapping(result.get("livePreview") or result.get("live_preview"))
    quality = _mapping(result.get("article_preview_quality"))
    if _first_present(live, "exactRender", "exact_render") is not True and basis != APPROVAL_BASIS_STAND_IN:
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


def article_review_identity_matches_approved_run(run, expected_identity):
    """Check the saved postapprove run against the review sent for approval."""
    return bool(
        str(getattr(run, "approval_state", "") or "").strip() == "approved"
        and article_review_identity_is_complete(run)
        and article_review_identity(run) == expected_identity
    )


def article_publish_approval_receipt_matches(run):
    """A later promote retry may use only the same run and hosted review."""
    from .run_ownership import user_stop_intent
    if user_stop_intent(run):
        return False
    from .run_reconciliation import authoritative_run
    if not authoritative_run(run) and str(getattr(run, "approval_state", "") or "").strip() != "approved":
        return False
    if _quality_status(run) not in ACCEPTED_QUALITY_STATUSES and approval_basis(run) != APPROVAL_BASIS_ARTICLE_TEXT:
        return False
    receipt = article_publish_approval_receipt(run)
    if receipt.get("action") != "approve" or not receipt.get("actor_id") or not receipt.get("approved_at"):
        return False
    identity = article_review_identity(run)
    if not article_review_identity_is_complete(run):
        return False
    return all(receipt.get(key) == value for key, value in identity.items())


def article_publish_approval_state(run):
    """Read backend command consent without changing original admission bytes."""
    from .run_ownership import (ARTICLE_PUBLISH_APPROVAL_RESULT_KEY, current_execution_ticket,
                               owner_receipt, wire_identity)
    from .website_contract import evidence_digest
    owner = owner_receipt(run)
    if owner is None:
        return _mapping(getattr(run, "run_request", None))
    state = _mapping(_mapping(getattr(run, "result", None)).get(ARTICLE_PUBLISH_APPROVAL_RESULT_KEY))
    try:
        if (state.get("schema_version") != 1
                or state.get("admission_digest") != evidence_digest(wire_identity(owner))
                or state.get("execution") != current_execution_ticket(run)):
            return {}
        if RECEIPT_KEY in state:
            from .run_review_quality import current_review_verified
            export = _mapping(_mapping(getattr(run, "result", None)).get("articleExport"))
            import hashlib
            body = str(export.get("markdown") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if (not current_review_verified(run) or state.get("review_revision") != export.get("revision")
                    or state.get("article_sha256") != hashlib.sha256(body.encode()).hexdigest()
                    or re.fullmatch(r"[a-f0-9]{64}", str(state.get("accepted_control_sha256") or "")) is None):
                return {}
    except (TypeError, ValueError):
        return {}
    return state


def article_publish_approval_receipt(run):
    return _mapping(article_publish_approval_state(run).get(RECEIPT_KEY))


def effective_approval_state(run):
    """Expose validated backend parent consent without changing CF lifecycle."""
    from .run_reconciliation import authoritative_run
    if authoritative_run(run) and article_publish_approval_receipt_matches(run):
        return "approved"
    return getattr(run, "approval_state", "")
