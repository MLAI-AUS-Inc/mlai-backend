"""Publishing for MLAI-hosted article directories.

Content Factory builds the site from its own repository with its hosted
articles, uploads the build and checks the live page; this module owns which
articles a hosted site serves and records each step.
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
        self.reason, self.message, self.status = code, message, status

    def as_dict(self):
        # Hosted publication outcomes are not Articles failure envelopes, so they
        # never use the failure registry's "code" field.
        return {"reason": self.reason, "detail": self.message}


def normalized_domain(value) -> str:
    domain = str(value or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/", 1)[0]
    return domain[4:] if domain.startswith("www.") else domain


def enabled() -> bool:
    from django.conf import settings
    return getattr(settings, "ARTICLES_HOSTED_SITES_ENABLED", False) is True


def active_site(organization) -> Optional[HostedArticleSite]:
    if organization is None or not enabled():
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
        "failureReason": publication.failure_reason,
        "error": publication.error,
        "deployedAt": publication.deployed_at.isoformat() if publication.deployed_at else None,
        "liveVerifiedAt": publication.live_verified_at.isoformat() if publication.live_verified_at else None,
    }


def site_render_state(site: HostedArticleSite) -> dict:
    """Everything Content Factory needs to build the next deployment of this site."""
    # An article that was live keeps its last revision while a republish is queued or after one fails.
    served = Q(status__in=HostedArticlePublication.RENDERED) | Q(status__in=[Status.PENDING, Status.FAILED], live_verified_at__isnull=False)
    rendered = site.publications.filter(served).exclude(article={}).order_by("created_at")
    return {
        "domain": site.domain,
        "siteId": str(site.pk),
        "organizationId": site.organization_id,
        "basePath": site.base_path,
        "state": site.state,
        "profile": site.profile,
        "workerName": site.worker_name,
        "workerUrl": site.worker_url,
        "publications": [{"runId": row.run_id, "slug": row.slug, "status": row.status,
                          "revisionDigest": row.revision_digest, "article": row.article} for row in rendered],
    }


BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
COMMIT = re.compile(r"^[a-f0-9]{40}$")


def _source_connection(site: HostedArticleSite):
    """The organisation's selected website repository, or the one connected repository that can be this site."""
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnection

    usable = [WebsiteConnection.State.CONNECTED, WebsiteConnection.State.PAUSED]
    config = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=site.organization).first()
    selected = config.website_connection if config else None
    auto = site.profile.get("auto") if isinstance(site.profile, dict) else None
    if isinstance(auto, dict):
        if (selected is None or selected.state != WebsiteConnection.State.CONNECTED
                or str(selected.pk) != auto.get("connection_id") or selected.generation != auto.get("generation")
                or selected.configuration_version != auto.get("configuration_version")):
            raise HostedPublishError("hosted_source_not_connected", "The hosted website's authorized repository changed.")
        return selected
    if selected is not None and selected.state in usable and selected.installation_id:
        return selected
    connections = list(WebsiteConnection.objects.filter(
        organization=site.organization, state__in=usable,
    ).exclude(installation_id="").order_by("-updated_at"))
    matching = [row for row in connections if normalized_domain(row.site_url) == site.domain]
    if len(matching) == 1 or (not matching and len(connections) == 1):
        return (matching or connections)[0]
    if not connections:
        raise HostedPublishError("hosted_source_not_connected", "Connect this website's repository to MLAI first.")
    raise HostedPublishError("hosted_source_ambiguous", "More than one connected repository could be this website.")


