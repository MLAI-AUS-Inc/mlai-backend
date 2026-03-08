from __future__ import annotations

import ast
import json
from copy import deepcopy
from typing import Any, Dict, Optional


ARTICLE_SYSTEM_TEMPLATE = {
    "state": "missing",
    "directory_name": None,
    "directory_path": None,
    "confidence": "low",
    "reason": "",
    "source": "scan",
    "verified_at": None,
}

READY_STATES = {"existing", "roo_scaffolded"}


def default_article_system() -> Dict[str, Any]:
    return deepcopy(ARTICLE_SYSTEM_TEMPLATE)


def normalize_article_system(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    article_system = default_article_system()
    if isinstance(value, dict):
        for key in ARTICLE_SYSTEM_TEMPLATE:
            if key in value:
                article_system[key] = value.get(key)

    if article_system["state"] not in {"missing", "existing", "roo_scaffolded", "ambiguous"}:
        article_system["state"] = "missing"
    if article_system["confidence"] not in {"high", "medium", "low"}:
        article_system["confidence"] = "low"
    if article_system["source"] not in {"scan", "scaffold", "manual_confirmed"}:
        article_system["source"] = "scan"
    return article_system


def infer_confidence(articles_status: Optional[Dict[str, Any]]) -> str:
    status = articles_status or {}
    explicit = status.get("confidence")
    if explicit in {"high", "medium", "low"}:
        return explicit

    if not status.get("has_articles_system"):
        return "low"

    existing_files = len(status.get("existing_files") or [])
    if status.get("directory_path") and status.get("detected_type") not in {None, "none"}:
        return "high"
    if existing_files >= 2 or status.get("routing_pattern") or status.get("content_format"):
        return "medium"
    return "low"


def infer_reason(articles_status: Optional[Dict[str, Any]]) -> str:
    status = articles_status or {}
    if status.get("reason"):
        return str(status["reason"])

    if status.get("has_articles_system"):
        directory_path = status.get("directory_path") or status.get("directory_name")
        detected_type = status.get("detected_type", "existing")
        if directory_path:
            return f"Detected {detected_type} article system at {directory_path}"
        return f"Detected {detected_type} article system"

    if status.get("directory_name") or status.get("directory_path"):
        location = status.get("directory_path") or status.get("directory_name")
        return f"Possible article system detected near {location}"
    return "No existing article or blog system detected"


def article_system_from_scan_summary(
    scan_summary: Optional[Dict[str, Any]],
    *,
    verified_at: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(scan_summary, str):
        try:
            scan_summary = json.loads(scan_summary)
        except json.JSONDecodeError:
            try:
                scan_summary = ast.literal_eval(scan_summary)
            except (SyntaxError, ValueError):
                scan_summary = None

    if not isinstance(scan_summary, dict):
        return default_article_system()

    articles_status = scan_summary.get("articles_status")
    if not isinstance(articles_status, dict):
        return default_article_system()

    confidence = infer_confidence(articles_status)
    has_articles_system = bool(articles_status.get("has_articles_system"))

    if has_articles_system:
        state = "existing" if confidence in {"high", "medium"} else "ambiguous"
    else:
        state = "missing"

    return normalize_article_system(
        {
            "state": state,
            "directory_name": articles_status.get("directory_name"),
            "directory_path": articles_status.get("directory_path"),
            "confidence": confidence,
            "reason": infer_reason(articles_status),
            "source": "scan",
            "verified_at": verified_at,
        }
    )


def resolve_article_system_with_source(config) -> tuple[Dict[str, Any], str]:
    if config is None:
        return default_article_system(), "default_missing"

    raw = getattr(config, 'article_system', None) or {}
    if raw:
        resolved = normalize_article_system(raw)
        if resolved["state"] != "missing" or resolved["directory_name"] or resolved["reason"]:
            return resolved, "canonical_field"

    updated_at = getattr(config, 'updated_at', None)
    scan_fallback = article_system_from_scan_summary(
        getattr(config, 'scan_summary', None),
        verified_at=updated_at.isoformat() if updated_at else None,
    )
    if scan_fallback != default_article_system():
        if (
            scan_fallback["state"] != "missing"
            or scan_fallback["directory_name"]
            or scan_fallback["directory_path"]
            or scan_fallback["reason"]
        ):
            return scan_fallback, "scan_summary_fallback"

    if getattr(config, 'articles_scaffolded', False):
        return normalize_article_system(
            {
                "state": "roo_scaffolded",
                "directory_name": "articles",
                "directory_path": None,
                "confidence": "high",
                "reason": "Roo previously scaffolded the article system for this repository",
                "source": "scaffold",
                "verified_at": getattr(config, 'updated_at', None).isoformat() if getattr(config, 'updated_at', None) else None,
            }
        ), "scaffold_flag"

    return default_article_system(), "default_missing"


def resolve_article_system(config) -> Dict[str, Any]:
    return resolve_article_system_with_source(config)[0]


def article_system_ready(article_system: Dict[str, Any]) -> bool:
    return normalize_article_system(article_system)["state"] in READY_STATES


def merge_article_system(current_value: Optional[Dict[str, Any]], incoming_value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    current = normalize_article_system(current_value)
    incoming = normalize_article_system(incoming_value)

    if incoming == default_article_system() and current != default_article_system():
        return current

    if current["state"] == "roo_scaffolded":
        if incoming["state"] == "existing" and incoming["confidence"] == "high":
            current_path = current.get("directory_path")
            incoming_path = incoming.get("directory_path")
            if not current_path or not incoming_path or current_path == incoming_path:
                return incoming
        if incoming["state"] == "missing" and incoming["confidence"] in {"high", "medium"}:
            return incoming
        return current

    if current["state"] in {"existing", "roo_scaffolded"} and incoming["state"] == "ambiguous":
        return current

    if current["state"] == "existing" and incoming["state"] == "missing" and incoming["confidence"] == "low":
        return current

    if incoming["state"] == "missing" and current["state"] in READY_STATES and incoming["confidence"] == "low":
        return current

    return incoming


def recommended_next_action(scan_completed: bool, article_system: Dict[str, Any]) -> str:
    resolved = normalize_article_system(article_system)
    if not scan_completed:
        return "scan"
    if resolved["state"] in READY_STATES:
        return "research_article"
    if resolved["state"] == "ambiguous":
        return "confirm_article_system"
    return "scaffold"
