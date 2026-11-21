"""Founder presentation of worker-owned hosted article quality state."""

from urllib.parse import urlsplit

from django.utils.dateparse import parse_datetime

SAFETY_MESSAGE = "Publishing is paused because a safety statement needs verification."
PREVIEW_INPUTS_MESSAGE = "The preview needs a technical check before publishing."
FALLBACK_MESSAGE = "We couldn't finish the final check automatically. Our team has been notified."
# Used when staff alerts are off: nothing would actually tell the team.
UNALERTED_FALLBACK_MESSAGE = "We couldn't finish the final check automatically. Check again, or contact us if it keeps happening."
FAILED_TO_RUN_MESSAGE = "The final check didn't finish. Check again to unlock publishing."
FAILED_TO_RUN_STATUSES = frozenset({"review_error", "queue_failed"})
# An exhausted hold on something MLAI generated (plan A20): staff fix it and the
# founder is told nothing is needed. Mirrors Content Factory's copy.
STAFF_FIXING_MESSAGE = "We're fixing this on our side. Nothing is needed from you; publishing unlocks when it's done."

# Founders see the blocker's code and founder copy only. The technical
# ``message``, findings and repair instructions are staff diagnostics.
FOUNDER_BLOCKER_KEYS = ("code", "founder_message", "retryable", "retry_action", "owner", "run_id")
FOUNDER_QUALITY_KEYS = (
    "status", "escalation", "self_heal_exhausted", "self_heal_attempt", "founder_message", "blocker_code",
    "preview_url", "previewUrl", "resume_generation", "retryable", "advisory", "quality_attempt",
    "queued_at", "started_at", "finished_at", "checked_at", "reviewed_preview_commit_sha", "hold_owner",
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
    if message == STAFF_FIXING_MESSAGE:
        return staff_fixing_message()
    return message


def hold_owner(quality):
    """Who an exhausted hosted quality hold waits on: "staff", "founder" or None.

    A finding about an artefact MLAI generated (image binding to the run's
    manifest, rendering, layout, style or review inputs) is a platform fault:
    ``staff`` (staff_fixing). Findings about the founder's own content (claims,
    offer, safety guidance) stay ``founder`` holds. Content Factory names the
    owner on the record; a record written before it did is staff-owned only
    when its review inputs were invalid, which is always our own evidence.
    """
    if (not isinstance(quality, dict) or quality.get("status") != "blocking_findings"
            or quality.get("self_heal_exhausted") is not True):
        return None
    if quality.get("escalation") == "safety_review":
        return "founder"
    owner = quality.get("hold_owner")
    if owner in {"staff", "founder"}:
        return owner
    visible = quality.get("visible_content_acceptance")
    if quality.get("preview_inputs_invalid") is True or (
            isinstance(visible, dict) and visible.get("code") == "visible_review_inputs_invalid"):
        return "staff"
    return "founder"


def staff_fixing(quality):
    """True when the hold is ours to fix and the founder has nothing to do."""
    return hold_owner(quality) == "staff"


def staff_fixing_message():
    """Staff-fixing copy, never claiming a fix that no alert would start."""
    from django.conf import settings
    alerts = bool(getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False))
    return STAFF_FIXING_MESSAGE if alerts else UNALERTED_FALLBACK_MESSAGE


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
    if blocker.get("code") == "article_preview_safety_review_required":
        view["founder_message"] = SAFETY_MESSAGE
    return view


def _time(value):
    try:
        stamp = parse_datetime(value) if isinstance(value, str) else None
        return stamp if stamp and stamp.tzinfo else None
    except ValueError:
        return None


def _safety_review_status(alert, quality):
    if not isinstance(alert, dict):
        return None
    reviewed = _time(quality.get("reviewed_at"))
    queued = _time(alert.get("queued_at"))
    if not reviewed or not queued or reviewed > queued:
        return None
    if _time(alert.get("acknowledged_at")):
        return "acknowledged"
    if alert.get("state") == "sent" and _time(alert.get("sent_at")):
        return "notified"
    return None


def _source_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and not (url.username or url.password):
            return value
    except ValueError:
        pass
    return None


