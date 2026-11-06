"""Founder presentation of worker-owned hosted article quality state."""

SAFETY_MESSAGE = "Our team is checking the safety guidance in this article before it can be published. You don't need to do anything yet."
FALLBACK_MESSAGE = "We couldn't finish the final check automatically. Our team has been notified."


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
        return "blocked", str(quality.get("founder_message") or FALLBACK_MESSAGE)
    return None
