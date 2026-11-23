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


# Ids match Content Factory's setup ladder (native, agentic, static_dropin,
# content_files) plus the two non-tier states.
SETUP_TIERS = ("native", "agentic", "static_dropin", "content_files", "cannot_yet", "not_checked")
TIER_RANKS = {"native": 1, "agentic": 2, "static_dropin": 3, "content_files": 4}
# Founder words for each tier (plan C3, 7.3): "works", "works with one step
# from you", or "can't be done yet" with the reason. Never "unsupported".
TIER_LABELS = {
    "native": "Works: MLAI sets up your articles page directly.",
    "agentic": "Works: MLAI's setup agent fits the articles page to your site, and checks it before anything changes.",
    "static_dropin": "Works: MLAI adds styled article pages beside your site.",
    "content_files": "Works with one step from you: MLAI sends the articles as files, and your developer adds them.",
    "cannot_yet": "Can't be done yet: your articles live in a content system, not in your website's code.",
    "not_checked": "We haven't checked your website's code yet.",
}
CMS_KINDS = ("wordpress", "drupal", "ghost", "headless_cms")


def content_source_snapshot(value):
    """Bounded view of CF's content-source classification (plan C15); {} when absent."""
    value = mapping(value)
    if value.get("version") != 1:
        return {}
    kind = value.get("kind") if value.get("kind") in {"cms", "repository", "unknown"} else "unknown"
    cms = value.get("cms") if kind == "cms" and value.get("cms") in CMS_KINDS else None
    if kind == "cms" and cms is None:
        kind = "unknown"
    # ``reason`` is the closed "can't be done yet" reason, not a registry failure code.
    out = {"version": 1, "kind": kind, "cms": cms, "reason": "cms_content" if cms else None,
           "signals": [str(item)[:200] for item in (value.get("signals") or [])[:8] if isinstance(item, str)]}
    if cms and isinstance(value.get("vendor"), str):
        out["vendor"] = value["vendor"][:40]
    return out


def setup_tier(normalized, content_source=None, *, complete=False):
    """The tier label for a scanned site; ``cannot_yet`` only for CMS content."""
    normalized = mapping(normalized)
    stored = mapping(normalized.get("setup_tier"))
    level = str(normalized.get("support_level") or "")
    if mapping(content_source).get("kind") == "cms":
        tier, reason = "cannot_yet", "cms_content"
    elif stored.get("id") in {"native", "agentic"}:
        tier, reason = stored["id"], ""
    elif level in {"native", "adapter_available"}:
        tier, reason = "native", ""
    elif level or complete:
        # Older scans labelled every site without a native adapter
        # "unsupported"; setup takes those to tier 2.
        tier, reason = "agentic", ""
    else:
        tier, reason = "not_checked", ""
    return {"id": tier, "rank": TIER_RANKS.get(tier), "label": TIER_LABELS[tier], "reason": reason}


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
    # Older inventory snapshots keep the detected framework inside tech_stack.
    legacy_framework = mapping(inventory.get("tech_stack")).get("framework")
    if not isinstance(legacy_framework, str) or len(legacy_framework) > 64:
        legacy_framework = None
    result = {"version": 1, "complete": inventory.get("discovery_complete") is True or payload.get("scan_complete") is True or normalized.get("discovery_complete") is True,
        "sourceSha": inventory.get("source_sha") or inventory.get("repo_head_sha") or payload.get("repo_head_sha") or payload.get("commit_sha") or normalized.get("source_sha") or None,
        "githubRepo": inventory.get("github_repo") or payload.get("github_repo") or "",
        "repositoryId": inventory.get("repository_id") or payload.get("repository_id"),
        "branch": inventory.get("branch") or payload.get("default_branch") or payload.get("branch") or "",
        "appRoot": inventory.get("app_root") or payload.get("app_root") or "",
        "framework": inventory.get("detected_framework") or inventory.get("framework")
            or payload.get("detected_framework") or payload.get("framework") or normalized.get("framework") or legacy_framework,
        "resolution": resolution, "candidates": candidates, "publishReady": False,
        "supportPaths": support_paths}
    complete = result["complete"]
    content_source = content_source_snapshot(inventory.get("content_source") or normalized.get("content_source")
                                             or payload.get("content_source"))
    tier = setup_tier(normalized, content_source, complete=complete)
    # Plan C3: tier wording, never "unsupported" (older scans said so for any
    # site without a native adapter, which only means setup goes to tier 2).
    result.update(supportLevel=tier["id"], setupTier=tier, contentSource=content_source)
    from .website_site_fingerprint import project, framework_hint
    fingerprint = project(inventory.get('site_fingerprint'), github_repo=result['githubRepo'],
        source_sha=result['sourceSha'], branch=result['branch'], app_root=result['appRoot'])
    if fingerprint:
        result['siteFingerprint'] = fingerprint
        result['framework'] = framework_hint(fingerprint) or result['framework']
    return result


def normalize_scan_callback(value):
    """Promote inventory metadata for presentation while preserving its boundary."""
    data = scan_payload(value)
    discovery = discovery_snapshot(data)
    data.setdefault("default_branch", discovery["branch"])
    data.setdefault("repo_head_sha", discovery["sourceSha"] or "")
    data.setdefault("article_surface_resolution", discovery["resolution"])
    data.setdefault("detected_candidates", discovery["candidates"])
    return data
