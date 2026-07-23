from __future__ import annotations

from datetime import timezone as datetime_timezone
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from content_factory.article_system import default_article_system


def _parse_reset_timestamp(value: Any):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        parsed = value
    else:
        parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def article_setup_reset_at(config):
    raw = getattr(config, "article_system", None)
    article_system = raw if isinstance(raw, dict) else {}
    return _parse_reset_timestamp(
        article_system.get("article_setup_reset_at")
        or article_system.get("articleSetupResetAt")
        or (article_system.get("article_setup_reset") or {}).get("resetAt")
        or (article_system.get("article_setup_reset") or {}).get("reset_at")
    )


def article_setup_reset_excluded_run_ids(config) -> set[str]:
    """Run ids tombstoned by the most recent reset.

    The timestamp watermark (``article_setup_reset_at``) is fragile: a run's
    ``updated_at`` can move past it, and a routine re-scan can drop the marker
    entirely. Recording the exact run ids that existed at reset time makes the
    reset survive both — those runs stay ignored regardless of timestamps.
    """
    raw = getattr(config, "article_system", None)
    article_system = raw if isinstance(raw, dict) else {}
    reset = article_system.get("article_setup_reset")
    reset = reset if isinstance(reset, dict) else {}
    ids = reset.get("excludedRunIds") or reset.get("excluded_run_ids") or []
    if not isinstance(ids, (list, tuple, set)):
        return set()
    return {str(run_id).strip() for run_id in ids if str(run_id or "").strip()}


ARTICLE_SETUP_RESET_KEYS = ("article_setup_reset", "article_setup_reset_at", "articleSetupResetAt")

# Fields the *deep* reset additionally restores to their model defaults. The shallow
# reset leaves these, which made a "reset" still reuse old components, short-circuit the
# next scan (stale sha/fingerprint), and inherit stale repo classification + design memory.
ARTICLE_SETUP_DEEP_RESET_FIELDS = (
    "article_system_setup_cache",
    "framework_component_specs",
    "last_scanned_sha",
    "last_scanned_at",
    "scan_request_fingerprint",
    "scan_summary",
    "tech_stack",
    "installed_packages",
    "repo_execution_contract",
    "build_healing_hints",
    "visual_context",
    "renderer_style_profile",
    "reference_screenshots",
    "directory_style_feedback",
)


def carry_reset_markers(source, target):
    """Copy the reset watermark + tombstone keys from ``source`` onto ``target``.

    article_system gets re-normalized whenever a scan persists org config, which
    drops these keys and silently un-resets the articles setup. Call this when
    re-writing article_system to keep an existing reset intact.
    """
    if not isinstance(source, dict) or not isinstance(target, dict):
        return target
    for key in ARTICLE_SETUP_RESET_KEYS:
        if key in source and key not in target:
            target[key] = source[key]
    return target


def article_setup_reset_marker(article_system) -> str:
    """The reset watermark, if the founder has explicitly reset this setup.

    Reads the *stored* article_system: ``normalize_article_system`` drops these
    non-template keys, so callers holding a resolved/normalized article_system must
    pass ``config.article_system`` (the raw field) here, not the normalized copy.
    """
    if not isinstance(article_system, dict):
        return ""
    for key in ("article_setup_reset_at", "articleSetupResetAt"):
        value = str(article_system.get(key) or "").strip()
        if value:
            return value
    info = article_system.get("article_setup_reset")
    if isinstance(info, dict):
        return str(info.get("reset_at") or info.get("resetAt") or "").strip()
    return ""


def clear_article_setup_reset_markers(article_system):
    if not isinstance(article_system, dict):
        return article_system
    for key in ARTICLE_SETUP_RESET_KEYS:
        article_system.pop(key, None)
    return article_system


def article_setup_reset_ignores_run(config, run) -> bool:
    if not run or getattr(run, "workflow", "") != "article_system_setup":
        return False
    # Tombstoned by run id: durable against updated_at changes and marker loss.
    run_id = str(getattr(run, "run_id", "") or "").strip()
    if run_id and run_id in article_setup_reset_excluded_run_ids(config):
        return True
    reset_at = article_setup_reset_at(config)
    if reset_at is None:
        return False
    run_timestamp = getattr(run, "updated_at", None) or getattr(run, "created_at", None)
    if run_timestamp is None:
        return False
    if timezone.is_naive(run_timestamp):
        run_timestamp = timezone.make_aware(run_timestamp, datetime_timezone.utc)
    return run_timestamp <= reset_at


