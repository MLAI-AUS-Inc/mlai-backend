"""Staff verification of an article's current safety sentences (plan step 8).

A verification, not a waiver. The actor and organization always come from the
authenticated staff session, never from the request. Content Factory rechecks
every held value under its run lock and only writes the safety receipt; the
normal quality recheck still has to pass. The founder hears "all sorted" once,
and only after the same preview and generation actually pass.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import re

from django.db import transaction

from workflow_runs.models import ContentFactoryRun
from .staff_alert_rules import mapping

RECEIPT_KEY = "staff_safety_verification_v1"
MAX_CLAIMS = 50
MIN_NOTE_CHARS = 20
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Content Factory's refusal reasons, shown to staff only. Anything else is "unknown".
CF_REFUSALS = frozenset({
    "actor_required", "run_not_held", "observe", "locale_unsupported", "no_safety_sentences",
    "medication_or_dosing", "claims_required", "claim_invalid", "duplicate_sentence", "stale_sentence",
    "source_not_allowed", "note_required", "partial_cover",
})


def held_for_safety(quality) -> bool:
    quality = mapping(quality)
    return (quality.get("status") == "blocking_findings" and quality.get("self_heal_exhausted") is True
            and quality.get("escalation") == "safety_review")


def _claims(raw):
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_CLAIMS:
        return None
    claims = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        identity, url = item.get("sentence_sha256"), item.get("source_url")
        note = " ".join(str(item.get("note") or "").split())
        if (not isinstance(identity, str) or not _SHA256.fullmatch(identity) or not isinstance(url, str)
                or not url.startswith("https://") or len(url) > 500 or not MIN_NOTE_CHARS <= len(note) <= 500):
            return None
        claims.append({"sentence_sha256": identity, "source_url": url, "note": note})
    return claims


def _remote_refusal(remote):
    detail = mapping(mapping(remote.get("content_factory_response")).get("detail"))
    reason = detail.get("code")
    return reason if reason in CF_REFUSALS else "unknown"


def verify_safety_claims(run, org, user, data, *, now, command_key):
    """Ask Content Factory to record a staff verification bound to the held state shown to staff."""
    from .staff_console import quality_blocker, recheck_acknowledged
    from .vibe_marketing_views import STAFF_ACTION_CAPABILITY, _call_content_factory_run_action
    result = mapping(run.result)
    quality = mapping(result.get("article_preview_quality"))
    if not held_for_safety(quality):
        return {"status": "refused", "detail": "This article is not held for a safety review."}
    held = mapping(data.get("held"))
    if (held.get("preview_url") != quality.get("preview_url")
            or held.get("resume_generation") != quality.get("resume_generation")
            or held.get("self_heal_fingerprint") != quality.get("self_heal_fingerprint")
            or not isinstance(held.get("safety_hash"), str) or not _SHA256.fullmatch(held["safety_hash"])):
        return {"status": "observe", "detail": "The article changed since it was shown. Reload and review it again."}
    claims = _claims(data.get("claims"))
    if claims is None:
        return {"status": "refused", "detail": "Each safety sentence needs its hash, an official HTTPS source and a note."}
    if not command_key:
        return {"status": "refused", "detail": "An idempotency key is required."}
    request_key = "staff-safety:" + hashlib.sha256(f"{org.pk}:{run.run_id}:{command_key}".encode()).hexdigest()[:40]
    email = str(getattr(user, "email", "") or "").strip().lower()
    payload = {
        "request_key": request_key,
        "revision": quality.get("resume_generation"),
        # Bound from the authenticated staff session only.
        "actor": {"user_id": str(user.pk), "organization_id": str(org.pk),
                  "email_sha256": hashlib.sha256(email.encode()).hexdigest() if email else ""},
        "held": {key: held[key] for key in ("preview_url", "resume_generation", "self_heal_fingerprint", "safety_hash")},
        "claims": claims,
    }
    remote = _call_content_factory_run_action(run_id=run.run_id, action="record-staff-safety-verification",
                                              payload=payload, workflow=run.workflow,
                                              staff_capability=STAFF_ACTION_CAPABILITY)
    remote = remote if isinstance(remote, dict) else {}
    if remote.get("error") or remote.get("status") not in {"recorded", "already_recorded"}:
        refusal = _remote_refusal(remote)
        return {"status": "observe" if refusal == "observe" else "refused", "refusal": refusal,
                "detail": "Content Factory did not record the verification."}
    audit = {"request_key": request_key, "claims": claims}
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        latest = dict(mapping(current.result))
        existing = mapping(latest.get(RECEIPT_KEY))
        if existing.get("request_key") != request_key:
            latest[RECEIPT_KEY] = {
                "request_key": request_key, "by_user_id": user.pk, "organization_id": org.pk,
                "preview_url": quality.get("preview_url"), "resume_generation": quality.get("resume_generation"),
                "recorded_at": now.isoformat(), "claims": len(claims), "notified_at": None,
            }
        recheck = remote.get("recheck")
        unchanged = mapping(latest.get("article_preview_quality")) == quality
        if unchanged and recheck_acknowledged(recheck, quality):
            latest["article_preview_quality"] = recheck["article_preview_quality"]
            if quality_blocker(latest.get("approval_blocker")):
                latest.pop("approval_blocker", None)
        ContentFactoryRun.objects.filter(pk=current.pk).update(result=latest)
    return {"status": "verified", "action": "verify_safety_claims", "recheck_queued": bool(
        unchanged and recheck_acknowledged(recheck, quality)), "audit": audit}


def _reviewed_at(quality):
    raw = quality.get("reviewed_at") or quality.get("finished_at") or quality.get("checked_at")
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else None


def _ready_to_notify(run, receipt):
    from .article_publish_approval import article_review_identity
    result = mapping(run.result)
    quality = mapping(result.get("article_preview_quality"))
    identity = article_review_identity(run)
    try:
        recorded = datetime.fromisoformat(str(receipt.get("recorded_at")))
    except ValueError:
        return False
    reviewed = _reviewed_at(quality)
    return bool(
        receipt.get("request_key") and not receipt.get("notified_at")
        and run.status not in {"cancelled", "canceled"}
        and quality.get("status") == "passed"
        and quality.get("preview_url") == receipt.get("preview_url") == identity["preview_url"]
        and quality.get("resume_generation") == receipt.get("resume_generation") == identity["resume_generation"]
        # A stale callback from before the verification never counts as its pass.
        and reviewed is not None and recorded.tzinfo is not None and reviewed >= recorded
    )


def notify_after_staff_safety_verification(run):
    """Send the existing team_resolved notice once, after the verified preview actually passes."""
    receipt = mapping(mapping(getattr(run, "result", None)).get(RECEIPT_KEY))
    if not receipt or not _ready_to_notify(run, receipt):
        return 0
    from django.contrib.auth import get_user_model
    from organizations.models import Organization
    from .founder_transitions import notify_team_resolved
    from django.utils import timezone
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=run.organization_id)
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        latest = dict(mapping(current.result))
        receipt = mapping(latest.get(RECEIPT_KEY))
        if not receipt or not _ready_to_notify(current, receipt):
            return 0
        staff = get_user_model().objects.filter(pk=receipt.get("by_user_id")).first()
        item = f"article:{current.run_id}:safety:{receipt['request_key']}:{receipt['resume_generation']}"
        count = notify_team_resolved(org, item, by_user=staff) if staff is not None else 0
        latest[RECEIPT_KEY] = {**receipt, "notified_at": timezone.now().isoformat(), "notified": count}
        ContentFactoryRun.objects.filter(pk=current.pk).update(result=latest)
    return count
