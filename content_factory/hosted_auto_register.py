"""Opted-in hosted fallback: immutable recipe, owner-approved proxy, verified activation."""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import timedelta

from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany
from workflow_runs.models import ContentFactoryRun
from . import hosted_articles as hosted
from .hosted_article_models import HostedArticleSite
from .models import OrganizationContentConfig
from .p2_foundation import p2_applies, stamp_run_request
from .preview_domain_claim import public_host, _rival_hosted_site, _rival_website
from .website_contract import WebsiteAuthorityError
from .website_discovery import discovery_snapshot
from .website_live_fetch import fetch_live_route

logger = logging.getLogger(__name__)
ADAPTER = "mlai_static_blog_v1"
RECIPE = {"adapter": ADAPTER, "registry_path": ".mlai-hosted/v1/articles.json",
          "body_dir": ".mlai-hosted/v1/bodies", "build_command": ["python3", ".mlai-hosted/v1/build.py"],
          "output_dir": ".mlai-hosted/v1/dist", "publish_prefix": "blog/", "images": "keep", "renderer_sha256": "8e5c322c70eb416f45104c8733e7356790fb56e8112697b0d126672766335e15"}
RECIPE_DIGEST = hashlib.sha256(json.dumps(RECIPE, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
NATIVE_RUNGS = {"adopt", "native_collection", "deterministic", "model_adapter", "agent"}


def mapping(value):
    """Only JSON objects carry hosted authority receipts."""
    return value if isinstance(value, dict) else {}


def _enabled(org):
    if not getattr(settings, "ARTICLES_HOSTED_AUTO_REGISTER_ENABLED", False) or not p2_applies(org):
        return False
    allowed = {part.strip().lower() for part in str(getattr(settings, "ARTICLES_HOSTED_RUNG_ORGANIZATIONS", "")).split(",")}
    return str(org.pk) in allowed or org.domain.lower() in allowed


def _candidate(org):
    config = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=org).first()
    site = config.website_connection if config else None
    if (site is None or site.state != "connected" or not site.repository_id or not site.installation_id
            or public_host(site.site_url) != public_host(org.domain) or not public_host(org.domain)):
        return None
    from .website_hosted_build import current_scan_source
    source = current_scan_source(site)
    scan = site.scan_snapshots.filter(generation=site.generation, source_sha=source).exclude(detector_version="github_head").order_by("-created_at").first() if source else None
    if scan is None:
        return None
    discovery = discovery_snapshot(scan.evidence)
    # The initial recipe is deliberately typed: unsupported site platforms keep the portable draft.
    framework = str(discovery.get("framework") or "").lower()
    if framework not in {"nextjs", "next", "astro", "gatsby", "vite", "react_router", "react-router", "hugo", "jekyll", "eleventy"}:
        return None
    return config, site, source, framework


def _native_failed(org, connection, source):
    for result in ContentFactoryRun.objects.filter(organization=org, status__in=["failed", "blocked", "denied", "stopped"]).values_list("result", flat=True):
        heal = mapping(mapping(result).get("self_heal_v1"))
        from .website_failure_contract import load_registry
        code = heal.get("code") or mapping(result).get("error_code")
        entry = load_registry()["by_code_v2"].get(code) if isinstance(code, str) else None
        if (entry and entry.get("failure_class") == "strategy" and heal.get("mode") == "policy" and heal.get("source_sha") == source
                and isinstance(heal.get("rung"), str) and heal["rung"] in NATIVE_RUNGS and type(heal.get("attempts")) is int and heal["attempts"] > 0):
            return True
    from . import website_goal as goals
    for op in connection.operations.filter(generation=connection.generation, action="prepare"):
        company_id = mapping(op.payload).get("company_id")
        goal = mapping(mapping(op.receipt).get("goal"))
        if not goals.valid_goal(goal, goals.scope_for(connection, company_id)):
            continue
        from .website_strategy import binding as strategy_binding
        binding = strategy_binding(goal, source)
        for item in goal.get("strategy_history", []):
            if (isinstance(item, dict) and item.get("from") in NATIVE_RUNGS
                    and item.get("reason") == "strategy_exhausted" and item.get("binding") == binding):
                return True
    return False


def hosted_rung_ready(organization):
    """Only an enrolled, supported, source-bound native failure can reach hosted fallback."""
    if not _enabled(organization):
        return False
    candidate = _candidate(organization)
    if candidate is None:
        return False
    _, connection, source, _ = candidate
    return _native_failed(organization, connection, source)


def _unclaimed(org, domain):
    if _rival_hosted_site(domain, org) or _rival_website(domain, org):
        return False
    # The organization/domain binding and live founder claim must agree; no claim is inferred from email.
    from founder_tools.services import claimed_organization, organization_owner_user_id
    claimant = claimed_organization(domain)
    if claimant is None or claimant.pk != org.pk or organization_owner_user_id(org) is None:
        return False
    return not VibeRaisingCompany.objects.filter(domain__iexact=domain).exclude(organization=org).exists()


def _profile(org, connection, source, framework):
    return {"domain": public_host(org.domain), "base_path": "/blog/", "name": org.name,
            "build": {**RECIPE, "recipe_digest": RECIPE_DIGEST},
            "source_recipe": {"version": 1, "adapter": ADAPTER, "digest": RECIPE_DIGEST,
                              "source_sha": source, "framework": framework},
            "auto": {"registered_at": timezone.now().isoformat(), "connection_id": str(connection.pk), "generation": connection.generation, "configuration_version": connection.configuration_version, "source_sha": source}}


def _remote(path, payload):
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    if not config["enabled"]:
        return None
    try:
        response = http_client.post(config["base_url"]+path, json=payload, headers=_content_factory_headers(), timeout=(3, 30))
        return response.json() if response.status_code in {200, 202} else None
    except Exception:
        logger.warning("hosted_fallback_dispatch_pending path=%s", path)
        return None


def _update_auto(site, **values):
    with transaction.atomic():
        current = HostedArticleSite.objects.select_for_update().get(pk=site.pk)
        profile = mapping(current.profile)
        profile = {**profile, "auto": {**mapping(profile.get("auto")), **values}}
        HostedArticleSite.objects.filter(pk=site.pk).update(profile=profile)
        site.profile = profile


def _proxy(site, connection, config):
    from .website_connections import contract_for
    engine = importlib.import_module("content_factory.failure_policy_engine")
    run_id = "hosted-proxy-"+site.pk.hex+"-"+str(connection.generation)
    if not engine.claim_pull_request_slot(site.organization, key=run_id+":1"):
        return False
    request = {**contract_for(connection), "run_id": run_id, "domain": site.domain,
               "hosted_site_id": str(site.pk), "worker_url": site.worker_url,
               "github_repo": connection.github_repo, "merge_policy": "backend_worker"}
    stamp_run_request(request, organization=site.organization)
    run, _ = ContentFactoryRun.objects.get_or_create(run_id=run_id, defaults={"organization": site.organization,
        "domain": site.domain, "github_repo": connection.github_repo, "workflow": "article_system_setup",
        "status": "queued", "run_request": request})
    if run.organization_id != site.organization_id:
        return False
    value = _remote(f"/api/hosted-sites/{site.pk}/proxy-setup", request)
    if value is None:
        return False
    _update_auto(site, proxy_run_id=run.run_id, proxy_requested_at=timezone.now().isoformat())
    return True


def activate(site):
    """Require the owner-approved merged proxy and this site's exact hosted response body."""
    auto = mapping(mapping(site.profile).get("auto"))
    run = ContentFactoryRun.objects.filter(run_id=auto.get("proxy_run_id"), organization=site.organization).first()
    merge = mapping(mapping(getattr(run, "result", None)).get("merge_v1"))
    click = mapping(mapping(getattr(run, "result", None)).get("owner_merge_approval_v1"))
    if (not merge.get("merge_sha") or click.get("kind") != "proxy" or not click.get("approved_at")
            or click.get("head_sha") != merge.get("head_sha")
            or click.get("approved_by_user_id") != merge.get("approved_by_user_id")):
        return False
    worker = urlsplit(site.worker_url)
    if (worker.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", worker.hostname or "")
            or worker.path not in {"", "/"} or worker.query or worker.fragment or worker.username or worker.password or worker.port not in {None, 443}):
        return False
    try:
        customer, headers = fetch_live_route(f"https://{site.domain}/blog/", site.domain)
        own, _ = fetch_live_route(site.worker_url.rstrip("/")+"/blog/", worker.hostname)
    except (WebsiteAuthorityError, ValueError):
        return False
    if headers.get("x-mlai-hosted") != "1" or hashlib.sha256(customer).digest() != hashlib.sha256(own).digest():
        return False
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=site.organization_id)
        current = HostedArticleSite.objects.select_for_update().get(pk=site.pk)
        current_run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        current_click = mapping(mapping(current_run.result).get("owner_merge_approval_v1"))
        current_merge = mapping(mapping(current_run.result).get("merge_v1"))
        from founder_tools.services import organization_owner_user_id
        if (current_click != click or current_merge != merge or organization_owner_user_id(site.organization) != click.get("approved_by_user_id")):
            return False
        if current.worker_url != site.worker_url or current.organization_id != site.organization_id or not _unclaimed(site.organization, site.domain):
            return False
        current.state = HostedArticleSite.State.ACTIVE
        current.proxy_verified_at = timezone.now()
        current.save(update_fields=["state", "proxy_verified_at", "updated_at"])
    return True


