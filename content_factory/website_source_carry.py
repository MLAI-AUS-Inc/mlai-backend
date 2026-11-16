"""Keep a website Ready across a push that leaves MLAI's articles files alone.

Before this, every push to the founder's branch (theirs, their developer's, or
MLAI merging an article) put articles back to "checking": drafting waited on
a new paid scan and publishing on fresh CI and host proof at the new commit.

With ``ARTICLES_PROOF_CARRY_FORWARD_ENABLED`` (a kill switch, default on), a
push carries the verified commit forward when one GitHub compare shows that:

- the new commit is a fast-forward of the verified one ("ahead");
- GitHub listed every changed file (no truncated comparison); and
- no file the articles setup owns or routes through (store, route shells,
  detail template, registry; recorded in ``article_system_setup_cache``) was
  changed, removed or renamed. MLAI's own article merges may change the
  registry and files under the articles content folder, since adding an
  article is exactly that.

The background source re-verification still runs, free of charge (no scan),
and only a failed re-check locks articles again.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# GitHub's compare API lists at most 300 files; a longer list is truncated.
COMPARE_FILE_LIMIT = 300

_PATH_LIST_KEYS = ("managed_files", "managed_file_paths", "support_paths", "route_paths")
_PATH_KEYS = (
    "route_shell_path", "served_route_shell_path", "registry_path", "seo_config_path",
    "data_store_path", "detail_route_shell_path", "detail_template_path",
)
_REGISTRY_NAMES = ("registry.ts", "registry.tsx", "registry.js", "registry.mjs", "registry.json")


def enabled() -> bool:
    """Kill switch, default on."""
    return bool(getattr(settings, "ARTICLES_PROOF_CARRY_FORWARD_ENABLED", True))


def _normalize(path: Any) -> str:
    if isinstance(path, dict):
        path = path.get("path") or path.get("file_path") or ""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def setup_paths(cache: Any) -> list[str]:
    """Files the articles setup owns or routes through, from the saved setup cache.

    Mirrors Content Factory's ``_article_setup_cache_managed_files`` without the
    founder's own read-only native sources, so an ordinary refactor of their
    site never counts as a change to MLAI's files.
    """
    payload = cache if isinstance(cache, dict) else {}
    raw: list[Any] = []
    for key in _PATH_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            raw.extend(value)
        elif isinstance(value, dict):
            raw.extend(value.values())
        elif isinstance(value, str):
            raw.append(value)
    raw.extend(payload.get(key) for key in _PATH_KEYS if isinstance(payload.get(key), str))
    seen: list[str] = []
    for item in raw:
        path = _normalize(item)
        if path and path not in seen:
            seen.append(path)
    return seen


def _article_content_roots(cache: Any, paths: Iterable[str]) -> list[str]:
    """Folders an MLAI article merge writes into: the registry's folder and the content root."""
    payload = cache if isinstance(cache, dict) else {}
    roots = []
    for path in paths:
        if path.rsplit("/", 1)[-1] in _REGISTRY_NAMES and "/" in path:
            roots.append(path.rsplit("/", 1)[0])
    for key in ("content_path", "content_dir", "content_root_path"):
        value = _normalize(payload.get(key))
        if "/" in value:
            roots.append(value)
    return sorted(set(roots))


def _touches(changed: dict[str, str], protected: Iterable[str]) -> list[str]:
    protected = set(protected)
    hits = set()
    for filename, previous in changed.items():
        if filename in protected:
            hits.add(filename)
        if previous and previous in protected:
            hits.add(previous)
    return sorted(hits)


def decide(cache: Any, comparison: dict[str, Any], *, owned_merge: bool) -> dict[str, Any]:
    """Pure decision from a GitHub compare payload. Returns ``{"carry": bool, "reason": str, ...}``."""
    paths = setup_paths(cache)
    if not paths:
        return {"carry": False, "reason": "no_setup_record"}
    if comparison.get("status") not in {"ahead", "identical"}:
        return {"carry": False, "reason": "not_fast_forward"}
    files = comparison.get("files")
    if not isinstance(files, list):
        return {"carry": False, "reason": "compare_unreadable"}
    total = comparison.get("total_commits")
    if len(files) >= COMPARE_FILE_LIMIT:
        return {"carry": False, "reason": "compare_truncated"}
    changed: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            return {"carry": False, "reason": "compare_unreadable"}
        name = _normalize(item.get("filename"))
        if not name:
            return {"carry": False, "reason": "compare_unreadable"}
        changed[name] = _normalize(item.get("previous_filename")) if item.get("status") == "renamed" else ""
    hits = _touches(changed, paths)
    if hits and owned_merge:
        roots = _article_content_roots(cache, paths)
        hits = [path for path in hits if not any(path == root or path.startswith(root + "/") for root in roots)
                and path.rsplit("/", 1)[-1] not in _REGISTRY_NAMES]
    if hits:
        return {"carry": False, "reason": "setup_files_changed", "changed_setup_files": hits[:20]}
    return {"carry": True, "reason": "setup_files_unchanged", "changed_files": len(changed),
            "commits": total if isinstance(total, int) else None}


