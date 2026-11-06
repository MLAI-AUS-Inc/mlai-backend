"""Staff verification of an article's current safety sentences (plan step 8).

A verification, not a waiver. The actor and organization always come from the
authenticated staff session, never from the request. Content Factory rechecks
every held value under its run lock and only writes the safety receipt; the
normal quality recheck still has to pass. The founder hears "all sorted" once,
and only after the same preview and generation actually pass.
"""
from __future__ import annotations

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
    from .staff_console import quality_blocker
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
    recheck = remote.get("recheck")
    task = _fresh_recheck_task(recheck, quality)
    from .article_publish_approval import article_review_identity
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        latest = dict(mapping(current.result))
        identity = article_review_identity(current)
        # Revalidate after the remote call: the organization, the current
        # preview target and the exact held quality and blocker must be the
        # ones staff reviewed. Otherwise nothing is installed here; Content
        # Factory keeps its own audit of what it recorded.
        if (current.organization_id != org.pk
                or identity["preview_url"] != quality.get("preview_url")
                or identity["resume_generation"] != quality.get("resume_generation")
                or mapping(latest.get("article_preview_quality")) != quality
                or latest.get("approval_blocker") != result.get("approval_blocker")):
            return {"status": "observe", "detail": "The article changed while the verification was recorded. "
                    "Reload and review it again.", "recheck_queued": False, "audit": audit}
        if task is None:
            # Recorded, but no fresh recheck was acknowledged, so there is no
            # exact review to wait for and no founder notice is armed.
            return {"status": "verified", "action": "verify_safety_claims", "recheck_queued": False,
                    "notice_armed": False, "audit": audit}
        if mapping(latest.get(RECEIPT_KEY)).get("request_key") != request_key:
            latest[RECEIPT_KEY] = {
                "request_key": request_key, "by_user_id": user.pk, "organization_id": org.pk,
                "preview_url": quality.get("preview_url"), "resume_generation": quality.get("resume_generation"),
                # The founder hears only when this exact acknowledged review passes.
                "recheck_task_id": task["task_id"], "recheck_review_token": task["review_token"],
                "recorded_at": now.isoformat(), "claims": len(claims), "notified_at": None,
            }
        latest["article_preview_quality"] = recheck["article_preview_quality"]
        if quality_blocker(latest.get("approval_blocker")):
            latest.pop("approval_blocker", None)
        ContentFactoryRun.objects.filter(pk=current.pk).update(result=latest)
    return {"status": "verified", "action": "verify_safety_claims", "recheck_queued": True,
            "notice_armed": True, "audit": audit}


def _fresh_recheck_task(recheck, held_quality):
    """The identity of a newly queued recheck, or None when none was positively acknowledged."""
    from .staff_console import recheck_acknowledged
    recheck = mapping(recheck)
    queued = mapping(recheck.get("article_preview_quality"))
    task_id, token = queued.get("task_id"), queued.get("review_token")
    if (not recheck_acknowledged(recheck, held_quality) or recheck.get("idempotent") is not False
            or queued.get("status") != "queued" or not isinstance(task_id, str) or not task_id
            or queued.get("preview_url") != held_quality.get("preview_url")
            or queued.get("resume_generation") != held_quality.get("resume_generation")
            or not isinstance(token, str) or not token or task_id == held_quality.get("task_id")):
        return None
    return {"task_id": task_id, "review_token": token}


def _ready_to_notify(run, receipt):
    from .article_publish_approval import article_review_identity
    result = mapping(run.result)
    quality = mapping(result.get("article_preview_quality"))
    identity = article_review_identity(run)
    task_id, token = receipt.get("recheck_task_id"), receipt.get("recheck_review_token")
    return bool(
        receipt.get("request_key") and not receipt.get("notified_at")
        and run.status not in {"cancelled", "canceled"}
        and quality.get("status") == "passed"
        # Only the exact recheck acknowledged after the verification counts. An
        # older overlapping review of the same preview never does, whenever it lands.
        and isinstance(task_id, str) and task_id and quality.get("task_id") == task_id
        and isinstance(token, str) and token and quality.get("review_token") == token
        and quality.get("preview_url") == receipt.get("preview_url") == identity["preview_url"]
        and quality.get("resume_generation") == receipt.get("resume_generation") == identity["resume_generation"]
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


def _text(value, limit):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def staff_safety_state(organization_id, run_id):
    """What staff verify against: the held binding and the current safety sentences.

    Staff console only (the view requires active staff). The sentences come
    from Content Factory's current draft; the binding must match the quality
    state this backend holds, or staff are told to reload instead of being
    shown a state they could not submit.
    """
    from organizations.models import Organization
    from .article_publish_approval import article_review_identity
    from .vibe_marketing_views import STAFF_ACTION_CAPABILITY, _call_content_factory_run_action
    org = Organization.objects.get(pk=organization_id)
    run = ContentFactoryRun.objects.get(organization=org, run_id=run_id)
    quality = mapping(mapping(run.result).get("article_preview_quality"))
    if not held_for_safety(quality):
        return {"status": "not_held", "runId": run.run_id}
    remote = _call_content_factory_run_action(run_id=run.run_id, action="staff-safety-state", payload={},
                                              workflow=run.workflow, staff_capability=STAFF_ACTION_CAPABILITY)
    remote = remote if isinstance(remote, dict) else {}
    if remote.get("error"):
        return {"status": "unavailable", "runId": run.run_id}
    identity = article_review_identity(run)
    held = {"previewUrl": remote.get("preview_url"), "resumeGeneration": remote.get("resume_generation"),
            "selfHealFingerprint": remote.get("self_heal_fingerprint"), "safetyHash": remote.get("safety_hash")}
    if (remote.get("held") is not True
            or held["previewUrl"] != quality.get("preview_url") or held["previewUrl"] != identity["preview_url"]
            or held["resumeGeneration"] != quality.get("resume_generation")
            or held["resumeGeneration"] != identity["resume_generation"]
            or held["selfHealFingerprint"] != quality.get("self_heal_fingerprint")
            or not isinstance(held["safetyHash"], str) or not _SHA256.fullmatch(held["safetyHash"])):
        return {"status": "observe", "runId": run.run_id}
    sentences = []
    for item in remote.get("sentences") if isinstance(remote.get("sentences"), list) else []:
        item = mapping(item)
        identity_hash, text = item.get("sentence_sha256"), _text(item.get("text"), 2000)
        if not isinstance(identity_hash, str) or not _SHA256.fullmatch(identity_hash) or text is None:
            return {"status": "observe", "runId": run.run_id}
        sentences.append({"sentenceSha256": identity_hash, "text": text,
                          "medicationOrDosing": item.get("medication_or_dosing") is True})
    domains = [domain for domain in remote.get("allowed_source_domains") or [] if _text(domain, 253)][:50]
    locale = remote.get("locale_supported") is True and bool(domains)
    reason = ("locale_unsupported" if not locale else "no_safety_sentences" if not sentences
              else "medication_or_dosing" if any(item["medicationOrDosing"] for item in sentences)
              else "too_many_sentences" if len(sentences) > MAX_CLAIMS else None)
    return {"status": "held", "runId": run.run_id, "held": held, "sentences": sentences,
            "allowedSourceDomains": domains, "verifiable": reason is None, "reason": reason}
