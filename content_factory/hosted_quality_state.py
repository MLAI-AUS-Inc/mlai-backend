"""Founder presentation of worker-owned hosted article quality state."""

SAFETY_MESSAGE = "Our team is checking the safety guidance in this article before it can be published. You don't need to do anything yet."
FALLBACK_MESSAGE = "We couldn't finish the final check automatically. Our team has been notified."
# Used when staff alerts are off: nothing would actually tell the team.
UNALERTED_FALLBACK_MESSAGE = "We couldn't finish the final check automatically. Check again, or contact us if it keeps happening."
FAILED_TO_RUN_MESSAGE = "The final check didn't finish. Check again to unlock publishing."
FAILED_TO_RUN_STATUSES = frozenset({"review_error", "queue_failed"})

# Founders see the blocker's code and founder copy only. The technical
# ``message``, findings and repair instructions are staff diagnostics.
FOUNDER_BLOCKER_KEYS = ("code", "founder_message", "retryable", "retry_action", "owner", "run_id")
FOUNDER_QUALITY_KEYS = (
    "status", "escalation", "self_heal_exhausted", "self_heal_attempt", "founder_message", "blocker_code",
    "preview_url", "previewUrl", "resume_generation", "retryable", "advisory", "quality_attempt",
    "queued_at", "started_at", "finished_at", "checked_at", "reviewed_preview_commit_sha",
)
# Finding markers founder copy is chosen from; every other finding stays internal.
FOUNDER_FINDING_MARKERS = frozenset({"editorial:score_missing"})


def fallback_message():
    from django.conf import settings
    alerts = bool(getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False))
    return FALLBACK_MESSAGE if alerts else UNALERTED_FALLBACK_MESSAGE


def _founder_copy(message):
    """Founder copy, never claiming a staff notification that alerts would not send."""
    if not isinstance(message, str) or not message.strip() or message == FALLBACK_MESSAGE:
        return fallback_message()
    return message


def founder_blocker(blocker):
    """Project an approval blocker to its code and founder copy."""
    if not isinstance(blocker, dict):
        return blocker
    view = {key: blocker[key] for key in FOUNDER_BLOCKER_KEYS if key in blocker}
    # A blocker without founder copy must not fall back to the staff message.
    view["founder_message"] = _founder_copy(blocker.get("founder_message"))
    return view


def founder_quality(quality):
    """Project hosted quality state to the fields founder surfaces read."""
    if not isinstance(quality, dict):
        return quality
    view = {key: quality[key] for key in FOUNDER_QUALITY_KEYS if key in quality}
    if view.get("founder_message") is not None:
        view["founder_message"] = _founder_copy(view["founder_message"])
    findings = quality.get("findings")
    if isinstance(findings, list):
        view["findings"] = [item for item in findings if item in FOUNDER_FINDING_MARKERS]
    return view


def founder_result(result):
    """Apply the founder projections wherever a run result carries quality state."""
    if not isinstance(result, dict):
        return result
    view = dict(result)
    if "approval_blocker" in view:
        view["approval_blocker"] = founder_blocker(view["approval_blocker"])
    for key in ("article_preview_quality", "articlePreviewQuality"):
        if key in view:
            view[key] = founder_quality(view[key])
    for key in ("result", "latest_control_response"):
        if isinstance(view.get(key), dict):
            view[key] = founder_result(view[key])
    return view


def preview_quality_publish_notice(quality):
    """Return a locked/blocked publish state without projecting raw editorial findings."""
    quality = quality if isinstance(quality, dict) else {}
    status = str(quality.get("status") or "").strip().lower()
    if status == "self_healing":
        return "locked", "We're fixing a few things in the preview automatically."
    if status in {"queued", "running", "transient_findings"}:
        return "locked", "Final checks are running. Publishing unlocks when they finish."
    if status == "blocking_findings":
        if quality.get("escalation") == "safety_review":
            return "blocked", SAFETY_MESSAGE
        return "blocked", str(quality.get("founder_message") or fallback_message())
    if status in FAILED_TO_RUN_STATUSES:
        # A required check that crashed or never queued has not passed.
        return "blocked", FAILED_TO_RUN_MESSAGE
    return None
