"""Track the real publish lifecycle of WrittenArticle rows.

A completed writing run only means the article was packaged — nothing is on
the customer's site until a PR merges and the site deploys. This module
derives the lifecycle status from run evidence and refreshes it against
GitHub (PR state) and the live site (sitemap membership), so the dashboard
can show real state instead of an unconditional "Published".

All network calls are best-effort with short timeouts: the backend runs a
sync worker, so refresh work is throttled per article and bounded per call.
"""

import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations import http_client as http_requests
from integrations.services.github_app import GitHubAppTokenError, create_installation_access_token

from .models import ArticlePublishStatus, WrittenArticle

logger = logging.getLogger(__name__)

# written -> pr_open/pr_closed -> merged -> live; refreshes never downgrade.
_STATUS_RANK = {
    ArticlePublishStatus.WRITTEN: 0,
    ArticlePublishStatus.PR_OPEN: 1,
    ArticlePublishStatus.PR_CLOSED: 1,
    ArticlePublishStatus.MERGED: 2,
    ArticlePublishStatus.LIVE: 3,
}

REFRESH_INTERVAL = timedelta(minutes=10)
SITEMAP_CACHE_SECONDS = 300
SITEMAP_FAILURE_CACHE_SECONDS = 60
_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+?)/pull/(\d+)")
# PR states we still poll: the pre-merge states, plus merged articles not yet
# confirmed on origin's default branch — so we can capture the merge commit and
# set on_main_verified_at, the authoritative "it's really on main" fact.
_PR_STATES_WORTH_CHECKING = {
    ArticlePublishStatus.WRITTEN,
    ArticlePublishStatus.PR_OPEN,
    ArticlePublishStatus.MERGED,
}
# Branch names treated as origin's source-of-truth default branch.
DEFAULT_BRANCH_NAMES = {"main", "master"}


def publish_status_rank(value) -> int:
    return _STATUS_RANK.get(value, 0)