def site_source(site: HostedArticleSite) -> dict:
    """The repository a hosted site is built from and its current commit, read with MLAI's GitHub App.

    The preview builder checks out exactly this commit. Nothing is written to the repository.
    """
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token

    connection = _source_connection(site)
    credential = None
    try:
        credential = create_installation_access_token(
            installation_id=connection.installation_id, repository=connection.github_repo,
            repository_id=connection.repository_id, permission_mode="read", use_cache=False)
        headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
        repository = http_client.get(f"https://api.github.com/repos/{connection.github_repo}", headers=headers, timeout=(3, 15))
        repository.raise_for_status()
        metadata = repository.json()
        if (not isinstance(metadata, dict) or str(metadata.get("full_name") or "").casefold() != connection.github_repo.casefold()
                or (connection.repository_id and metadata.get("id") != connection.repository_id)):
            raise HostedPublishError("hosted_source_mismatch", "GitHub returned a different repository.")
        branch = connection.branch or str(metadata.get("default_branch") or "")
        if not BRANCH.match(branch) or ".." in branch:
            raise HostedPublishError("hosted_source_unavailable", "The website's branch could not be read.", status=503)
        commit = http_client.get(f"https://api.github.com/repos/{connection.github_repo}/commits/{branch}",
                                 headers=headers, timeout=(3, 15))
        commit.raise_for_status()
        sha = str((commit.json() or {}).get("sha") or "")
        if not COMMIT.match(sha):
            raise HostedPublishError("hosted_source_unavailable", "GitHub did not return the branch's commit.", status=503)
    except HostedPublishError:
        raise
    except Exception:
        logger.warning("hosted_source_unavailable domain=%s", site.domain, exc_info=True)
        raise HostedPublishError("hosted_source_unavailable", "GitHub could not be read. Retry shortly.", status=503) from None
    finally:
        if credential is not None:
            try:
                http_client.delete("https://api.github.com/installation/token",
                                   headers={"Authorization": f"Bearer {credential.token}"}, timeout=(3, 10))
            except Exception:
                pass
    return {"githubRepo": connection.github_repo, "repositoryId": connection.repository_id, "branch": branch, "sha": sha}


def register_site(*, organization, profile, update_existing=True):
    """Shared admin/automatic registration path; draft sites never imply publication consent."""
    if not isinstance(profile, dict) or not isinstance(profile.get("build"), dict):
        raise HostedPublishError("hosted_profile_invalid", "A hosted site needs a build contract.")
    domain = normalized_domain(profile.get("domain"))
    from .preview_domain_claim import public_host
    if not domain or public_host(domain) != public_host(organization.domain):
        raise HostedPublishError("hosted_site_mismatch", "The hosted profile belongs to another startup.")
    base_path = profile.get("base_path")
    if not isinstance(base_path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", base_path):
        raise HostedPublishError("hosted_profile_invalid", "Use a relative hosted directory path.")
    with transaction.atomic():
        site, created = HostedArticleSite.objects.get_or_create(domain=domain,
            defaults={"organization": organization, "base_path": profile["base_path"], "profile": profile})
        if site.organization_id != organization.pk:
            raise HostedPublishError("hosted_site_mismatch", "This domain belongs to another startup.")
        if not created and update_existing:
            site.base_path, site.profile = profile["base_path"], profile
            site.save(update_fields=["base_path", "profile", "updated_at"])
    return site, created


def require_current_publish_rights(organization, actor_id):
    """Resolve the approving actor's current owner/member grant, including frozen teams."""
    from django.contrib.auth import get_user_model
    from founder_tools.startup_team import member_may_publish

    user = get_user_model().objects.filter(pk=actor_id, is_active=True).first() if type(actor_id) is int else None
    if user is None or member_may_publish(user, organization) is not True:
        raise HostedPublishError("hosted_publish_not_allowed", "Publishing permission changed. Open the article again.", status=403)
    return user


def _locked_publication_site(*, site_id=None, domain=None):
    from organizations.models import Organization
    query = HostedArticleSite.objects.filter(pk=site_id) if site_id else HostedArticleSite.objects.filter(domain=normalized_domain(domain))
    identity = query.values('pk', 'organization_id').first()
    if identity is None:
        raise HostedPublishError("hosted_site_not_found", "No hosted site for this domain.", status=404)
    # Team grants and owner offboarding take this same first lock. Do not acquire
    # it after a site/publication lock or keep a cached membership decision.
    organization = Organization.objects.select_for_update().get(pk=identity['organization_id'])
    site = HostedArticleSite.objects.select_for_update().filter(pk=identity['pk'], organization=organization).first()
    if site is None or normalized_domain(organization.domain) != site.domain:
        raise HostedPublishError("hosted_site_mismatch", "This website changed after publishing was approved.", status=403)
    return organization, site


def _publication_run(site, run_id):
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id, organization_id=site.organization_id).first()
    if run is None or normalized_domain(run.domain) != site.domain:
        raise HostedPublishError("hosted_site_mismatch", "This article belongs to another website.", status=403)
    request = run.run_request if isinstance(run.run_request, dict) else {}
    if request.get('hosted_site_id') not in (None, '', str(site.pk)):
        raise HostedPublishError("hosted_site_mismatch", "This publication belongs to another hosted website.", status=403)
    return run


