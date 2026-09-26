"""A human rewrite is an assertion; changing disclosure is not evidence review."""
import copy

NARRATIVE_FIELDS = ("summary", "highlights", "challenges", "learnings", "next30Days", "asks")


def manual_validation(previous, proposed, validation, *, metrics_changed=False):
    unchanged = previous is not None and not metrics_changed and all(
        str(previous.get(key) or "").strip() == str(proposed.get(key) or "").strip()
        for key in NARRATIVE_FIELDS
    )
    if unchanged:
        # Audience/config changes and repeated saves cannot erase an AI failure.
        return copy.deepcopy(validation or {"groundedness_status": "pending"})
    return {
        "groundedness_status": "founder_asserted",
        "notes": "Founder-entered or edited content. Claims require the founder's explicit review; this is not an AI verification.",
    }
