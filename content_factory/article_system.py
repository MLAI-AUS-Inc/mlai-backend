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
    "system_type": "",
    "route_template": "",
    "content_source": None,
    "publish_mutation_target": None,
    "readiness": {},
    "registry": {},
    "diagnostics": {},
    "registry_selection_cache": {},
    "observability": {},
}

READY_STATES = {"existing", "roo_scaffolded"}
REGISTRY_DRIVEN_SEO_TARGET_KIND = "registry_driven_seo"
REGISTRY_ENTRY_DELIVERY_ADAPTER = "registry_entry"
REGISTRY_ENTRY_STRATEGY = "registry_entry_patch"
REGISTRY_READINESS_SUBSTATES = (
    "structure_ready",
    "mapping_ready",
    "routing_ready",
    "safety_ready",
)


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
    if not isinstance(article_system.get("readiness"), dict):
        article_system["readiness"] = {}
    if not isinstance(article_system.get("registry"), dict):
        article_system["registry"] = {}
    if not isinstance(article_system.get("diagnostics"), dict):
        article_system["diagnostics"] = {}
    if not isinstance(article_system.get("registry_selection_cache"), dict):
        article_system["registry_selection_cache"] = {}
    if not isinstance(article_system.get("observability"), dict):
        article_system["observability"] = {}
    return article_system