def _require_approval_actor(run, actor_id):
    result = run.result if isinstance(run.result, dict) else {}
    approval = result.get('article_approval_v1')
    if isinstance(approval, dict) and approval.get('source') == 'publish_reviewed':
        if type(approval.get('approved_by_user_id')) is not int or approval['approved_by_user_id'] != actor_id:
            raise HostedPublishError("hosted_publish_not_allowed", "This publication needs a new review.", status=403)


def _require_publication_active(site, run):
    if not enabled() or site.state != HostedArticleSite.State.ACTIVE:
        raise HostedPublishError("hosted_site_not_active", "Hosted publishing is not switched on for this website.")
    from .run_ownership import user_stop_intent
    if user_stop_intent(run) or run.status in {'cancelled', 'denied', 'stopped'}:
        raise HostedPublishError("hosted_publish_not_allowed", "This publishing request was stopped.", status=403)


def request_publication(*, site: HostedArticleSite, run, user, allow_continuation=False) -> HostedArticlePublication:
    """Queue the exact reviewed run only while its approver still has publish rights."""
    with transaction.atomic():
        organization, site = _locked_publication_site(site_id=site.pk)
        run = _publication_run(site, run.run_id)
        _require_publication_active(site, run)
        user = require_current_publish_rights(organization, getattr(user, 'pk', None))
        _require_approval_actor(run, user.pk)
        result = run.result if isinstance(run.result, dict) else {}
        continuation = (allow_continuation and run.workflow == "direct_generate" and
                        isinstance(result.get("article_approval_v1"), dict) and
                        result["article_approval_v1"].get("source") == "publish_reviewed" and
                        isinstance(result.get("publish_reviewed_v1"), dict))
        if str(run.status) != "completed" and not continuation:
            raise HostedPublishError("article_not_ready", "Finish the article before publishing it.")
        publication, created = HostedArticlePublication.objects.select_for_update().get_or_create(
            site=site, run_id=run.run_id, defaults={"requested_by": user})
        if not created and publication.status in {Status.PENDING, Status.RENDERING}:
            if publication.requested_by_id != user.pk:
                raise HostedPublishError("hosted_publish_not_allowed", "This publication needs a new review.", status=403)
            return publication
        if publication.status == Status.WITHDRAWN:
            raise HostedPublishError("hosted_publication_withdrawn", "This article was taken down. Publish a new revision instead.")
        publication.status = Status.PENDING
        publication.requested_by = user
        publication.failure_reason = publication.error = ""
        publication.save(update_fields=["status", "requested_by", "failure_reason", "error", "updated_at"])
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
        from workflow_runs.models import ContentFactoryRun
        child = ContentFactoryRun.objects.filter(run_id=publication.run_id, organization_id=site.organization_id).first()
        result = child.result if child is not None and isinstance(child.result, dict) else {}
        request = child.run_request if child is not None and isinstance(child.run_request, dict) else {}
        if isinstance(result.get("article_approval_v1"), dict) and result["article_approval_v1"].get("source") == "publish_reviewed":
            source = ContentFactoryRun.objects.filter(run_id=request.get("source_run_id"), organization_id=site.organization_id).first()
            from .publish_reviewed_draft import published_child_for_source
            if source is not None and getattr(published_child_for_source(source), "pk", None) == child.pk:
                article = WrittenArticle.objects.filter(organization_id=site.organization_id, source_run_id=source.run_id).order_by("-created_at").first()
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


def _already_applied_live(publication, site, target, data):
    """A terminal callback may acknowledge exactly owned history without rewriting it."""
    if target != Status.LIVE or publication.status != Status.LIVE or not publication.revision_digest:
        return False
    if data.get('verified_digest') != publication.revision_digest:
        return False
    if 'article' in data and (_clean_article(data['article']) != publication.article
            or data.get('revision_digest') != publication.revision_digest):
        return False
    if data.get('revision_digest', publication.revision_digest) != publication.revision_digest:
        return False
    deployment = data.get('deployment')
    if deployment is not None:
        if not isinstance(deployment, dict):
            return False
        expected = {'worker_name': site.worker_name, 'url': site.worker_url, 'manifest_digest': site.manifest_digest}
        if any(key in deployment and str(deployment[key]) != expected[key] for key in expected):
            return False
    return True


