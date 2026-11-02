"""Read-only engagement ratings and protected import metadata on contacts.

Ratings are observations, never permission to send. Keeping the snapshot in
``Contact.attributes`` needs no migration and leaves unrated contacts unknown.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite

ENGAGEMENT_RATING_KEY = "engagement_rating"
MARKETING_REVIEW_HOLD_KEY = "marketing_review_hold"
RESERVED_ATTRIBUTE_KEYS = frozenset((ENGAGEMENT_RATING_KEY, MARKETING_REVIEW_HOLD_KEY, "engagement_import", "engagement_activity", "engagement_refresh", "engagement_legacy_attributes", "engagement_newsletter"))


def engagement_rating(attributes) -> dict | None:
    """Return the public rating snapshot, or None for missing/malformed data."""

    if not isinstance(attributes, dict):
        return None
    value = attributes.get(ENGAGEMENT_RATING_KEY)
    if not isinstance(value, dict):
        return None
    stars, score = value.get("stars"), value.get("score")
    if type(stars) is not int or not 1 <= stars <= 5:
        return None
    if type(score) not in (int, float):
        return None
    try:
        if score < 0 or not isfinite(score):
            return None
    except OverflowError:
        return None
    version, as_of, reason = (value.get(key) for key in ("version", "as_of", "reason"))
    if not isinstance(version, str) or not version.strip() or len(version) > 100:
        return None
    if not isinstance(as_of, str) or len(as_of) > 64:
        return None
    try:
        datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not isinstance(reason, str) or len(reason) > 2000:
        return None
    coverage = value.get("coverage")
    if not isinstance(coverage, dict):
        return None
    return {
        "stars": stars,
        "score": score,
        "version": version,
        "as_of": as_of,
        "reason": reason,
        "coverage": deepcopy(coverage),
    }


def marketing_review_hold(attributes) -> bool:
    """Treat any present value except JSON false as an unresolved review hold."""

    if not isinstance(attributes, dict):
        return True
    return (
        MARKETING_REVIEW_HOLD_KEY in attributes
        and attributes[MARKETING_REVIEW_HOLD_KEY] is not False
    )


def editable_attributes(attributes) -> dict:
    """Return ordinary attributes without system-owned rating/review metadata."""

    if not isinstance(attributes, dict):
        return {}
    return {key: value for key, value in attributes.items() if key not in RESERVED_ATTRIBUTE_KEYS}
