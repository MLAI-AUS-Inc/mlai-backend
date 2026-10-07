"""Versioned discovery projection; inventory never grants publishing authority."""

from .activation import mapping


def scan_payload(value):
    """Read historical flat and durable nested scan envelopes consistently."""
    payload = mapping(value)
    for _ in range(3):
        nested = mapping(payload.get("result"))
        if not nested:
            break
        payload = {**payload, **nested}
        payload.pop("result", None)
    return payload


def discovery_snapshot(value):
    """Return display evidence without synthesizing targets or ready verdicts."""
    payload = scan_payload(value)
    inventory = mapping(payload.get("repository_inventory"))
    normalized = mapping(payload.get("repository_discovery"))
    resolution = mapping(inventory.get("article_surface_resolution")) or mapping(payload.get("article_surface_resolution")) or mapping(mapping(payload.get("articles_status")).get("article_surface_resolution"))
    candidates, seen = [], set()
    ranked = mapping(resolution.get("ranked_candidates"))
    for group in ("listing_surface_candidates", "detail_surface_candidates", "publish_mutation_candidates", "content_source_candidates"):
        for item in ranked.get(group) or []:
            if not isinstance(item, dict):
                continue
            metadata = mapping(item.get("metadata"))
            path = str(item.get("path_or_locator") or "")
            route = str(item.get("route") or metadata.get("route_path") or metadata.get("listing_route_path") or "")
            key = (group, path, route)
            if not path or key in seen:
                continue
            seen.add(key)
            candidates.append({"id": ":".join(key), "candidate_group": group, "kind": item.get("kind"), "path_or_locator": path,
                "route": route, "route_template": item.get("route_template") or metadata.get("route_template") or "",
                "confidence": item.get("confidence")})
    if not candidates:
        candidates = [item for item in (payload.get("detected_candidates") or normalized.get("candidates") or []) if isinstance(item, dict)][:64]
    raw_paths = normalized.get("support_paths") if isinstance(normalized.get("support_paths"), list) else []
    support_paths = [{key: item[key][:1000] for key in ("id", "label", "status", "reason", "action") if isinstance(item.get(key), str)}
        for item in raw_paths[:8] if isinstance(item, dict) and item.get("id") in {"native", "custom_contract", "bring_ci", "provider", "portable"}]
    return {"version": 1, "complete": inventory.get("discovery_complete") is True or payload.get("scan_complete") is True or normalized.get("discovery_complete") is True,
        "sourceSha": inventory.get("source_sha") or inventory.get("repo_head_sha") or payload.get("repo_head_sha") or payload.get("commit_sha") or normalized.get("source_sha") or None,
        "githubRepo": inventory.get("github_repo") or payload.get("github_repo") or "",
        "repositoryId": inventory.get("repository_id") or payload.get("repository_id"),
        "branch": inventory.get("branch") or payload.get("default_branch") or payload.get("branch") or "",
        "appRoot": inventory.get("app_root") or payload.get("app_root") or "",
        "framework": inventory.get("detected_framework") or inventory.get("framework")
            or payload.get("detected_framework") or payload.get("framework") or normalized.get("framework"),
        "resolution": resolution, "candidates": candidates, "publishReady": False,
        "supportLevel": str(normalized.get("support_level") or "unsupported")[:100], "supportPaths": support_paths}


def normalize_scan_callback(value):
    """Promote inventory metadata for presentation while preserving its boundary."""
    data = scan_payload(value)
    discovery = discovery_snapshot(data)
    data.setdefault("default_branch", discovery["branch"])
    data.setdefault("repo_head_sha", discovery["sourceSha"] or "")
    data.setdefault("article_surface_resolution", discovery["resolution"])
    data.setdefault("detected_candidates", discovery["candidates"])
    return data