def record_progress(*, domain: str, run_id: str, data: dict) -> HostedArticlePublication:
    """Apply one Content Factory report for a publication; stale reports are refused."""
    try:
        target = Status(str(data.get("status") or ""))
    except ValueError:
        raise HostedPublishError("hosted_status_invalid", "Unknown publication status.", status=400) from None
    now = timezone.now()
    with transaction.atomic():
        organization, site = _locked_publication_site(domain=domain)
        publication = HostedArticlePublication.objects.select_for_update().filter(site=site, run_id=run_id).first()
        if publication is None:
            raise HostedPublishError("hosted_publication_not_found", "This article was not queued for publishing.", status=404)
        run = _publication_run(site, run_id)
        _require_approval_actor(run, publication.requested_by_id)
        if _already_applied_live(publication, site, target, data):
            # A lost terminal callback acknowledgement is history, not consent
            # for another build. Preserve all clocks and cancelled run history.
            return publication
        _require_publication_active(site, run)
        require_current_publish_rights(organization, publication.requested_by_id)
        if target == Status.RENDERING and 'article' not in data and 'revision_digest' in data:
            # The finished build asks for fresh upload admission, not another
            # rendering transition. It cannot replace a body, rotate revision,
            # demote a live publication or reopen a completed/stopped intent.
            if publication.status != Status.RENDERING or not publication.revision_digest or data['revision_digest'] != publication.revision_digest:
                raise HostedPublishError("hosted_review_changed", "This publication has a different reviewed revision.")
            return publication
        if target not in TRANSITIONS[Status(publication.status)]:
            raise HostedPublishError("hosted_status_stale", f"A {publication.status} publication cannot become {target}.")
        fields = {"status", "updated_at"}
        if "article" in data:
            article = _clean_article(data.get("article"))
            digest = str(data.get("revision_digest") or "")
            if not SHA256.match(digest):
                raise HostedPublishError("hosted_article_invalid", "The rendered article has no revision digest.", status=400)
            if publication.article and publication.status != Status.PENDING and (
                    article != publication.article or digest != publication.revision_digest):
                raise HostedPublishError("hosted_review_changed", "This publication has a different reviewed revision.")
            clash = site.publications.filter(slug=article["slug"]).exclude(pk=publication.pk).first()
            if clash is not None:
                raise HostedPublishError("hosted_slug_taken", f"{site.base_path}{article['slug']}/ is already used by another article.")
            publication.article, publication.slug, publication.revision_digest = article, article["slug"], digest
            fields |= {"article", "slug", "revision_digest"}
        publication.status = target
        if target == Status.FAILED:
            publication.failure_reason = str(data.get("failure_reason") or "hosted_publish_failed")[:100]
            publication.error = str(data.get("error") or "Publishing did not finish.")[:500]
            fields |= {"failure_reason", "error"}
        elif target in {Status.RENDERING, Status.DEPLOYED, Status.LIVE}:
            publication.failure_reason = publication.error = ""
            fields |= {"failure_reason", "error"}
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
        # P2 approved-body continuations have no repository merge callback. The
        # authenticated hosted progress record owns their real live outcome.
        from workflow_runs.models import ContentFactoryRun
        result = run.result if isinstance(run.result, dict) else {}
        if (run is not None and isinstance(result.get("article_approval_v1"), dict)
                and result["article_approval_v1"].get("source") == "publish_reviewed"):
            from .website_run_authority import event_owned
            owned = event_owned(run)
            if owned:
                from .run_ownership import HOSTED_PUBLICATION_OBSERVATION_KEY
                result = {**result, HOSTED_PUBLICATION_OBSERVATION_KEY: {
                    "schema_version": 1, "publication_id": str(publication.pk), "site_id": str(site.pk),
                    "status": str(target), "revision_digest": publication.revision_digest,
                    "observed_at": now.isoformat()}}
            if target == Status.LIVE:
                url = f"https://{site.domain}{site.base_path}{publication.slug}/"
                result = {**result, "deploy_v1": {"url": url, "provider": "mlai_hosted", "verified_at": now.isoformat()},
                          "channel_v1": {"version": 1, "channel": "hosted", "waiting_to_publish": False, "at": now.isoformat()}}
                if owned:
                    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
                else:
                    ContentFactoryRun.objects.filter(pk=run.pk).update(status="completed", current_step="hosted_publish",
                        result=result, updated_at=now)
            elif target == Status.RENDERING:
                if owned:
                    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
                else:
                    ContentFactoryRun.objects.filter(pk=run.pk).update(status="running", current_step="hosted_publish", updated_at=now)
            elif target == Status.FAILED:
                if owned:
                    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
                else:
                    result = {**result, "error_code": "HOSTED_PREVIEW_UNAVAILABLE"}
                    ContentFactoryRun.objects.filter(pk=run.pk).update(status="failed", current_step="hosted_publish", result=result, updated_at=now)
    return publication


