"""Process GitHub webhook events into authoritative article publish state.

Instead of the best-effort 10-minute poll (refresh_publish_statuses), a
`pull_request` event updates an article's PR / merged / on-main facts the moment
GitHub reports them, and a `push` to the default branch confirms content is
literally on main (the source of truth). All handlers are best-effort and never
raise on a malformed payload — the view turns exceptions into a 5xx for GitHub
to retry.
"""

import hashlib
import hmac
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .article_publish_status import apply_pull_request_state, coerce_pr_number
from .models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle

logger = logging.getLogger(__name__)


def verify_github_signature(secret, body, signature_header) -> bool:
    """Validate GitHub's X-Hub-Signature-256 (HMAC-SHA256 of the raw body).

    Refuses when no secret is configured — an unauthenticated webhook must never
    be trusted to mutate publish state.
    """
    if not secret:
        return False
    signature_header = str(signature_header or "")
    if not signature_header.startswith("sha256="):
        return False
    if isinstance(secret, str):
        secret = secret.encode()
    if isinstance(body, str):
        body = body.encode()
    expected = "sha256=" + hmac.new(secret, body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _articles_for_pr(html_url, number, repo):
    """Find the article(s) a PR belongs to: by exact PR url, else by (number,
    repo) via the org's configured github_repo."""
    if html_url:
        by_url = list(WrittenArticle.objects.filter(pr_url=html_url))
        if by_url:
            return by_url
    if number is None or not repo:
        return []
    org_ids = list(
        OrganizationContentConfig.objects.filter(github_repo__iexact=repo).values_list(
            "organization_id", flat=True
        )
    )
    if not org_ids:
        return []
    return list(WrittenArticle.objects.filter(pr_number=number, organization_id__in=org_ids))


def handle_pull_request(payload) -> dict:
    pr = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo = str(repository.get("full_name") or "").strip()
    default_branch = str(repository.get("default_branch") or "").strip()
    number = coerce_pr_number(pr.get("number"))
    html_url = str(pr.get("html_url") or "").strip()
    merged_at_raw = str(pr.get("merged_at") or "").strip()
    state = str(pr.get("state") or "").strip().lower()
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}

    if pr.get("merged") or merged_at_raw:
        publish_status = ArticlePublishStatus.MERGED
    elif state == "closed":
        publish_status = ArticlePublishStatus.PR_CLOSED
    else:
        publish_status = ArticlePublishStatus.PR_OPEN

    pr_state = {
        "status": publish_status,
        "number": number,
        "merged_at": parse_datetime(merged_at_raw) if merged_at_raw else None,
        "merge_commit_sha": str(pr.get("merge_commit_sha") or "").strip(),
        "base_ref": str(base.get("ref") or "").strip(),
        "default_branch": default_branch,
    }
    articles = _articles_for_pr(html_url, number, repo)
    now = timezone.now()
    updated = 0
    for article in articles:
        changed = apply_pull_request_state(article, pr_state, now=now)
        if changed:
            article.save(update_fields=sorted(changed))
            updated += 1
    return {"event": "pull_request", "matched": len(articles), "updated": updated}


def handle_push(payload) -> dict:
    """A push to the repo's default branch confirms its changed files are on main.

    Marks any not-yet-verified article whose content_path is among the pushed
    files as on-main — catches direct pushes and reaffirms the source of truth.
    """
    repository = payload.get("repository") or {}
    repo = str(repository.get("full_name") or "").strip()
    default_branch = str(repository.get("default_branch") or "").strip()
    ref = str(payload.get("ref") or "").strip()
    if not repo or not default_branch or ref != f"refs/heads/{default_branch}":
        return {"event": "push", "matched": 0, "updated": 0, "ignored": "not default branch"}

    changed_paths = set()
    for commit in payload.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified"):
            for path in commit.get(key) or []:
                changed_paths.add(str(path))
    if not changed_paths:
        return {"event": "push", "matched": 0, "updated": 0}

    org_ids = list(
        OrganizationContentConfig.objects.filter(github_repo__iexact=repo).values_list(
            "organization_id", flat=True
        )
    )
    if not org_ids:
        return {"event": "push", "matched": 0, "updated": 0}

    head_commit = payload.get("head_commit") if isinstance(payload.get("head_commit"), dict) else {}
    head_sha = str(head_commit.get("id") or "").strip()
    now = timezone.now()
    matched = 0
    candidates = (
        WrittenArticle.objects.filter(organization_id__in=org_ids, on_main_verified_at__isnull=True)
        .exclude(content_path="")
    )
    for article in candidates:
        if article.content_path in changed_paths:
            article.on_main_verified_at = now
            article.on_main_commit_sha = head_sha
            article.save(update_fields=["on_main_verified_at", "on_main_commit_sha"])
            matched += 1
    return {"event": "push", "matched": matched, "updated": matched}


def process_github_event(event_type, payload) -> dict:
    """Dispatch a verified GitHub event to its handler."""
    event_type = str(event_type or "").strip().lower()
    payload = payload if isinstance(payload, dict) else {}
    from .website_wakeups import wake_from_event, enabled as event_wakes_enabled
    if event_wakes_enabled() and any(payload.get(key) is not None and not isinstance(payload[key], dict) for key in ("installation", "repository", "pull_request", "check_run", "check_suite", "deployment", "deployment_status")):
        return {"event": event_type, "ignored": True, "reason": "malformed_event"}
    wakes = wake_from_event(event_type, payload) if event_wakes_enabled() else 0
    if event_type == "installation" and payload.get("action") == "new_permissions_accepted":
        from django.db.models import F
        from .website_models import WebsiteConnection
        from .website_prepare import advance_prepares_for_connection
        from .website_github_permissions import invalidate_installation_grants
        identifier = str((payload.get("installation") or {}).get("id") or "")
        if not identifier.isdigit():
            return {"event": event_type, "ignored": True}
        invalidate_installation_grants(identifier)
        connections = WebsiteConnection.objects.filter(installation_id=identifier, state="connected",
            operations__action="prepare", operations__state="needs_user", operations__generation=F("generation"))
        count = wakes if event_wakes_enabled() else sum(advance_prepares_for_connection(key) for key in connections.values_list("id", flat=True).distinct())
        return {"event": event_type, "action": "new_permissions_accepted", "prepares_woken": count}
    if event_type == "ping":
        return {"event": "ping"}
    if event_type in {"installation", "installation_repositories", "repository"}:
        from .website_reconciliation import handle_website_github_event
        return {**handle_website_github_event(event_type, payload), **({"prepares_woken": wakes} if event_wakes_enabled() else {})}
    if event_type == "pull_request":
        return {**handle_pull_request(payload), **({"prepares_woken": wakes} if event_wakes_enabled() else {})}
    if event_type == "push":
        from .website_reconciliation import handle_website_github_event
        lifecycle = handle_website_github_event(event_type, payload)
        return {**handle_push(payload), "website_connections_invalidated": lifecycle.get("invalidated", 0)}
    if event_wakes_enabled() and event_type in {"deployment_status", "check_run", "check_suite"}:
        return {"event": event_type, "prepares_woken": wakes}
    return {"event": event_type or "unknown", "ignored": True}
