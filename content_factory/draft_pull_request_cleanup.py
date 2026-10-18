"""Close a deleted draft article's own pull request so it is never orphaned.

Deleting a draft used to stop at any recorded PR link, even one already closed,
which left founders unable to remove stale drafts. Instead, deletion now asks
GitHub for the PR's real state and, when the PR is still open and contains only
automation commits on a branch inside the connected repository, closes it under
the website connection's cleanup authority. Branches are never deleted, and a PR
with any commit from a person is left alone for the customer to decide.
"""

import logging
import re

from integrations import http_client

from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)

# Outcomes that let the draft be deleted.
CLOSED = "closed"
ALREADY_CLOSED = "already_closed"
# MLAI has no consented access to the PR's repository, so it cannot manage the
# PR at all; keeping the draft would block deletion forever without protecting it.
NOT_ACCESSIBLE = "not_accessible"
# Outcomes that keep the draft.
MERGED = "merged"
TEAM_CHANGES = "team_changes"
CLOSE_MANUALLY = "close_manually"
GITHUB_UNAVAILABLE = "github_unavailable"

BLOCKED_DETAIL = {
    MERGED: "This article's GitHub pull request has already been merged, so it can't be deleted as a draft.",
    TEAM_CHANGES: (
        "This draft's GitHub pull request has commits from your team, so it was left open. "
        "Close it on GitHub, then delete the draft."
    ),
    CLOSE_MANUALLY: (
        "This draft has an open GitHub pull request that MLAI can't close while publishing is paused. "
        "Close it on GitHub, then delete the draft."
    ),
    GITHUB_UNAVAILABLE: "GitHub couldn't be reached to close this draft's pull request. Try deleting it again in a moment.",
}
# When several drafts in one card are kept, report the reason the user can act on first.
_BLOCKED_PRIORITY = (GITHUB_UNAVAILABLE, TEAM_CHANGES, CLOSE_MANUALLY, MERGED)

AUTOMATION_COMMIT_EMAILS = frozenset({"content-factory-bot@users.noreply.github.com"})
_PR_URL_RE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+?)/pull/(\d+)/?$")
_MAX_PULL_COMMITS = 100


def blocked_detail(reasons):
    """Return the most actionable user-facing message for kept drafts."""
    for reason in _BLOCKED_PRIORITY:
        if reason in reasons:
            return BLOCKED_DETAIL[reason]
    return BLOCKED_DETAIL[GITHUB_UNAVAILABLE]


def close_draft_pull_request(config, pr_url):
    """Make sure a draft's recorded PR is closed before the draft is deleted.

    Returns a receipt dict. ``receipt["ok"]`` is True when the draft can be
    deleted: the PR was closed here, was already closed, or lives somewhere
    MLAI has no consented access to. Otherwise ``receipt["reason"]`` says why
    the draft must be kept.
    """
    pr_url = str(pr_url or "").strip()
    match = _PR_URL_RE.match(pr_url)
    connection = getattr(config, "website_connection", None)
    if (not match or connection is None
            or match.group(1).casefold() != str(connection.github_repo or "").casefold()):
        return _receipt(pr_url, ok=True, action=NOT_ACCESSIBLE)
    repo, number = connection.github_repo, int(match.group(2))
    token, writable, failure = _repository_token(connection, repo)
    if failure:
        return _failure_receipt(pr_url, failure)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{repo}"
    try:
        response = http_client.get(f"{base}/pulls/{number}", headers=headers, timeout=(3, 15))
        failure = _response_failure(response)
        if failure:
            return _failure_receipt(pr_url, failure)
        pull = response.json() or {}
        if pull.get("merged") or pull.get("merged_at"):
            return _receipt(pr_url, reason=MERGED)
        if pull.get("state") == "closed":
            return _receipt(pr_url, ok=True, action=ALREADY_CLOSED)
        if not writable:
            return _receipt(pr_url, reason=CLOSE_MANUALLY)
        owned = _automation_only_pull(pull, repo=repo, base=base, number=number, headers=headers)
        if owned is None:
            return _receipt(pr_url, reason=GITHUB_UNAVAILABLE)
        if not owned:
            return _receipt(pr_url, reason=TEAM_CHANGES)
        response = http_client.patch(f"{base}/pulls/{number}", headers=headers, json={"state": "closed"}, timeout=(3, 15))
        if response.status_code != 200:
            logger.warning("draft_pull_request_close_failed repo=%s pr=%s status=%s", repo, number, response.status_code)
            return _receipt(pr_url, reason=GITHUB_UNAVAILABLE)
        logger.info("draft_pull_request_closed repo=%s pr=%s", repo, number)
        return _receipt(pr_url, ok=True, action=CLOSED)
    except (http_client.RequestException, ValueError):
        logger.warning("draft_pull_request_cleanup_error repo=%s pr=%s", repo, number, exc_info=True)
        return _receipt(pr_url, reason=GITHUB_UNAVAILABLE)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass


