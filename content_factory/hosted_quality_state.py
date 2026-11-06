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


# Content Factory's longest automatic retry delay (30 s) plus its 60 s sync
# grace, with a little clock-skew allowance. A stamp further out is not trusted.
MAX_AUTOMATIC_RETRY_WINDOW_SECONDS = 120


def current_preview_target(result):
    """The proven current preview of a run result: an absolute URL and an integer generation, or None."""
    if not isinstance(result, dict):
        return None
    preview = result.get("live_preview") if isinstance(result.get("live_preview"), dict) else result.get("livePreview")
    if not isinstance(preview, dict):
        return None
    url = preview.get("previewUrl") or preview.get("preview_url")
    generation = preview.get("resumeGeneration", preview.get("resume_generation"))
    if (not isinstance(url, str) or not url.startswith(("https://", "http://"))
            or type(generation) is not int or generation < 0):
        return None
    return url, generation


def automatic_retry_pending(quality, now=None, *, target=None):
    """True while Content Factory's bounded automatic retry of a failed enqueue is due for the current preview.

    It needs a proven current target, a quality record for exactly that
    preview and generation, and a timezone-aware ``automatic_retry_until``
    stamp that is still ahead but within the configured retry window.
    Otherwise the failure is real.
    """
    from datetime import datetime, timedelta, timezone
    if not isinstance(quality, dict) or str(quality.get("status") or "").strip().lower() != "queue_failed":
        return False
    if target is None or (quality.get("preview_url"), quality.get("resume_generation")) != target:
        return False  # Currentness is never inferred from the quality record itself.
    raw = quality.get("automatic_retry_until")
    if not isinstance(raw, str):
        return False
    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now < until <= now + timedelta(seconds=MAX_AUTOMATIC_RETRY_WINDOW_SECONDS)


def fallback_message():
    from django.conf import settings
    alerts = bool(getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False))
    return FALLBACK_MESSAGE if alerts else UNALERTED_FALLBACK_MESSAGE


def _founder_copy(message):
    """Founder copy, never claiming a staff notification that alerts would not send."""
    if not isinstance(message, str) or not message.strip() or message == FALLBACK_MESSAGE:
        return fallback_message()
    return message


def quality_blocker_code(blocker):
    """True for Content Factory's hosted article quality blockers (``article_preview_*``)."""
    return isinstance(blocker, dict) and str(blocker.get("code") or "").startswith("article_preview_")


def founder_blocker(blocker):
    """Project an article quality blocker to its code and founder copy; other blockers pass through."""
    if not quality_blocker_code(blocker):
        return blocker
    view = {key: blocker[key] for key in FOUNDER_BLOCKER_KEYS if key in blocker}
    # A blocker without founder copy must not fall back to the staff message.
    view["founder_message"] = _founder_copy(blocker.get("founder_message"))
    return view


def founder_quality(quality, *, target=None):
    """Project hosted quality state to the fields founder surfaces read."""
    if not isinstance(quality, dict):
        return quality
    view = {key: quality[key] for key in FOUNDER_QUALITY_KEYS if key in quality}
    if automatic_retry_pending(quality, target=target):
        # A recoverable enqueue failure reads as the check still running. The
        # stored state keeps the real failure, and approval stays blocked.
        view["status"] = "queued"
    if view.get("founder_message") is not None:
        view["founder_message"] = _founder_copy(view["founder_message"])
    findings = quality.get("findings")
    if isinstance(findings, list):
        # Malformed (non-string) upstream findings are diagnostics, never a response error.
        view["findings"] = [item for item in findings if isinstance(item, str) and item in FOUNDER_FINDING_MARKERS]
    return view


_TOP_LEVEL = object()


def founder_result(result, *, target=_TOP_LEVEL):
    """Apply the founder projections wherever a run result carries quality state.

    The current preview target comes from the top-level result only, and
    nested ``result`` and control-response copies inherit it.
    """
    if not isinstance(result, dict):
        return result
    if target is _TOP_LEVEL:
        target = current_preview_target(result)
    view = dict(result)
    blocker = view.get("approval_blocker")
    if "approval_blocker" in view:
        view["approval_blocker"] = founder_blocker(blocker)
    if quality_blocker_code(blocker):
        # A rejected approval's control response repeats the staff message as
        # error text and keeps Content Factory's raw body; founders get the copy.
        copy = view["approval_blocker"]["founder_message"]
        for key in ("error", "detail", "message"):
            if key in view:
                view[key] = copy
        if "errors" in view:
            view["errors"] = [copy]
        for key in ("content_factory_response", "contentFactoryResponse"):
            if key in view:
                raw = view[key] if isinstance(view[key], dict) else {}
                view[key] = {"detail": founder_blocker(raw["detail"])} if quality_blocker_code(raw.get("detail")) else {}
    for key in ("article_preview_quality", "articlePreviewQuality"):
        if key in view:
            view[key] = founder_quality(view[key], target=target)
    for key in ("result", "latest_control_response"):
        if isinstance(view.get(key), dict):
            view[key] = founder_result(view[key], target=target)
    return view


def preview_quality_publish_notice(quality, *, target=None):
    """Return a locked/blocked publish state without projecting raw editorial findings."""
    quality = quality if isinstance(quality, dict) else {}
    status = str(quality.get("status") or "").strip().lower()
    if automatic_retry_pending(quality, target=target):
        status = "queued"
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