def run_hosted_sweep(now=None, limit=None):
    """Create draft sites, deploy their immutable scaffold, request consent-bound proxy, activate."""
    if not getattr(settings, "ARTICLES_HOSTED_AUTO_REGISTER_ENABLED", False):
        return {"status": "disabled"}
    now = now or timezone.now()
    result = {"registered": 0, "proxy_requested": 0, "activated": 0}
    allowed = {part.strip().lower() for part in str(getattr(settings, "ARTICLES_HOSTED_RUNG_ORGANIZATIONS", "")).split(",") if part.strip()}
    ids = [int(part) for part in allowed if part.isdecimal() and len(part) <= 18]
    domains = [part for part in allowed if not part.isdecimal()]
    for org in Organization.objects.filter(Q(pk__in=ids) | Q(domain__in=domains)).order_by("pk")[:max(1, int(limit or 500))]:
        if not _enabled(org):
            continue
        existing = HostedArticleSite.objects.filter(organization=org).first()
        if existing and not mapping(mapping(existing.profile).get("auto")):
            continue  # Existing sites, PawShare included, are never rewritten or activated here.
        if existing is None:
            candidate = _candidate(org)
            if candidate is None:
                continue
            config, connection, source, framework = candidate
            if not _unclaimed(org, public_host(org.domain)) or not _native_failed(org, connection, source):
                continue
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=org.pk)
                if HostedArticleSite.objects.filter(organization=org).exists() or not _unclaimed(org, public_host(org.domain)):
                    continue
                fresh = _candidate(org)
                if fresh is None or fresh[1].pk != connection.pk or fresh[1].generation != connection.generation or fresh[2] != source:
                    continue
                existing, created = hosted.register_site(organization=org, profile=_profile(org, connection, source, framework), update_existing=False)
                result["registered"] += int(created)
        else:
            config = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=org).first()
            connection = config.website_connection if config else None
            auto = mapping(mapping(existing.profile).get("auto"))
            if (connection is None or connection.state != "connected" or str(connection.pk) != auto.get("connection_id")
                    or connection.generation != auto.get("generation") or connection.configuration_version != auto.get("configuration_version") or not _unclaimed(org, existing.domain)):
                continue
        if existing.state != HostedArticleSite.State.DRAFT:
            continue
        auto = mapping(mapping(existing.profile).get("auto"))
        if not existing.worker_url:
            # The site must exist before the builder can read its immutable recipe.
            try:
                attempted = parse_datetime(auto.get("build_last_attempt_at", ""))
            except ValueError:
                attempted = None
            if attempted and timezone.is_aware(attempted) and attempted > now-timedelta(minutes=10):
                continue
            attempts = auto.get("build_attempts", 0)
            attempts = attempts if type(attempts) is int and attempts >= 0 else 0
            if attempts >= 6:
                from .founder_journey_alerts import queue_founder_journey_alert
                queue_founder_journey_alert(organization=org, alert_reason="self_heal_capped", slot="hosted-"+existing.pk.hex)
                continue
            _update_auto(existing, build_attempts=attempts+1, build_last_attempt_at=now.isoformat())
            if _remote("/api/hosted-sites/sync", {"domain": existing.domain}) is not None:
                _update_auto(existing, build_requested_at=now.isoformat())
            continue
        if not auto.get("proxy_run_id"):
            try:
                result["proxy_requested"] += int(_proxy(existing, connection, config))
            except (ImportError, AttributeError):
                pass
        else:
            result["activated"] += int(activate(existing))
    return {"status": "completed", **result}


