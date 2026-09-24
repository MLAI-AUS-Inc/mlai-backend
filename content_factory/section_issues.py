"""Small, public projection of Content Factory article grounding issues.

Raw run diagnostics and grounding artifacts may contain paths, source text, or
credentials. Founder Tools only needs a bounded explanation anchored to a
stable article section; no arbitrary diagnostic fields cross this boundary.
"""

from __future__ import annotations

import re


_SECTION_ID = re.compile(r"^section:[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}$")
_CLAIM_ID = re.compile(r"^claim-[0-9]{1,6}$")
_SECRET = re.compile(
    r"(?i)(?:\bBearer\s+\S+|\bsk[-_][A-Za-z0-9_-]{8,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_-]{8,}"
    r"|\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)"
)


def _safe_text(value, limit):
    if not isinstance(value, str):
        return ""
    flattened = " ".join(value.split())
    return _SECRET.sub("[redacted]", flattened)[:limit]


def public_section_issues(raw):
    """Return the supported section issue contract, dropping malformed entries."""
    if not isinstance(raw, list):
        return []
    issues = []
    seen = set()
    for value in raw[:100]:
        if not isinstance(value, dict):
            continue
        section_id = value.get("sectionId") or value.get("section_id")
        claim_id = value.get("claimId") or value.get("claim_id")
        state = value.get("state")
        if (
            not isinstance(section_id, str)
            or not _SECTION_ID.fullmatch(section_id)
            or not isinstance(claim_id, str)
            or not _CLAIM_ID.fullmatch(claim_id)
            or state not in {"needs_review", "removed"}
        ):
            continue
        issue_id = f"{section_id}:{claim_id}"
        if issue_id in seen:
            continue
        seen.add(issue_id)
        issues.append(
            {
                "id": issue_id,
                "sectionId": section_id,
                "claimId": claim_id,
                "claimExcerpt": _safe_text(value.get("claimExcerpt") or value.get("claim_excerpt"), 240),
                "reason": _safe_text(value.get("reason"), 240)
                or "This claim could not be substantiated by the gathered sources.",
                "state": state,
                "sourceHint": _safe_text(value.get("sourceHint") or value.get("source_hint"), 160)
                or "Check the research sources or remove this section.",
            }
        )
    return issues