def _receipt(pr_url, *, ok=False, action="", reason=""):
    return {"prUrl": pr_url, "ok": ok, "action": action, "reason": "" if ok else reason}


def _failure_receipt(pr_url, failure):
    if failure == NOT_ACCESSIBLE:
        return _receipt(pr_url, ok=True, action=NOT_ACCESSIBLE)
    return _receipt(pr_url, reason=failure)


def _repository_token(connection, repo):
    """Return ``(token, writable, failure)`` under the connection's saved consent."""
    from .website_connections import contract_for
    from .website_tokens import mint_website_token
    data = {**contract_for(connection), "github_repo": repo}
    try:
        return mint_website_token(data, permission_mode="write", action="cleanup")["github_token"], True, ""
    except WebsiteAuthorityError as exc:
        if exc.retryable:
            return "", False, GITHUB_UNAVAILABLE
        logger.info("draft_pull_request_write_unavailable repo=%s code=%s", repo, exc.code)
    # Paused publishing or a write policy still allows confirming the PR is closed.
    try:
        return mint_website_token(data, permission_mode="read", action="read")["github_token"], False, ""
    except WebsiteAuthorityError as exc:
        return "", False, GITHUB_UNAVAILABLE if exc.retryable else NOT_ACCESSIBLE


def _response_failure(response):
    if response.status_code == 200:
        return ""
    headers = getattr(response, "headers", None) or {}
    rate_limited = response.status_code == 403 and (
        headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"
    )
    if response.status_code in {401, 403, 404} and not rate_limited:
        return NOT_ACCESSIBLE
    return GITHUB_UNAVAILABLE


def _automation_only_pull(pull, *, repo, base, number, headers):
    """True when the PR is a same-repo branch carrying only automation commits.

    Returns None when GitHub could not list the commits.
    """
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = (pull.get("base") or {}).get("repo") if isinstance(pull.get("base"), dict) else {}
    default_branch = str((base_repo or {}).get("default_branch") or "")
    if str(head_repo.get("full_name") or "").casefold() != repo.casefold():
        return False
    if not head.get("ref") or head.get("ref") == default_branch:
        return False
    response = http_client.get(f"{base}/pulls/{number}/commits", headers=headers,
        params={"per_page": _MAX_PULL_COMMITS}, timeout=(3, 15))
    if response.status_code != 200:
        return None
    commits = response.json()
    if not isinstance(commits, list) or not commits or len(commits) >= _MAX_PULL_COMMITS:
        return False
    return all(_is_automation_commit(item) for item in commits)


def _is_automation_commit(item):
    if not isinstance(item, dict):
        return False
    email = str(((item.get("commit") or {}).get("author") or {}).get("email") or "").strip().casefold()
    if email in AUTOMATION_COMMIT_EMAILS or email.endswith("[bot]@users.noreply.github.com"):
        return True
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return author.get("type") == "Bot" or str(author.get("login") or "").endswith("[bot]")
