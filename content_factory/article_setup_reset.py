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


def reset_article_setup_config(config, *, github_repo: str = "") -> dict:
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

    if update_fields:
        update_fields.append("updated_at")
        config.save(update_fields=update_fields)

    return {
        "status": "reset",
        "changed": bool(update_fields),
        "clearedFields": cleared_fields,
        "cleared_fields": cleared_fields,
        "resetAt": reset_at_iso,
        "reset_at": reset_at_iso,
        "excludedRunIds": excluded_run_ids,
        "excluded_run_ids": excluded_run_ids,
        "githubRepo": repo_value,
        "github_repo": repo_value,
    }
