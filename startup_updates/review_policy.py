"""A human rewrite is an assertion; changing disclosure is not evidence review."""
import copy

NARRATIVE_FIELDS = ("startupOverview", "summary", "highlights", "challenges", "learnings", "next30Days", "asks")
# Memo sections behind those fields: highlights merges financial_performance and
# operations, challenges are lowlights, and a legacy topline stands in for summary.
NARRATIVE_MEMO_KEYS = ("summary", "topline", "financial_performance", "highlights", "operations",
    "lowlights", "learnings", "next_30_days", "asks")


def manual_validation(previous, proposed, validation, *, metrics_changed=False):
    unchanged = previous is not None and not metrics_changed and all(
        str(previous.get(key) or "").strip() == str(proposed.get(key, previous.get(key) if key == "startupOverview" else None) or "").strip()
        for key in NARRATIVE_FIELDS
    )
    if unchanged:
        # Audience/config changes and repeated saves cannot erase an AI failure.
        return copy.deepcopy(validation or {"groundedness_status": "pending"})
    return {
        "groundedness_status": "founder_asserted",
        "notes": "Founder-entered or edited content. Claims require the founder's explicit review; this is not an AI verification.",
    }


def has_narrative(memo):
    """Metrics, artwork and disclosure alone are not an update a founder can approve."""
    return any(_has_text((memo or {}).get(key)) for key in NARRATIVE_MEMO_KEYS)


def _has_text(value):
    if isinstance(value, dict):
        value = value.get("text") or value.get("value") or value.get("title") or value.get("label")
    if isinstance(value, (list, tuple)):
        return any(_has_text(item) for item in value)
    return value is not None and bool(str(value).strip())