def publish_via_hosted(run, *, retry=False):
    """Publish an already reviewed revision with the existing hosted service, without repository writes."""
    site = hosted.active_site(run.organization)
    if site is None:
        return None
    from django.contrib.auth import get_user_model
    from .publish_reviewed_draft import may_publish, claim_count, PublishReviewedError
    from .article_review_views import remote_review
    from rest_framework.response import Response
    approval = mapping(mapping(run.result).get("article_approval_v1"))
    user = get_user_model().objects.filter(pk=approval.get("approved_by_user_id"), is_active=True).first()
    if user is None or not may_publish(user, run.organization):
        raise PublishReviewedError("not_allowed", "Publishing permission changed.", 403)
    request = mapping(run.run_request)
    if approval.get("source") == "publish_reviewed" and request.get("hosted_site_id"):
        if request.get("hosted_site_id") != str(site.pk):
            raise PublishReviewedError("not_offered", "The hosted website changed. Open the draft again.")
        publication = hosted.request_publication(site=site, run=run, user=user, allow_continuation=True)
        source_id = request.get("source_run_id")
        response = _remote(f"/api/runs/{source_id}/hosted-publish-reviewed", {**request, **({"publish_retry": True} if retry else {})})
        if response is None or response.get("run_id", response.get("new_run_id")) != run.run_id:
            raise PublishReviewedError("not_available", "Publishing is still being confirmed. Please try again shortly.", 503)
        return hosted.publication_payload(publication)
    review = remote_review(run)
    if isinstance(review, Response) or claim_count(run, review) != 0:
        raise PublishReviewedError("claims_open", "Check the highlighted passages before publishing this article.")
    revision = str(review.get("revision") or "")
    if not revision:
        raise PublishReviewedError("not_offered", "Open the latest saved draft before publishing.")
    if (getattr(settings, "HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW", False)
            and (review.get("previewPending") or str(review.get("previewedRevision") or "") != revision)):
        raise PublishReviewedError("preview_required", "Open the rendered preview of this version before publishing.")
    publication = hosted.request_publication(site=site, run=run, user=user)
    from .hosted_article_views import _dispatch_hosted_publish
    dispatch = _dispatch_hosted_publish(publication, revision)
    if not dispatch["queued"]:
        raise PublishReviewedError("not_available", "Publishing is still being confirmed. Please try again shortly.", 503)
    return hosted.publication_payload(publication)
