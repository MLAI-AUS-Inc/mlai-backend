from __future__ import annotations

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


def resolve_article_system(config) -> Dict[str, Any]:
    if config is None:
        return default_article_system()

    raw = getattr(config, 'article_system', None) or {}
    if raw:
        resolved = normalize_article_system(raw)
        if resolved["state"] != "missing" or resolved["directory_name"] or resolved["reason"]:
            return resolved

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
        )

    return default_article_system()


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