def coerce_pr_number(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def article_bucket(article) -> str:
    """Which dashboard bucket a written article belongs to.

    A WrittenArticle has finished authoring, so it is never a "draft": it is
    "published" once its content is confirmed on origin's default branch (the
    source of truth), otherwise still "publishing". The weaker sitemap-based LIVE
    status is honoured as published for back-compat until on-main verification
    backfills it.
    """
    if article.on_main_verified_at or article.publish_status == ArticlePublishStatus.LIVE:
        return "published"
    return "publishing"


def derive_publish_status_from_evidence(evidence) -> str:
    """Map run evidence (see _publish_evidence_from_run) to a lifecycle status."""
    evidence = evidence if isinstance(evidence, dict) else {}
    merge_status = str(evidence.get("mergeStatus") or "").strip().lower()
    if merge_status == "merged":
        return ArticlePublishStatus.MERGED
    if evidence.get("prUrl"):
        if merge_status in {"closed", "rejected", "declined"}:
            return ArticlePublishStatus.PR_CLOSED
        return ArticlePublishStatus.PR_OPEN
    return ArticlePublishStatus.WRITTEN


def advance_publish_status(article, new_status, *, pr_number=None, pr_merged_at=None, live_url=None):
    """Apply new_status to the article without ever downgrading it.

    pr_open <-> pr_closed share a rank and may flip in either direction
    (PRs get closed and reopened); everything else only moves forward.
    Mutates the instance and returns the list of changed field names —
    callers decide when to save.
    """
    changed = []
    if pr_number and article.pr_number != pr_number:
        article.pr_number = pr_number
        changed.append("pr_number")
    current_rank = publish_status_rank(article.publish_status)
    new_rank = publish_status_rank(new_status)
    pr_flip = {article.publish_status, new_status} <= {
        ArticlePublishStatus.PR_OPEN,
        ArticlePublishStatus.PR_CLOSED,
    }
    if new_status != article.publish_status and (new_rank > current_rank or pr_flip):
        article.publish_status = new_status
        changed.append("publish_status")
    if new_rank >= publish_status_rank(ArticlePublishStatus.MERGED) and pr_merged_at and not article.pr_merged_at:
        article.pr_merged_at = pr_merged_at
        changed.append("pr_merged_at")
    if new_status == ArticlePublishStatus.LIVE:
        if not article.live_verified_at:
            article.live_verified_at = timezone.now()
            changed.append("live_verified_at")
        if live_url and article.live_url != live_url:
            article.live_url = live_url
            changed.append("live_url")
    return changed


def _apply_on_main_evidence(article, pr_state, now):
    """Record the authoritative on-main facts from a PR's GitHub state.

    A PR shown as merged into origin's default branch means its merge commit —
    and therefore the article's files — are on main. That is the source of truth
    the dashboard's "published" bucket gates on. Captures the merge commit even
    for non-default-branch merges (useful evidence) but only sets
    on_main_verified_at for a default-branch merge. Never downgrades.

    When pr_state carries an explicit "default_branch" (e.g. from a GitHub
    webhook, which names the repo's real default) it is authoritative; otherwise
    we fall back to the common default-branch names.
    """
    changed = set()
    merge_commit_sha = str(pr_state.get("merge_commit_sha") or "").strip()
    if merge_commit_sha and article.merge_commit_sha != merge_commit_sha:
        article.merge_commit_sha = merge_commit_sha
        changed.add("merge_commit_sha")
    base_ref = str(pr_state.get("base_ref") or "").strip().lower()
    default_branch = str(pr_state.get("default_branch") or "").strip().lower()
    on_default_branch = base_ref == default_branch if default_branch else base_ref in DEFAULT_BRANCH_NAMES
    merged_into_main = pr_state.get("status") == ArticlePublishStatus.MERGED and on_default_branch
    if merged_into_main and not article.on_main_verified_at:
        article.on_main_verified_at = now
        article.on_main_commit_sha = merge_commit_sha
        changed.update({"on_main_verified_at", "on_main_commit_sha"})
    return changed


def apply_pull_request_state(article, pr_state, *, now=None):
    """Apply a PR's GitHub state (from the poller or a webhook) to an article.

    Advances the publish status and records the authoritative on-main facts when
    the PR merged into the default branch. pr_state keys: status, number,
    merged_at, merge_commit_sha, base_ref, and optionally default_branch. Mutates
    the instance; returns the set of changed field names (callers save).
    """
    now = now or timezone.now()
    changed = set(
        advance_publish_status(
            article,
            pr_state["status"],
            pr_number=pr_state.get("number"),
            pr_merged_at=pr_state.get("merged_at"),
        )
    )
    changed.update(_apply_on_main_evidence(article, pr_state, now))
    return changed


def refresh_publish_statuses(organization, config=None, *, limit=6, force=False):
    """Best-effort refresh of the organization's non-live articles.

    Bounded work per call: one cached sitemap fetch plus at most `limit`
    GitHub PR lookups. Articles checked within REFRESH_INTERVAL are skipped
    unless force=True. Returns the articles that were (re)checked.
    """
    now = timezone.now()
    candidates = list(
        WrittenArticle.objects.filter(organization=organization)
        .exclude(publish_status=ArticlePublishStatus.LIVE)
        .order_by("-created_at")[:25]
    )
    if not force:
        candidates = [
            article
            for article in candidates
            if not article.live_checked_at or now - article.live_checked_at >= REFRESH_INTERVAL
        ]
    candidates = candidates[:limit]
    if not candidates:
        return []

    site_urls = _site_article_urls(getattr(organization, "domain", ""))
    refreshed = []
    for article in candidates:
        changed = {"live_checked_at"}
        article.live_checked_at = now
        live_url = _match_live_url(article, site_urls)
        if live_url:
            changed.update(advance_publish_status(article, ArticlePublishStatus.LIVE, live_url=live_url))
        # Poll the PR even when the sitemap already matched: on-main verification
        # is the authoritative published signal and we want the merge commit on
        # record. Skipped once on_main_verified_at is set (terminal).
        if (
            article.pr_url
            and not article.on_main_verified_at
            and article.publish_status in _PR_STATES_WORTH_CHECKING
        ):
            pr_state = _github_pr_state(article.pr_url, config)
            if pr_state:
                changed.update(apply_pull_request_state(article, pr_state, now=now))
        article.save(update_fields=sorted(changed))
        refreshed.append(article)
    return refreshed


def _normalized_domain(domain) -> str:
    text = str(domain or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip("/")


def _site_article_urls(domain):
    """Fetch (cached) the customer site's sitemap URLs; [] when unavailable."""
    domain = _normalized_domain(domain)
    if not domain:
        return []
    cache_key = f"article-publish-status:sitemap:{domain}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    urls = []
    try:
        response = http_requests.get(f"https://{domain}/sitemap.xml", timeout=(3, 5))
        if response.status_code == 200:
            root = ElementTree.fromstring(response.content)
            urls = [
                element.text.strip()
                for element in root.iter()
                if element.tag.endswith("loc") and element.text and element.text.strip()
            ]
        else:
            logger.info("article_publish_status_sitemap_unavailable domain=%s status=%s", domain, response.status_code)
    except Exception:
        logger.warning("article_publish_status_sitemap_fetch_failed domain=%s", domain, exc_info=True)
        cache.set(cache_key, [], SITEMAP_FAILURE_CACHE_SECONDS)
        return []
    cache.set(cache_key, urls, SITEMAP_CACHE_SECONDS if urls else SITEMAP_FAILURE_CACHE_SECONDS)
    return urls


def _match_live_url(article, site_urls):
    """An article is live when its slug is the last path segment of a sitemap URL."""
    slug = str(article.slug or "").strip().strip("/").split("/")[-1].lower()
    if not slug or not site_urls:
        return None
    for url in site_urls:
        path = urlsplit(url).path.rstrip("/").lower()
        if path == f"/{slug}" or path.endswith(f"/{slug}"):
            return url
    return None


def _github_pr_state(pr_url, config):
    match = _PR_URL_RE.search(str(pr_url or ""))
    if not match:
        return None
    repo, number = match.group(1), int(match.group(2))
    token = _github_read_token(config, repo)
    if not token:
        return None
    try:
        response = http_requests.get(
            f"https://api.github.com/repos/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=(3, 5),
        )
    except Exception:
        logger.warning("article_publish_status_pr_fetch_failed repo=%s pr=%s", repo, number, exc_info=True)
        return None
    if response.status_code != 200:
        logger.info("article_publish_status_pr_fetch_status repo=%s pr=%s status=%s", repo, number, response.status_code)
        return None
    payload = response.json() or {}
    merged_at_raw = str(payload.get("merged_at") or "").strip()
    if payload.get("merged") or merged_at_raw:
        status = ArticlePublishStatus.MERGED
    elif str(payload.get("state") or "").strip().lower() == "closed":
        status = ArticlePublishStatus.PR_CLOSED
    else:
        status = ArticlePublishStatus.PR_OPEN
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    return {
        "status": status,
        "number": number,
        "merged_at": parse_datetime(merged_at_raw) if merged_at_raw else None,
        "merge_commit_sha": str(payload.get("merge_commit_sha") or "").strip(),
        "base_ref": str(base.get("ref") or "").strip(),
    }


def _github_read_token(config, repo):
    installation_id = str(getattr(config, "github_installation_id", "") or "").strip()
    if not installation_id:
        return None
    try:
        return create_installation_access_token(
            installation_id=installation_id,
            repository=repo,
            permission_mode="read",
        ).token
    except GitHubAppTokenError as exc:
        logger.info("article_publish_status_pr_token_unavailable repo=%s detail=%s", repo, exc)
        return None
    except Exception:
        logger.warning("article_publish_status_pr_token_failed repo=%s", repo, exc_info=True)
        return None
