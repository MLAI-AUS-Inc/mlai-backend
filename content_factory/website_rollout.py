"""Operator kill switch and exact-domain canary policy for repository writes."""

import os
import re

from .website_contract import WebsiteAuthorityError


WRITE_ACTIONS = frozenset({"setup", "publish", "merge", "preview", "cleanup"})


def repository_write_policy(domain, *, environ=None):
    """Return a fail-closed rollout decision without granting connection consent."""
    env = os.environ if environ is None else environ
    mode = str(env.get("WEBSITE_CONNECTION_WRITE_MODE", "enabled")).strip().lower()
    domain = str(domain or "").strip().lower().rstrip(".")
    canaries = {
        item.strip().lower().rstrip(".")
        for item in str(env.get("WEBSITE_CONNECTION_CANARY_DOMAINS", "")).split(",")
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", item.strip())
    }
    allowed = mode == "enabled" or (mode == "canary" and bool(domain) and domain in canaries)
    return {
        "mode": mode if mode in {"enabled", "disabled", "canary"} else "invalid",
        "allowed": allowed,
        "code": "" if allowed else "website_writes_paused",
        "message": "" if allowed else "Website changes are temporarily paused. You can still inspect the repository, export content or disconnect.",
    }


def require_repository_write_policy(action, domain):
    """Deny new repository/deployment writes during a pause or outside a canary."""
    if action not in WRITE_ACTIONS:
        return
    policy = repository_write_policy(domain)
    if not policy["allowed"]:
        raise WebsiteAuthorityError(policy["code"], policy["message"], status=409)


def apply_repository_write_policy(summary, domain):
    """Project the same operator restriction into the canonical client summary."""
    policy = repository_write_policy(domain)
    result = {**summary, "writePolicy": {"mode": policy["mode"], "allowed": policy["allowed"]}}
    if policy["allowed"]:
        return result
    result["capabilities"] = {**summary.get("capabilities", {}), "publishingReady": False, "previewSupported": False}
    result["allowedActions"] = [action for action in summary.get("allowedActions", []) if action not in WRITE_ACTIONS]
    result["blockers"] = [*summary.get("blockers", []), {"code": policy["code"], "message": policy["message"]}]
    return result