def _safety_check(quality):
    check = quality.get("safety_check")
    if not isinstance(check, dict) or check.get("status") not in ("checked", "needs_verification"):
        return None
    raw_items = check.get("items")
    items = []
    seen_sentences = set()
    for item in raw_items[:12] if isinstance(raw_items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("sentence"), str):
            continue
        sentence = item["sentence"].strip()
        if not sentence or len(sentence) > 1000 or sentence in seen_sentences:
            continue
        seen_sentences.add(sentence)
        if check["status"] == "needs_verification":
            items.append({"sentence": sentence})
            continue
        raw_sources = item.get("sources")
        sources = [
            {"url": source["url"], "passage": source["passage"][:1400]}
            for source in (raw_sources[:8] if isinstance(raw_sources, list) else [])
            if isinstance(source, dict) and _source_url(source.get("url"))
            and isinstance(source.get("passage"), str) and source["passage"].strip()
        ]
        if sources:
            items.append({"sentence": sentence, "sources": sources})
    if items or check["status"] == "needs_verification":
        return {"status": check["status"], "items": items}
    return None


def founder_quality(quality, *, target=None, safety_alert=None):
    """Project hosted quality state to the fields founder surfaces read."""
    if not isinstance(quality, dict):
        return quality
    view = {key: quality[key] for key in FOUNDER_QUALITY_KEYS if key in quality}
    if quality.get("self_heal_removed_claims"):
        # Owner decision D4: the founder sees every sentence of theirs the final
        # check removed. Their own article text only; findings stay staff-only.
        from .article_quality_record import removed_claims
        view["removed_claims"] = [text for entry in removed_claims(quality) for text in entry["removed"]]
    if automatic_retry_pending(quality, target=target):
        # A recoverable enqueue failure reads as the check still running. The
        # stored state keeps the real failure, and approval stays blocked.
        view["status"] = "queued"
    if view.get("founder_message") is not None:
        view["founder_message"] = _founder_copy(view["founder_message"])
    safety_hold = quality.get("status") == "blocking_findings" and quality.get("escalation") == "safety_review"
    if safety_hold:
        view["founder_message"] = SAFETY_MESSAGE
    ours = staff_fixing(quality)
    if ours:
        view["hold_owner"] = "staff"
        view["founder_message"] = staff_fixing_message()
    if target is not None and (quality.get("preview_url"), quality.get("resume_generation")) == target:
        check = _safety_check(quality)
        if check:
            view["safety_check"] = check
        if safety_hold:
            review_status = _safety_review_status(safety_alert, quality)
            if review_status:
                view["safety_review_status"] = review_status
        # Our own invalid review evidence is not a technical check for the founder.
        if quality.get("preview_inputs_invalid") is True and not ours:
            view["preview_inputs_invalid"] = True
            if not safety_hold and quality.get("status") == "blocking_findings":
                view["founder_message"] = PREVIEW_INPUTS_MESSAGE
    findings = quality.get("findings")
    if isinstance(findings, list):
        # Malformed (non-string) upstream findings are diagnostics, never a response error.
        view["findings"] = [item for item in findings if isinstance(item, str) and item in FOUNDER_FINDING_MARKERS]
    return view


_TOP_LEVEL = object()
# Staff audit records (actors, notes, sources) never reach a founder response.
STAFF_ONLY_RESULT_KEYS = ("staff_actions_v1", "staff_safety_verification_v1", "ops_alerts")


def founder_result(result, *, target=_TOP_LEVEL, safety_alert=None):
    """Apply the founder projections wherever a run result carries quality state.

    The current preview target comes from the top-level result only, and
    nested ``result`` and control-response copies inherit it.
    """
    if not isinstance(result, dict):
        return result
    if target is _TOP_LEVEL:
        target = current_preview_target(result)
        alerts = result.get("ops_alerts")
        if safety_alert is None and isinstance(alerts, dict):
            safety_alert = alerts.get("article_safety_review_required")
    view = {key: value for key, value in result.items() if key not in STAFF_ONLY_RESULT_KEYS}
    blocker = view.get("approval_blocker")
    if "approval_blocker" in view:
        view["approval_blocker"] = founder_blocker(blocker)
        if quality_blocker_code(blocker) and staff_fixing(result.get("article_preview_quality")):
            view["approval_blocker"] = {**view["approval_blocker"], "founder_message": staff_fixing_message(),
                                        "owner": "staff"}
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
            view[key] = founder_quality(view[key], target=target, safety_alert=safety_alert)
    for key in ("result", "latest_control_response"):
        if isinstance(view.get(key), dict):
            view[key] = founder_result(view[key], target=target, safety_alert=safety_alert)
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
        if staff_fixing(quality):
            return "blocked", staff_fixing_message()
        if quality.get("preview_inputs_invalid") is True:
            return "blocked", PREVIEW_INPUTS_MESSAGE
        return "blocked", str(quality.get("founder_message") or fallback_message())
    if status in FAILED_TO_RUN_STATUSES:
        # A required check that crashed or never queued has not passed.
        return "blocked", FAILED_TO_RUN_MESSAGE
    return None