def compare_sources(website, from_sha: str, to_sha: str) -> dict[str, Any] | None:
    """Read GitHub's comparison with a short-lived read token; None when it can't be read."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token

    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        credential = create_installation_access_token(installation_id=website.installation_id,
            repository=website.github_repo, repository_id=website.repository_id, permission_mode="read", use_cache=False)
    except Exception:
        logger.warning("source_carry_token_unavailable website=%s", website.pk, exc_info=True)
        return None
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    try:
        response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/compare/{from_sha}...{to_sha}",
            headers=headers, timeout=(3, 15))
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("source_carry_compare_unavailable website=%s", website.pk, exc_info=True)
        return None
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            logger.debug("source_carry_token_revoke_failed website=%s", website.pk, exc_info=True)


def carry_decision(website, config, source_sha: str, *, owned_merge: bool) -> dict[str, Any]:
    """Whether this push can keep the current proof; read outside any authority lock."""
    from .website_contract import SHA_PATTERN
    if not enabled():
        return {"carry": False, "reason": "disabled"}
    from_sha = str(website.verified_sha or "")
    if not SHA_PATTERN.fullmatch(from_sha) or not SHA_PATTERN.fullmatch(str(source_sha or "")):
        return {"carry": False, "reason": "no_verified_source"}
    cache = getattr(config, "article_system_setup_cache", None)
    if not setup_paths(cache):
        return {"carry": False, "reason": "no_setup_record"}
    target = _default_target(website, getattr(config, "default_publish_target_id", "") or "")
    if target is None or target.source_sha != from_sha or not carriable(website, target):
        return {"carry": False, "reason": "proof_not_carriable"}
    comparison = compare_sources(website, from_sha, source_sha)
    if comparison is None:
        return {"carry": False, "reason": "compare_unreadable"}
    return {**decide(cache, comparison, owned_merge=owned_merge), "from_sha": from_sha}


def carriable(website, target) -> bool:
    """Whether ``target``'s Ready rests on MLAI's own build proof alone, so it can be carried.

    A target that needs the founder's sealed CI proof at the exact commit keeps
    the old push-time lock: that proof can only come from their CI run.
    """
    from .website_live_status import enabled as ready_on_merge
    from .website_live_verification import build_proof
    from .website_sealed_workflow import required
    return bool(target is not None and ready_on_merge() and build_proof(target) and not required(website, target))


def _default_target(website, target_key: str):
    if not target_key:
        return None
    return website.targets.filter(generation=website.generation, target_key=target_key).first()


def apply_carry(website, target_key: str, source_sha: str, decision: dict[str, Any]) -> bool:
    """Move the verified commit (and its target's build proof) forward inside the caller's lock.

    The target keeps its original ``verified_at`` so the 7-day proof age still
    applies; ``proofCarriedFrom`` (on the target and in its verification)
    records honestly that this commit was not itself rebuilt. Returns False
    when the locked rows no longer match or the proof can't be carried.
    """
    from_sha = decision.get("from_sha")
    if not decision.get("carry") or not from_sha or website.verified_sha != from_sha:
        return False
    target = _default_target(website, target_key)
    if target is None or target.source_sha != from_sha or not carriable(website, target):
        return False
    contract = dict(target.contract or {})
    contract["verification"] = {**dict(contract.get("verification") or {}), "source_sha": source_sha,
        "carried_from": from_sha}
    target.source_sha = source_sha
    target.contract = contract
    target.capabilities = {**(target.capabilities or {}), "proofCarriedFrom": from_sha}
    target.save(update_fields=["source_sha", "contract", "capabilities", "updated_at"])
    website.verified_sha = source_sha
    return True


def recheck_carried(website, target, source_sha: str) -> dict[str, Any]:
    """The free background re-check of a carried proof: GitHub's branch still points at it.

    Raises ``WebsiteAuthorityError`` (``website_source_changed`` when a newer
    push moved the branch, which queued its own check) like every source check.
    """
    from .website_connections import (
        require_unlocked_remote_call,
        verify_repository_head,
    )
    from .website_contract import WebsiteAuthorityError
    if not carriable(website, target) or target.source_sha != source_sha:
        raise WebsiteAuthorityError("verified_target_required", "Waiting for a current verified publishing target.", retryable=False)
    require_unlocked_remote_call()
    verify_repository_head(website, source_sha)
    return {"method": "carried_build_proof", "carried_from": str((target.capabilities or {}).get("proofCarriedFrom") or "")}


# A carried proof's background re-check may retry for this long before the
# carry is withdrawn; host lag is already renewed on MLAI's build proof.
RECHECK_WINDOW_HOURS = 72


def recheck_failed(op, receipt: dict[str, Any], *, now) -> bool:
    """Whether a carried source's re-check has failed for good."""
    from datetime import timedelta
    if receipt.get("retryable") is False or receipt.get("status") == "needs_attention":
        return True
    return bool(op.created_at and now - op.created_at >= timedelta(hours=RECHECK_WINDOW_HOURS))


def lock_carried_source(op) -> bool:
    """Withdraw a carried proof: the old push-time lock, applied after the failed re-check."""
    from django.db import transaction

    from .website_models import WebsiteConnection
    source_sha = str(op.payload.get("source_sha") or "")
    with transaction.atomic():
        website = WebsiteConnection.objects.select_for_update().filter(pk=op.connection_id).first()
        if website is None or website.verified_sha != source_sha or website.generation != op.generation:
            # A newer push or a fresh verification owns the connection now.
            return False
        website.capabilities = {**(website.capabilities or {}), "publishingReady": False, "previewSupported": False}
        website.blockers = [item for item in website.blockers if item.get("code") != "repository_source_changed"] + [{
            "code": "repository_source_changed", "source_sha": source_sha,
            "message": "The repository changed and its re-check didn't pass. Scan and verify the current source before publishing."}]
        website.save(update_fields=["capabilities", "blockers", "updated_at"])
    logger.warning("source_carry_withdrawn website=%s source=%s", op.connection_id, source_sha)
    return True
