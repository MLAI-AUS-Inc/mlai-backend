"""The readable final-check record of one article run, for staff (plan workstream 5).

Content Factory sends a capped findings list with its hosted quality state
(``article_preview_quality.findings_detail``, behind its
CONTENT_FACTORY_QUALITY_FINDINGS_CALLBACK setting). The run keeps it in its
existing ``result`` JSON. This module turns that state into one record for the
staff console (beside "Retry now") and for ``manage.py article_quality``, so a
held article's real findings no longer need a server session to read.

Staff only: claim text never reaches a founder response (see
hosted_quality_state.FOUNDER_QUALITY_KEYS). Read-only: nothing here writes.
"""
from __future__ import annotations

import re

MAX_FINDINGS = 50
MAX_FIELD_CHARS = 300
SEVERITIES = ("blocking", "advisory", "info")
SOURCES = ("authored", "site")
_URL_WITH_QUERY = re.compile(r"https?://[^\s\"'<>]*\?[^\s\"'<>]*", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")
HISTORY_FIELDS = ("attempt", "buckets", "targets", "result")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    """One line, at most 300 characters, with no URL that carries a query string."""
    text = _URL_WITH_QUERY.sub("[link removed]", " ".join(str(value or "").split()))
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS - 1].rstrip() + "…"


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def findings(quality) -> list[dict]:
    """Content Factory's findings list, re-validated and re-capped on read."""
    raw = _mapping(quality).get("findings_detail")
    items = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        items.append({
            "code": _text(item.get("code")) or "finding",
            "severity": item.get("severity") if item.get("severity") in SEVERITIES else "blocking",
            "claim": _text(item.get("claim")),
            "reason": _text(item.get("reason")),
            "source": item.get("source") if item.get("source") in SOURCES else "authored",
        })
        if len(items) >= MAX_FINDINGS:
            break
    return items


def removed_claims(quality) -> list[dict]:
    """Sentences the final check removed automatically (owner decision D4), by attempt."""
    raw = _mapping(quality).get("self_heal_removed_claims")
    rounds = []
    for entry in raw if isinstance(raw, list) else []:
        entry = _mapping(entry)
        texts = [_text(text) for text in entry.get("removed") or [] if isinstance(text, str) and text.strip()]
        if texts:
            attempt = entry.get("attempt")
            rounds.append({"attempt": attempt if isinstance(attempt, int) else None, "removed": texts[:MAX_FINDINGS]})
    return rounds


def _history(quality) -> list[dict]:
    rows = []
    for entry in _mapping(quality).get("self_heal_history") or []:
        entry = _mapping(entry)
        row = {}
        for key in HISTORY_FIELDS:
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                row[key] = value
            elif isinstance(value, list):
                row[key] = [token for token in (_token(item) for item in value) if token][:20]
            else:
                row[key] = _token(value)
        rows.append(row)
    return rows[-5:]


def quality_record(run) -> dict:
    """Everything staff need to understand a held final check, without article prose beyond the findings."""
    from .hosted_quality_state import hold_owner
    result = _mapping(getattr(run, "result", None))
    quality = _mapping(result.get("article_preview_quality"))
    return {
        "runId": getattr(run, "run_id", None),
        "status": _token(quality.get("status")),
        "holdOwner": hold_owner(quality),
        "blockerCode": _token(quality.get("blocker_code")) or _token(_mapping(result.get("approval_blocker")).get("code")),
        "stopReason": _token(quality.get("self_heal_stop_reason")),
        "attempts": quality.get("self_heal_attempt") if isinstance(quality.get("self_heal_attempt"), int) else 0,
        "history": _history(quality),
        "findings": findings(quality),
        "findingsReported": "findings_detail" in quality,
        "removedClaims": removed_claims(quality),
    }
