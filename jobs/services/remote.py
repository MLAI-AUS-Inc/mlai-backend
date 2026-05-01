from __future__ import annotations

import re
from typing import Any

AUSTRALIA_ELIGIBLE_TERMS = (
    "australia",
    "apac",
    "asia pacific",
    "anz",
    "australia/new zealand",
    "worldwide",
    "global",
    "anywhere",
)

REMOTE_RESTRICTED_TERMS = (
    "us only",
    "united states only",
    "uk only",
    "canada only",
    "europe only",
    "must be based in the us",
    "must be based in united states",
)


def infer_remote_eligibility(job: dict[str, Any]) -> dict[str, Any]:
    text = searchable_text(job)
    is_remote = bool(job.get("is_remote")) or has_remote_signal(text)
    remote_region = job.get("remote_region")
    eligibility = "not_remote"
    score = 0.0

    if is_remote:
        eligibility = "unknown_remote"
        score = 0.45
        if any(term in text for term in AUSTRALIA_ELIGIBLE_TERMS):
            eligibility = "australia_eligible"
            score = 0.9
        if any(term in text for term in REMOTE_RESTRICTED_TERMS):
            eligibility = "restricted_remote"
            score = 0.15
        if not remote_region:
            remote_region = infer_remote_region(text)

    job.update(
        {
            "is_remote": is_remote,
            "remote_region": remote_region,
            "remote_eligibility": eligibility,
            "remote_eligibility_score": score,
        }
    )
    return job


def has_remote_signal(text: str) -> bool:
    return bool(re.search(r"\b(remote|work from home|anywhere|worldwide|global)\b", text))


def infer_remote_region(text: str) -> str | None:
    if "apac" in text or "asia pacific" in text:
        return "APAC"
    if "australia" in text or "anz" in text:
        return "Australia/ANZ"
    if "worldwide" in text or "global" in text or "anywhere" in text:
        return "Global"
    return "Remote"


def searchable_text(job: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            job.get("title"),
            job.get("location"),
            job.get("remote_region"),
            job.get("description"),
        )
    ).lower()