def _existing_article_system_setup_run_ids(config) -> list[str]:
    """Every article_system_setup run id for this org at reset time."""
    organization = getattr(config, "organization", None)
    domain = str(getattr(organization, "domain", "") or "").strip()
    if not domain:
        return []
    try:
        from workflow_runs.models import ContentFactoryRun
    except Exception:
        return []
    try:
        run_ids = (
            ContentFactoryRun.objects.filter(domain__iexact=domain, workflow="article_system_setup")
            .values_list("run_id", flat=True)
        )
    except Exception:
        return []
    seen: list[str] = []
    for run_id in run_ids:
        normalized = str(run_id or "").strip()
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def _setup_owner_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("setup_run_id")
        or payload.get("setupRunId")
        or payload.get("source_setup_run_id")
        or payload.get("sourceSetupRunId")
        or ""
    ).strip()


def clear_cancelled_article_setup_config(config, *, setup_run_id: str) -> dict:
    """Clear setup state owned by one cancelled build without resetting the repo.

    An organization may already have a working blog/articles target while it
    builds a second `/articles` scaffold. Cancellation must remove only the
    pending/cache/targets created by the matching setup run, never the existing
    publishing configuration.
    """

    owner_id = str(setup_run_id or "").strip()
    if not owner_id:
        return {"changed": False, "cleared_fields": [], "removed_target_ids": []}

    update_fields: list[str] = []
    cleared_fields: list[str] = []
    removed_target_ids: list[str] = []

    raw_article_system = config.article_system if isinstance(getattr(config, "article_system", None), dict) else {}
    article_system = dict(raw_article_system)
    pending = article_system.get("pending_article_system_setup")
    pending_owned = _setup_owner_id(pending) == owner_id
    if pending_owned:
        article_system.pop("pending_article_system_setup", None)
        config.article_system = article_system
        update_fields.append("article_system")
        cleared_fields.append("pending_article_system_setup")

    setup_cache = (
        config.article_system_setup_cache
        if isinstance(getattr(config, "article_system_setup_cache", None), dict)
        else {}
    )
    cache_owned = _setup_owner_id(setup_cache) == owner_id
    if cache_owned:
        config.article_system_setup_cache = {}
        update_fields.append("article_system_setup_cache")
        cleared_fields.append("article_system_setup_cache")

    targets = config.publish_targets if isinstance(getattr(config, "publish_targets", None), list) else []
    kept_targets = []
    for target in targets:
        if not isinstance(target, dict):
            kept_targets.append(target)
            continue
        target_owned = _setup_owner_id(target) == owner_id
        scaffold_cache_owned = cache_owned and str(target.get("source") or "").strip() == "scaffold_cache"
        if target_owned or scaffold_cache_owned:
            target_id = str(target.get("target_id") or target.get("targetId") or "").strip()
            if target_id:
                removed_target_ids.append(target_id)
            continue
        kept_targets.append(target)
    if kept_targets != targets:
        config.publish_targets = kept_targets
        update_fields.append("publish_targets")
        cleared_fields.append("publish_targets")
        if str(getattr(config, "default_publish_target_id", "") or "").strip() in removed_target_ids:
            config.default_publish_target_id = None
            update_fields.append("default_publish_target_id")
            cleared_fields.append("default_publish_target_id")

    if update_fields:
        update_fields.append("updated_at")
        config.save(update_fields=list(dict.fromkeys(update_fields)))

    return {
        "changed": bool(update_fields),
        "cleared_fields": cleared_fields,
        "removed_target_ids": removed_target_ids,
        "setup_run_id": owner_id,
    }


