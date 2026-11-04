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
    """Founder blogs are no longer a setup strategy."""
    return False


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
    """Old proxy requests are inert after blog retirement."""
    return False


def activate(site):
    """Retired sites cannot be activated, even by an old in-flight sweep."""
    return False


def run_hosted_sweep(now=None, limit=None):
    """Compatibility seam for old scheduler invocations; no reads or writes."""
    return {"status": "retired"}


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
