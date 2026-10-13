"""Shared setup merge decisions; GitHub remains the final protection enforcer."""

from urllib.parse import quote

from integrations import http_client


class GitHubResponseError(ValueError):
    """Keep provider status available when a GitHub JSON error has a message."""

    def __init__(self, message, *, status_code):
        super().__init__(message)
        self.status_code = status_code


def _reviews_required(parameters):
    if not isinstance(parameters, dict):
        raise ValueError("Invalid GitHub review policy")
    return bool(
        parameters.get("required_approving_review_count", 0)
        or parameters.get("require_code_owner_review")
        or parameters.get("require_code_owner_reviews")
        or parameters.get("require_last_push_approval")
    )


def _rate_limited(response):
    headers = response.headers
    return response.status_code == 429 or (response.status_code == 403 and (
        headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"))


def setup_merge_policy(website, pr, *, token, checks, requires_review=False):
    """Return merge, needs_user, or wait using current owner and GitHub policy.

    Branch rules need Metadata read. Classic protection needs Administration
    read, which repository-scoped credentials deliberately do not have. A
    403/404 there means unknown policy; it never grants a protection bypass.
    Every merge still uses the separately validated, exact PR head SHA.
    """
    if requires_review:
        return {"decision": "needs_user", "reason": "Review and merge the setup pull request."}
    if pr.get("state") != "open":
        return {"decision": "needs_user", "reason": "The setup pull request is no longer open."}
    branch = quote(website.branch, safe="")
    base = f"https://api.github.com/repos/{website.github_repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}
    try:
        # Do not silently miss an organisation ruleset on a later page.
        for page in range(1, 11):
            rules = http_client.get(f"{base}/rules/branches/{branch}", headers=headers,
                params={"per_page": 100, "page": page}, timeout=(3, 15))
            if _rate_limited(rules):
                return {"decision": "wait", "reason": "Waiting for GitHub's rate limit to clear."}
            if rules.status_code in {403, 404}:
                break  # Unknown policy: the merge endpoint enforces it.
            rules.raise_for_status()
            inventory = rules.json()
            if not isinstance(inventory, list) or any(not isinstance(rule, dict) for rule in inventory):
                return {"decision": "wait", "reason": "Waiting for GitHub branch policy."}
            if any(rule.get("type") == "pull_request" and _reviews_required(rule.get("parameters") or {})
                    for rule in inventory):
                return {"decision": "needs_user", "reason": "GitHub requires a reviewer for this setup pull request."}
            if len(inventory) < 100:
                break
        else:
            return {"decision": "wait", "reason": "GitHub branch policy is too large to inspect in one pass."}
        protection = http_client.get(f"{base}/branches/{branch}/protection", headers=headers, timeout=(3, 15))
        if _rate_limited(protection):
            return {"decision": "wait", "reason": "Waiting for GitHub's rate limit to clear."}
        if protection.status_code not in {403, 404}:
            protection.raise_for_status()
            payload = protection.json()
            if not isinstance(payload, dict):
                return {"decision": "wait", "reason": "Waiting for GitHub branch policy."}
            if _reviews_required(payload.get("required_pull_request_reviews") or {}):
                return {"decision": "needs_user", "reason": "GitHub requires a reviewer for this setup pull request."}
    except (http_client.RequestException, ValueError, TypeError):
        return {"decision": "wait", "reason": "GitHub branch policy is temporarily unavailable."}
    if str(checks.get("state") or "").lower() in {"pending", "queued", "in_progress", "waiting"}:
        return {"decision": "wait", "reason": checks.get("message") or "Waiting for GitHub checks."}
    return {"decision": "merge", "reason": "GitHub will enforce repository protection at merge."}


def setup_merge_refusal(error):
    """Distinguish a provider wait from a merge requiring an owner's action."""
    message = str(error or "GitHub did not allow this setup pull request to merge.")
    lowered = message.lower()
    status_code = getattr(error, "status_code", 0)
    if status_code == 429 or status_code >= 500 or isinstance(error, http_client.RequestException) or any(fragment in lowered for fragment in (
        "status check", "checks", "pending", "rate limit", "temporarily", "service unavailable",
        "github returned 5", "github returned 429", "mergeability", "not mergeable yet",
    )):
        return {"decision": "wait", "reason": message}
    return {"decision": "needs_user", "reason": message}
