"""Reviewed occurrence counts attached to existing profile patches."""

import hashlib
import json

COUNT_ORIGIN = "volunteer_badge_counts"
COUNT_KIND = "badge_earn_count"
PUBLISHED = "published"
WITHDRAWN = "withdrawn"

_PROGRESS_KEYS = {
    "hello.steady": {"months", "accepted_deliverables"},
    "event.series": {"months", "events_led"},
}
_MAX_SAFE_INTEGER = 2**53 - 1


def count_key(user_id, badge_id):
    return f"volunteer-badge-count:{user_id}:{badge_id}"


def grant_fingerprint(grant_id, evidence_sha256, evidence_ref):
    """Bind the projection to the reviewed grant and its exact private evidence."""
    data = json.dumps([str(grant_id), evidence_sha256, evidence_ref], separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def valid_value(badge_id, count, status, progress):
    if type(count) is not int or not 1 <= count <= _MAX_SAFE_INTEGER:
        return False
    if status not in ("exact", "at_least") or not isinstance(progress, dict):
        return False
    return progress.keys() <= _PROGRESS_KEYS.get(badge_id, set()) and all(
        type(value) is int and 0 <= value <= _MAX_SAFE_INTEGER for value in progress.values()
    )


def public_count(grant, badge_id, receipt):
    """Return only public, validated count fields for the corresponding active grant."""
    unknown = dict(earn_count=None, earn_count_status="unresolved", earn_count_progress={})
    if receipt is None or receipt.actor_id != grant.actor_id or not isinstance(receipt.metadata, dict):
        return unknown
    meta = receipt.metadata
    if meta.get("badge_id") != badge_id or meta.get("grant_fingerprint") != grant_fingerprint(
        grant.pk, grant.metadata.get("evidence_sha256"), grant.metadata.get("evidence_ref")
    ):
        return unknown
    count, status, progress = meta.get("count"), meta.get("status"), meta.get("progress")
    if not valid_value(badge_id, count, status, progress):
        return unknown
    return dict(earn_count=count, earn_count_status=status, earn_count_progress=progress)
