"""Reviewed audience vocabulary; labels never serve as stored predicate IDs."""

from __future__ import annotations

import re

TAXONOMY_VERSION = "2026-11-03"
CITY_OPTIONS = {
    "au-sa-adelaide": "Adelaide",
    "au-vic-melbourne": "Melbourne",
    "au-nsw-sydney": "Sydney",
    "au-wa-perth": "Perth",
    "au-qld-brisbane": "Brisbane",
    "au-act-canberra": "Canberra",
    "au-tas-hobart": "Hobart",
    "au-nt-darwin": "Darwin",
}
FACETS = {
    "location_affinity": {"label": "City connections", "multiple": True, "options": CITY_OPTIONS},
    "reported_city": {"label": "Reported city", "multiple": False, "options": CITY_OPTIONS},
    "career": {"label": "Career", "multiple": True, "options": {
        "founder": "Founder", "developer": "Developer", "designer": "Designer",
        "researcher": "Researcher", "clinician": "Clinician", "student": "Student",
        "product": "Product", "operations": "Operations", "other": "Other role",
    }},
    "career_stage": {"label": "Career stage", "multiple": False, "options": {
        "student": "Student", "early_career": "Early career", "mid_career": "Mid-career",
        "senior": "Senior",
    }},
    "career_transition": {"label": "Career transition", "multiple": False, "options": {
        "career_changer": "Career changer", "not_changing": "Not changing career",
    }},
    "gender": {"label": "Gender", "multiple": True, "self_report_only": True, "options": {
        "woman": "Woman", "man": "Man", "non_binary": "Non-binary", "self_described": "Self-described",
    }},
    "age_band": {"label": "Age", "multiple": False, "self_report_only": True, "options": {
        "18_24": "18–24", "25_34": "25–34", "35_44": "35–44", "45_plus": "45+",
    }},
    "interests": {"label": "Interests", "multiple": True, "options": {
        "research": "Research", "building": "Building", "startups": "Startups",
        "learning": "Learning", "community": "Community", "other": "Other interest",
    }},
}
FACET_STATUSES = ("known", "not_provided", "withheld", "conflicted", "unmapped")
PROVENANCES = ("self_reported", "verified", "derived", "imported")
SOURCE_KINDS = ("luma_answer", "csv", "admin_correction", "community_profile", "event", "membership")
ACTIVITY_SOURCES = {
    "luma_checkin": "Checked in at an event",
    "slack_message": "Active in public Slack",
    "app_login": "Explicit app sign-in",
    "newsletter_click": "Clicked a newsletter link",
}
ACTIVITY_STATUSES = ("available", "partial", "stale", "unavailable")
IDENTITY_STATUSES = ("verified", "unmapped", "conflicted")


def normalized_label(value: str) -> str:
    """Normalize exact reviewed aliases, without fuzzy or inferred classification."""

    return re.sub(r"\s+", " ", value.strip().casefold().replace("–", "-").replace("_", " "))


def normalize_value(facet: str, value: str, *, aliases: dict | None = None) -> str | None:
    """Return an allowed stable ID; unknown titles and demographic text stay unmapped."""

    spec = FACETS.get(facet)
    if spec is None or not isinstance(value, str):
        return None
    if value in spec["options"]:
        return value
    target = (aliases or {}).get(normalized_label(value))
    if target in spec["options"]:
        return target
    matches = [key for key, label in spec["options"].items() if normalized_label(label) == normalized_label(value)]
    return matches[0] if len(matches) == 1 else None


def taxonomy_catalog() -> list[dict]:
    """Return fresh public metadata without exposing raw profile evidence."""

    return [{
        "id": key, "label": spec["label"], "multiple": spec["multiple"],
        "self_report_only": spec.get("self_report_only", False),
        "options": [{"id": value, "label": label} for value, label in spec["options"].items()],
    } for key, spec in FACETS.items()]