def record_site_deployment(*, domain: str, data: dict) -> HostedArticleSite:
    """Record a whole-site deployment (no new article) and whether the customer's domain serves it."""
    now = timezone.now()
    with transaction.atomic():
        site = HostedArticleSite.objects.select_for_update().filter(domain=normalized_domain(domain)).first()
        if site is None:
            raise HostedPublishError("hosted_site_not_found", "No hosted site for this domain.", status=404)
        fields = {"updated_at"}
        deployment = data.get("deployment") if isinstance(data.get("deployment"), dict) else {}
        if deployment:
            site.worker_name = str(deployment.get("worker_name") or site.worker_name)[:100]
            site.worker_url = str(deployment.get("url") or site.worker_url)[:255]
            site.manifest_digest = str(deployment.get("manifest_digest") or site.manifest_digest)[:64]
            site.last_deployed_at = now
            fields |= {"worker_name", "worker_url", "manifest_digest", "last_deployed_at"}
        if data.get("proxy_verified") is True and site.proxy_verified_at is None:
            site.proxy_verified_at = now
            fields.add("proxy_verified_at")
        site.save(update_fields=sorted(fields))
    return site


def hosted_site_active(config) -> bool:
    """True when this company's articles are published to an MLAI-hosted site, not its repository."""
    return config is not None and active_site(getattr(config, "organization", None)) is not None


def hosted_capabilities(config) -> dict:
    """Hosted publishing fields for the capability projection; none while the rollout is off."""
    if not enabled():
        return {}
    site = active_site(getattr(config, "organization", None)) if config is not None else None
    if site is None:
        return {"canPublishHostedArticle": False, "hostedSite": None}
    return {"canPublishHostedArticle": True,
            "hostedSite": {"state": site.state, "baseUrl": f"https://{site.domain}{site.base_path}",
                           "proxyVerified": site.proxy_verified_at is not None}}


def hosted_publish_step(run, config):
    """The article run's publish step on a hosted site, or None for repository publishing."""
    from .portable_drafts import original_portable_run
    site = active_site(getattr(config, "organization", None)) if config is not None else None
    if site is None or run is None or not original_portable_run(run):
        return None
    publication = site.publications.filter(run_id=run.run_id).first()
    planned = f"https://{site.domain}{site.base_path}"
    if publication is not None and publication.status == Status.LIVE:
        url = publication_payload(publication)["liveUrl"]
        return {"status": "complete", "label": "Open live article", "href": url, "intent": "", "summary": f"Live at {url}"}
    if publication is not None and publication.status in {Status.PENDING, Status.RENDERING, Status.DEPLOYED}:
        waiting = "Uploaded. Waiting for your blog to serve it." if publication.status == Status.DEPLOYED else "Publishing to your blog."
        return {"status": "running", "label": "", "href": "", "intent": "", "summary": waiting}
    failed = publication is not None and publication.status == Status.FAILED
    return {"status": "ready", "label": "Retry publishing" if failed else "Publish article", "href": "", "intent": "hosted-publish",
            "summary": (publication.error or "Publishing did not finish.") if failed else f"Publish it to your blog at {planned}"}
