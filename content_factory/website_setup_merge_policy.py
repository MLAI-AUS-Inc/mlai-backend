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


VERIFICATION_WORKFLOW_PATH = ".github/workflows/mlai-articles-verification.yml"
VERIFICATION_RUNNER_PREFIX = ".mlai/articles-verification."
# The kit workflow's job id. GitHub names an unnamed job's check run after it.
VERIFICATION_JOB_CHECK = "verify"
# Recorded on a setup whose merge this kit's own check blocked.
VERIFICATION_KIT_CHECKS = "verification_kit"


def _paths(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def setup_pr_adds_verification_kit(run):
    """Whether this setup PR writes the MLAI verification workflow or its runner."""
    result = run.result if isinstance(getattr(run, "result", None), dict) else {}
    setup = result.get("article_system_setup")
    for item in (result, setup if isinstance(setup, dict) else {}):
        if item.get("verification_kit_required") is True:
            return True
        adoption = item.get("adoption") if isinstance(item.get("adoption"), dict) else {}
        paths = [*_paths(adoption.get("kit_paths")), *_paths(item.get("files_created")), *_paths(item.get("files_updated"))]
        if any(path == VERIFICATION_WORKFLOW_PATH or path.startswith(VERIFICATION_RUNNER_PREFIX) for path in paths):
            return True
    return False


def verification_kit_checks(website, head_sha):
    """Read the setup PR's own MLAI verification job with CI evidence credentials.

    The merge credential has no Checks read, so a queued, running or failed
    verification looked "unknown" and the merge went ahead (Vyavos PR #3).
    Returns a pending or failed checks state, or None when this exact head has
    no verification check yet. Credential and provider failures raise
    WebsiteAuthorityError so the merge stops instead of guessing.
    """
    from .website_tokens import mint_ci_evidence_token, read_ci_provider_checks
    token = mint_ci_evidence_token(installation_id=website.installation_id, repository=website.github_repo,
        repository_id=website.repository_id)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}
    try:
        payload = read_ci_provider_checks(
            f"https://api.github.com/repos/{website.github_repo}/commits/{quote(str(head_sha), safe='')}/check-runs"
            f"?check_name={VERIFICATION_JOB_CHECK}&filter=latest&per_page=100", headers=headers)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            pass
    runs = [row for row in payload["check_runs"] if row.get("name") == VERIFICATION_JOB_CHECK
        and row.get("head_sha") == head_sha and (row.get("app") or {}).get("slug") == "github-actions"]
    if any(row.get("status") == "completed" and row.get("conclusion") not in {"success", "neutral", "skipped"} for row in runs):
        return {"state": "failed", "ready": False, "source": VERIFICATION_KIT_CHECKS, "message": "MLAI articles "
            "verification failed on GitHub for this setup pull request. Try again to rebuild the setup."}
    if any(row.get("status") != "completed" for row in runs):
        return {"state": "pending", "ready": False, "message": "Waiting for MLAI articles verification to finish on GitHub."}
    return None
