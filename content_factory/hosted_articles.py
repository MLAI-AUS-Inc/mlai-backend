"""Publishing for MLAI-hosted article directories.

Content Factory renders the site, uploads it and checks the live page; this
module owns which articles a hosted site serves and records each step.
Publishing never touches the customer's repository.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .hosted_article_models import HostedArticlePublication, HostedArticleSite
from .models import ArticlePublishStatus, WrittenArticle

logger = logging.getLogger(__name__)

Status = HostedArticlePublication.Status
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
# Content Factory may report a step again; anything else is a stale or out-of-order report.
TRANSITIONS = {
    Status.PENDING: {Status.RENDERING, Status.FAILED},
    Status.RENDERING: {Status.RENDERING, Status.DEPLOYED, Status.LIVE, Status.FAILED},
    Status.DEPLOYED: {Status.DEPLOYED, Status.LIVE, Status.FAILED, Status.RENDERING},
    Status.LIVE: {Status.LIVE, Status.RENDERING},
    Status.FAILED: {Status.RENDERING, Status.FAILED},
    Status.WITHDRAWN: set(),
}
ARTICLE_FIELDS = ("slug", "title", "excerpt", "published_at", "modified_at", "author_name", "body_html",
                  "categories", "tags", "cover", "structured_data")


class HostedPublishError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def as_dict(self):
        return {"code": self.code, "detail": self.message}


def normalized_domain(value) -> str:
    domain = str(value or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/", 1)[0]
    return domain[4:] if domain.startswith("www.") else domain


def active_site(organization) -> Optional[HostedArticleSite]:
    if organization is None:
        return None
    return HostedArticleSite.objects.filter(organization=organization, state=HostedArticleSite.State.ACTIVE).first()


def publication_payload(publication: HostedArticlePublication) -> dict:
    site = publication.site
    live_url = f"https://{site.domain}{site.base_path}{publication.slug}/" if publication.slug else ""
    return {
        "runId": publication.run_id,
        "publicationId": str(publication.pk),
        "slug": publication.slug,
        "status": publication.status,
        "statusLabel": publication.get_status_display(),
        "liveUrl": live_url if publication.status == Status.LIVE else "",
        "plannedUrl": live_url,
        "revisionDigest": publication.revision_digest,
        "errorCode": publication.error_code,
        "error": publication.error,
        "deployedAt": publication.deployed_at.isoformat() if publication.deployed_at else None,
        "liveVerifiedAt": publication.live_verified_at.isoformat() if publication.live_verified_at else None,
    }


def site_render_state(site: HostedArticleSite) -> dict:
    """Everything Content Factory needs to render the next deployment of this site."""
    # An article that was live keeps its last revision while a republish is queued or after one fails.
    served = Q(status__in=HostedArticlePublication.RENDERED) | Q(status__in=[Status.PENDING, Status.FAILED], live_verified_at__isnull=False)
    rendered = site.publications.filter(served).exclude(article={}).order_by("created_at")
    return {
        "domain": site.domain,
        "basePath": site.base_path,
        "state": site.state,
        "profile": site.profile,
        "workerName": site.worker_name,
        "workerUrl": site.worker_url,
        "publications": [{"runId": row.run_id, "slug": row.slug, "status": row.status,
                          "revisionDigest": row.revision_digest, "article": row.article} for row in rendered],
    }


def request_publication(*, site: HostedArticleSite, run, user) -> HostedArticlePublication:
    """Queue (or re-queue) one finished article for this site; the caller dispatches the render."""
    if site.state != HostedArticleSite.State.ACTIVE:
        raise HostedPublishError("hosted_site_not_active", "Hosted publishing is not switched on for this website.")
    if run.organization_id != site.organization_id:
        raise HostedPublishError("hosted_site_mismatch", "This article belongs to another website.", status=404)
    if str(run.status) != "completed":
        raise HostedPublishError("article_not_ready", "Finish the article before publishing it.")
    with transaction.atomic():
        publication, created = HostedArticlePublication.objects.select_for_update().get_or_create(
            site=site, run_id=run.run_id, defaults={"requested_by": user})
        if not created and publication.status in {Status.PENDING, Status.RENDERING}:
            return publication
        if publication.status == Status.WITHDRAWN:
            raise HostedPublishError("hosted_publication_withdrawn", "This article was taken down. Publish a new revision instead.")
        publication.status = Status.PENDING
        publication.requested_by = user
        publication.error_code = publication.error = ""
        publication.save(update_fields=["status", "requested_by", "error_code", "error", "updated_at"])
    return publication


def _clean_article(article) -> dict:
    if not isinstance(article, dict):
        raise HostedPublishError("hosted_article_invalid", "The rendered article is missing.", status=400)
    missing = [key for key in ("slug", "title", "body_html", "published_at") if not article.get(key)]
    if missing or not SLUG.match(str(article.get("slug"))):
        raise HostedPublishError("hosted_article_invalid", "The rendered article is incomplete.", status=400)
    return {key: article[key] for key in ARTICLE_FIELDS if key in article}


def _mark_written_article_live(publication: HostedArticlePublication, now):
    site = publication.site
    url = f"https://{site.domain}{site.base_path}{publication.slug}/"
    article = publication.written_article or WrittenArticle.objects.filter(
        organization_id=site.organization_id, source_run_id=publication.run_id).order_by("-created_at").first()
    if article is None:
        logger.info("hosted_publication_live_without_written_article run_id=%s", publication.run_id)
        return None
    article.publish_status = ArticlePublishStatus.LIVE
    article.article_url = url
    article.live_url = url
    article.canonical_url = url
    article.canonical_path = urlparse(url).path
    article.live_checked_at = now
    article.live_verified_at = now
    article.published_at = article.published_at or now
    article.save(update_fields=["publish_status", "article_url", "live_url", "canonical_url", "canonical_path",
                                "live_checked_at", "live_verified_at", "published_at"])
    return article


def record_progress(*, domain: str, run_id: str, data: dict) -> HostedArticlePublication:
    """Apply one Content Factory report for a publication; stale reports are refused."""
    try:
        target = Status(str(data.get("status") or ""))
    except ValueError:
        raise HostedPublishError("hosted_status_invalid", "Unknown publication status.", status=400) from None
    now = timezone.now()
    with transaction.atomic():
        site = HostedArticleSite.objects.select_for_update().filter(domain=normalized_domain(domain)).first()
        if site is None:
            raise HostedPublishError("hosted_site_not_found", "No hosted site for this domain.", status=404)
        publication = HostedArticlePublication.objects.select_for_update().filter(site=site, run_id=run_id).first()
        if publication is None:
            raise HostedPublishError("hosted_publication_not_found", "This article was not queued for publishing.", status=404)
        if target not in TRANSITIONS[Status(publication.status)]:
            raise HostedPublishError("hosted_status_stale", f"A {publication.status} publication cannot become {target}.")
        fields = {"status", "updated_at"}
        if "article" in data:
            article = _clean_article(data.get("article"))
            digest = str(data.get("revision_digest") or "")
            if not SHA256.match(digest):
                raise HostedPublishError("hosted_article_invalid", "The rendered article has no revision digest.", status=400)
            clash = site.publications.filter(slug=article["slug"]).exclude(pk=publication.pk).first()
            if clash is not None:
                raise HostedPublishError("hosted_slug_taken", f"{site.base_path}{article['slug']}/ is already used by another article.")
            publication.article, publication.slug, publication.revision_digest = article, article["slug"], digest
            fields |= {"article", "slug", "revision_digest"}
        publication.status = target
        if target == Status.FAILED:
            publication.error_code = str(data.get("error_code") or "hosted_publish_failed")[:100]
            publication.error = str(data.get("error") or "Publishing did not finish.")[:500]
            fields |= {"error_code", "error"}
        elif target in {Status.RENDERING, Status.DEPLOYED, Status.LIVE}:
            publication.error_code = publication.error = ""
            fields |= {"error_code", "error"}
        if target in {Status.DEPLOYED, Status.LIVE} and not publication.slug:
            raise HostedPublishError("hosted_article_invalid", "A deployed publication needs its rendered article.", status=400)
        if target in {Status.DEPLOYED, Status.LIVE}:
            deployment = data.get("deployment") if isinstance(data.get("deployment"), dict) else {}
            if deployment:
                site.worker_name = str(deployment.get("worker_name") or site.worker_name)[:100]
                site.worker_url = str(deployment.get("url") or site.worker_url)[:255]
                site.manifest_digest = str(deployment.get("manifest_digest") or site.manifest_digest)[:64]
                site.last_deployed_at = now
                site.save(update_fields=["worker_name", "worker_url", "manifest_digest", "last_deployed_at", "updated_at"])
                publication.deployed_at = now
                fields.add("deployed_at")
        if target == Status.LIVE:
            if str(data.get("verified_digest") or "") != publication.revision_digest:
                raise HostedPublishError("hosted_live_unverified", "The live page does not carry this revision.")
            publication.live_verified_at = now
            publication.written_article = _mark_written_article_live(publication, now) or publication.written_article
            fields |= {"live_verified_at", "written_article"}
            if site.proxy_verified_at is None:
                site.proxy_verified_at = now
                site.save(update_fields=["proxy_verified_at", "updated_at"])
        publication.save(update_fields=sorted(fields))
    return publication
