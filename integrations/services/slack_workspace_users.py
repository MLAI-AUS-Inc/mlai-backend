"""Shared, sanitized Slack workspace directory for fast mention searches.

The bridge worker reads one Slack page per turn. A complete snapshot is
published only after the final page, so searches never mistake a partial
directory for all the people in the workspace.
"""

import hashlib
import re
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.message_sync.scheduler import BudgetDeferred

SNAPSHOT_TTL_SECONDS = 3600
REFRESH_SECONDS = 900
PAGE_LIMIT = 200


def configured_scope():
    """Return the configured workspace and credential-bound public scope."""
    workspace = str(
        getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "") or ""
    ).strip()
    token = str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip()
    if not re.fullmatch(r"T[A-Z0-9]+", workspace) or not token:
        return None
    return workspace, hashlib.sha256(f"{workspace}:{token}".encode()).hexdigest()


def cache_key(scope, category, value=""):
    """Keep metadata from separate Slack installations in separate keys."""
    return (
        "slack-community-directory-v1:"
        + hashlib.sha256(f"{scope}:{category}:{value}".encode()).hexdigest()
    )


def cached_workspace_snapshot(workspace, *, version=""):
    """Return a complete snapshot, including a prior version during pagination."""
    configured = configured_scope()
    if configured is None or configured[0] != workspace:
        return None
    snapshot = cache.get(cache_key(configured[1], "snapshot", version))
    if (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("users"), list)
        and isinstance(snapshot.get("version"), str)
    ):
        return snapshot
    return None


def cached_workspace_users(workspace):
    """Return only the current complete directory for this workspace."""
    snapshot = cached_workspace_snapshot(workspace)
    return snapshot["users"] if snapshot else None


def warm_workspace_directory_once():
    """Read at most one users.list page, retaining a prior complete snapshot.

    The cache lock is shared by bridge worker replicas. A budget deferral or
    provider error only postpones this independent lane; the last complete
    snapshot stays available for searches.
    """
    configured = configured_scope()
    if configured is None:
        return 0
    workspace, scope = configured
    snapshot_key = cache_key(scope, "snapshot")
    progress_key = cache_key(scope, "progress")
    retry_key = cache_key(scope, "retry")
    lock_key = cache_key(scope, "warm-lock")
    now = time.time()
    snapshot = cache.get(snapshot_key)
    if (
        isinstance(snapshot, dict)
        and now - float(snapshot.get("completed_at") or 0) < REFRESH_SECONDS
        and not cache.get(progress_key)
    ):
        return 0
    if float(cache.get(retry_key) or 0) > now:
        return 0
    lock_value = uuid.uuid4().hex
    if not cache.add(lock_key, lock_value, timeout=60):
        return 0
    try:
        progress = cache.get(progress_key) or {"cursor": "", "users": []}
        cursor = str(progress.get("cursor") or "")
        client = SlackBridgeClient.get_client()
        verified_key = cache_key(scope, "workspace")
        if not cache.get(verified_key):
            identity = client.auth_test()
            if identity.get("team_id") != workspace:
                raise ValueError("slack_directory_workspace_mismatch")
            cache.set(verified_key, True, timeout=600)
        response = client.users_list(limit=PAGE_LIMIT, cursor=cursor)
        next_cursor = str(
            (response.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if next_cursor and next_cursor == cursor:
            raise ValueError("slack_directory_cursor_stalled")

        # Use the same narrow serialization as the request path. No emails or
        # raw Slack profile objects enter shared cache.
        from integrations.services.slack_mentions import sanitized_directory_page

        page = sanitized_directory_page(response, workspace)
        cache.set(cache_key(scope, "users", cursor), page, timeout=SNAPSHOT_TTL_SECONDS)
        existing = progress.get("users") or []
        known = {user["slack_user_id"] for user in existing}
        users = list(existing)
        for user in page["users"]:
            if user["slack_user_id"] not in known:
                users.append(user)
                known.add(user["slack_user_id"])
        if next_cursor:
            cache.set(
                progress_key,
                {"cursor": next_cursor, "users": users},
                timeout=SNAPSHOT_TTL_SECONDS,
            )
        else:
            version = uuid.uuid4().hex
            complete = {
                "users": users,
                "version": version,
                "completed_at": time.time(),
            }
            cache.set(
                cache_key(scope, "snapshot", version),
                complete,
                timeout=SNAPSHOT_TTL_SECONDS,
            )
            cache.set(
                snapshot_key, complete,
                timeout=SNAPSHOT_TTL_SECONDS,
            )
            cache.delete(progress_key)
        return 1
    except BudgetDeferred as exc:
        cache.set(retry_key, time.time() + exc.retry_after, timeout=exc.retry_after)
        return 0
    except Exception:
        cache.set(retry_key, time.time() + 60, timeout=60)
        raise
    finally:
        if cache.get(lock_key) == lock_value:
            cache.delete(lock_key)