def reset_article_setup_config(config, *, github_repo: str = "", deep: bool = False) -> dict:
    reset_at = timezone.now()
    reset_at_iso = reset_at.isoformat()
    raw_article_system = config.article_system if isinstance(getattr(config, "article_system", None), dict) else {}
    scan_state = raw_article_system.get("scan") if isinstance(raw_article_system.get("scan"), dict) else {}
    excluded_run_ids = _existing_article_system_setup_run_ids(config)
    repo_value = str(github_repo or getattr(config, "github_repo", "") or "").strip()

    next_article_system = default_article_system()
    if scan_state:
        next_article_system["scan"] = scan_state
    next_article_system["article_setup_reset_at"] = reset_at_iso
    next_article_system["articleSetupResetAt"] = reset_at_iso
    next_article_system["article_setup_reset"] = {
        "resetAt": reset_at_iso,
        "reset_at": reset_at_iso,
        "githubRepo": repo_value,
        "github_repo": repo_value,
        "excludedRunIds": excluded_run_ids,
        "excluded_run_ids": excluded_run_ids,
    }

    update_fields = []
    cleared_fields = []

    if config.article_system != next_article_system:
        config.article_system = next_article_system
        update_fields.append("article_system")
        cleared_fields.append("article_system")
    if config.publish_targets:
        config.publish_targets = []
        update_fields.append("publish_targets")
        cleared_fields.append("publish_targets")
    if config.default_publish_target_id:
        config.default_publish_target_id = None
        update_fields.append("default_publish_target_id")
        cleared_fields.append("default_publish_target_id")
    if config.articles_scaffolded:
        config.articles_scaffolded = False
        update_fields.append("articles_scaffolded")
        cleared_fields.append("articles_scaffolded")
    if config.articles_scaffold_pr_url:
        config.articles_scaffold_pr_url = None
        update_fields.append("articles_scaffold_pr_url")
        cleared_fields.append("articles_scaffold_pr_url")
    if config.articles_scaffold_preview_url:
        config.articles_scaffold_preview_url = None
        update_fields.append("articles_scaffold_preview_url")
        cleared_fields.append("articles_scaffold_preview_url")
    # Reset the content/registry path patterns to their model defaults so a re-scaffold
    # re-derives them from the fresh repo instead of inheriting a stale pattern (e.g. a prior
    # repo's `app/articles/...` when the new target keeps its system at the repo root).
    for _path_field in ("article_path_pattern", "registry_path"):
        _default_value = config._meta.get_field(_path_field).get_default()
        if getattr(config, _path_field) != _default_value:
            setattr(config, _path_field, _default_value)
            update_fields.append(_path_field)
            cleared_fields.append(_path_field)

    # Deep reset: also restore the scan/reuse/design caches the shallow reset leaves
    # behind, so a re-scaffold re-derives everything from a fresh scan instead of reusing
    # stale components, short-circuiting the next scan, or inheriting stale design memory.
    if deep:
        for _cache_field in ARTICLE_SETUP_DEEP_RESET_FIELDS:
            _default_value = config._meta.get_field(_cache_field).get_default()
            if getattr(config, _cache_field) != _default_value:
                setattr(config, _cache_field, _default_value)
                update_fields.append(_cache_field)
                cleared_fields.append(_cache_field)

    if update_fields:
        update_fields.append("updated_at")
        config.save(update_fields=update_fields)

    # Deep reset: delete the article_system_setup runs (tombstoning alone left them
    # resolvable, resurfacing phantom wizard state) and drop persisted design snapshots.
    # Best-effort + reported. Deleting the runs is safe even though the latest scan
    # result may still reference them: the bootstrap gate drops dangling run refs (#474).
    deleted_setup_runs = 0
    dropped_design_snapshots = 0
    if deep:
        organization = getattr(config, "organization", None)
        domain = str(getattr(organization, "domain", "") or "").strip()
        if domain:
            try:
                from workflow_runs.models import ContentFactoryRun

                _deleted = ContentFactoryRun.objects.filter(
                    domain__iexact=domain, workflow="article_system_setup"
                ).delete()
                deleted_setup_runs = _deleted[1].get(ContentFactoryRun._meta.label, 0)
            except Exception:
                deleted_setup_runs = 0
        if organization is not None:
            try:
                from content_factory.models import WebsiteDesignSnapshot

                _dropped = WebsiteDesignSnapshot.objects.filter(organization=organization).delete()
                dropped_design_snapshots = _dropped[1].get(WebsiteDesignSnapshot._meta.label, 0)
            except Exception:
                dropped_design_snapshots = 0

    return {
        "status": "reset",
        "changed": bool(update_fields),
        "deep": bool(deep),
        "clearedFields": cleared_fields,
        "cleared_fields": cleared_fields,
        "deletedSetupRuns": deleted_setup_runs,
        "deleted_setup_runs": deleted_setup_runs,
        "droppedDesignSnapshots": dropped_design_snapshots,
        "dropped_design_snapshots": dropped_design_snapshots,
        "resetAt": reset_at_iso,
        "reset_at": reset_at_iso,
        "excludedRunIds": excluded_run_ids,
        "excluded_run_ids": excluded_run_ids,
        "githubRepo": repo_value,
        "github_repo": repo_value,
    }