def _target_strategy(target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    strategy = (target or {}).get("registration_strategy")
    return strategy if isinstance(strategy, dict) else {}


def is_registry_driven_publish_target(target: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(target, dict):
        return False
    kind = str(target.get("kind") or "").strip()
    adapter = str(target.get("delivery_adapter") or "").strip()
    strategy = str(_target_strategy(target).get("type") or "").strip()
    return (
        kind == REGISTRY_DRIVEN_SEO_TARGET_KIND
        or adapter == REGISTRY_ENTRY_DELIVERY_ADAPTER
        or strategy == REGISTRY_ENTRY_STRATEGY
    )


def registry_target_readiness(target: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    if not is_registry_driven_publish_target(target):
        return {key: False for key in REGISTRY_READINESS_SUBSTATES}

    readiness = (target or {}).get("readiness")
    if not isinstance(readiness, dict):
        readiness = _target_strategy(target).get("readiness")
    if not isinstance(readiness, dict):
        readiness = {}

    status = str(
        (target or {}).get("registry_status")
        or (target or {}).get("status")
        or readiness.get("status")
        or ""
    ).strip()
    all_ready = status == "publish_ready" or bool(readiness.get("publish_ready"))
    return {
        key: bool(readiness.get(key) or all_ready)
        for key in REGISTRY_READINESS_SUBSTATES
    }


def registry_target_publish_ready(target: Optional[Dict[str, Any]]) -> bool:
    if not is_registry_driven_publish_target(target):
        return False
    readiness = registry_target_readiness(target)
    return all(readiness.get(key) for key in REGISTRY_READINESS_SUBSTATES)


def _article_system_path_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get("registry_path") or value.get("path") or value.get("file")


def registry_publish_target_from_article_system(article_system: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(article_system, dict):
        return None
    if str(article_system.get("system_type") or "").strip() != REGISTRY_DRIVEN_SEO_TARGET_KIND:
        return None

    registry = article_system.get("registry")
    if not isinstance(registry, dict):
        registry = {}
    registry_path = (
        registry.get("path")
        or _article_system_path_value(article_system.get("publish_mutation_target"))
        or _article_system_path_value(article_system.get("content_source"))
        or article_system.get("directory_path")
        or article_system.get("directory_name")
    )
    readiness = article_system.get("readiness") if isinstance(article_system.get("readiness"), dict) else {}
    diagnostics = article_system.get("diagnostics") if isinstance(article_system.get("diagnostics"), dict) else {}
    observability = article_system.get("observability") if isinstance(article_system.get("observability"), dict) else {}
    return {
        "kind": REGISTRY_DRIVEN_SEO_TARGET_KIND,
        "delivery_adapter": REGISTRY_ENTRY_DELIVERY_ADAPTER,
        "readiness": readiness,
        "diagnostics": diagnostics,
        "observability": observability,
        "registration_strategy": {
            "type": REGISTRY_ENTRY_STRATEGY,
            "registry_path": registry_path,
            "route_template": article_system.get("route_template") or "",
        },
    }


def best_registry_driven_publish_target(
    publish_targets: Optional[list] = None,
    article_system: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    targets = [item for item in (publish_targets or []) if isinstance(item, dict)]
    article_system_target = registry_publish_target_from_article_system(article_system)
    if article_system_target:
        targets = [*targets, article_system_target]
    registry_targets = [item for item in targets if is_registry_driven_publish_target(item)]
    if not registry_targets:
        return None
    ready_target = next((item for item in registry_targets if registry_target_publish_ready(item)), None)
    return ready_target or registry_targets[0]


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


def is_directly_publishable_target(target: Optional[Dict[str, Any]]) -> bool:
    """Whether a target represents a real, safe publish route (not a manual bundle fallback).

    ``react_article_system`` file-drop targets carry ``publish_capability == "direct"``; a
    registry-driven target counts when it is publish-ready. A ``bundle_only_article_directory``
    fallback (``publish_capability == "bundle_only"``) is explicitly NOT directly publishable —
    it is what the generic scan emits when it cannot confirm a safe publish route.
    """
    if not isinstance(target, dict):
        return False
    kind = str(target.get("kind") or "").strip()
    capability = str(target.get("publish_capability") or "").strip()
    if kind == "bundle_only_article_directory" or capability == "bundle_only":
        return False
    if capability == "direct":
        return True
    if is_registry_driven_publish_target(target) and registry_target_publish_ready(target):
        return True
    return False


def _has_directly_publishable_target(targets: Optional[list]) -> bool:
    return any(is_directly_publishable_target(item) for item in (targets or []))


def preserve_registered_publish_targets(
    *,
    incoming_targets: Optional[list],
    incoming_default_id: Optional[str],
    existing_targets: Optional[list],
    existing_default_id: Optional[str],
    article_system: Optional[Dict[str, Any]],
) -> tuple[list, Optional[str]]:
    """Decide which publish targets a scan callback should persist.

    The scaffold registers a high-confidence publish target at setup time, but
    the generic scan detector frequently cannot re-derive that target from the
    repository alone (a chicken-and-egg problem: it needs the registered surface
    to recognise the surface). Without this guard, a routine re-scan overwrites the
    stored target and silently unlinks publishing even though the article surface
    is still live in the repo.

    Two ways a re-scan would erase a registered publish-capable target:

    1. It returns ``publish_targets=[]`` — keep the stored target while the surface
       is still ready.
    2. It returns only a *weaker* fallback (e.g. a ``bundle_only_article_directory``
       for an RR v6 Vite SPA the generic detector cannot confirm as directly
       publishable). A non-empty result is normally authoritative, but a downgrade
       from a directly-publishable target to a bundle-only fallback must not clobber
       a live, publishing surface — that is exactly what silently forces
       ``content_only`` delivery on a working article system.

    A genuinely better detection still wins: if the incoming scan itself carries a
    directly-publishable target, it is authoritative and replaces the stored one.

    A deliberate teardown still clears targets via ``article_setup_reset`` — that
    path does not flow through here, so this guard does not block resets.
    """
    incoming_targets = incoming_targets or []
    existing_targets = existing_targets or []

    if incoming_targets:
        if (
            not _has_directly_publishable_target(incoming_targets)
            and _has_directly_publishable_target(existing_targets)
            and article_system_ready(article_system)
        ):
            return existing_targets, existing_default_id
        return incoming_targets, incoming_default_id

    if existing_targets and article_system_ready(article_system):
        return existing_targets, existing_default_id

    return incoming_targets, incoming_default_id


def recommended_next_action(scan_completed: bool, article_system: Dict[str, Any]) -> str:
    resolved = normalize_article_system(article_system)
    if not scan_completed:
        return "scan"
    if resolved["state"] in READY_STATES:
        return "research_article"
    if resolved["state"] == "ambiguous":
        return "confirm_article_system"
    return "scaffold"
