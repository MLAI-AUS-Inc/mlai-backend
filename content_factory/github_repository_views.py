"""Explicit repository selection, independent of authorization and scanning."""
import logging
import re

from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView
from organizations.models import Organization

from . import vibe_marketing_views as marketing
from .article_setup_reset import ARTICLE_SETUP_DEEP_RESET_FIELDS, reset_article_setup_config
from .models import ResearchAutomation, ResearchAutomationStatus
from .website_connections import bind_website, contract_for, transition_connection
from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)

# Founder-facing reasons a bound repository failed re-verification; other codes keep the generic text.
REVERIFY_REFUSAL_DETAILS = {
    "github_access_required": "MLAI's GitHub app can no longer read {repo}. In GitHub, give the app access to this repository, then save again.",
    "website_disconnected": "This website was disconnected. Reconnect it before saving {repo} again.",
    "github_unavailable": "GitHub did not respond while checking {repo}. Wait a minute, then save again.",
}
REVERIFY_REFUSAL_FALLBACK = "GitHub access to {repo} could not be verified. Check the connection and try again."


class VibeMarketingGitHubRepositoryView(APIView):
    """Validate founder access before replacing a startup's repository."""

    @transaction.atomic
    def put(self, request):
        if not marketing._company_id_from_request(request):
            return Response({"companyId": "Choose a startup."}, status=400)
        context, error = marketing._resolve_context_or_response(request)
        if error is not None:
            return error
        raw = request.data.get("githubRepo", request.data.get("github_repo"))
        if not isinstance(raw, str) or (raw.strip() and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw.strip())):
            return Response({"githubRepo": "Use owner/repository, or an empty value to unlink."}, status=400)
        repo = raw.strip()
        # Match the lifecycle service lock order: organization before config.
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        config = marketing._get_config(context.organization)
        config = type(config).objects.select_for_update().get(pk=config.pk)
        conflict = marketing._github_repo_company_conflict_response(context=context, requested_repo=repo)
        if conflict is not None:
            return conflict
        changed = repo.lower() != str(config.github_repo or "").strip().lower()
        needs_binding = bool(repo and (changed or not config.website_connection_id))
        if repo and not needs_binding:
            # Re-saving the bound repository proves its access is still live. A new
            # or changed selection is verified by bind_website against the founder's
            # own installations instead: this probe mints through the existing
            # connection, so without one it can only refuse.
            access = marketing._verify_github_repository_access(context, config, force=True)
            if not access.get("verified"):
                # The failure-contract audit pins this exact code expression.
                body = {"code": access.get("reasonCode", "github_access_required")}
                logger.warning("vibe_marketing_repository_selection_refused org=%s repo=%s stage=reverify code=%s",
                    context.organization.pk, repo, body["code"])
                body["detail"] = REVERIFY_REFUSAL_DETAILS.get(body["code"], REVERIFY_REFUSAL_FALLBACK).format(repo=repo)
                return Response(body, status=409)
        if changed or needs_binding:
            try:
                if repo:
                    bind_website(config, user=request.user, repo=repo, expected=request.data)
                elif config.website_connection_id:
                    transition_connection(config, action="disconnect",
                        expected=request.data if request.data.get("website_connection_id") else contract_for(config.website_connection))
            except WebsiteAuthorityError as exc:
                transaction.set_rollback(True)
                logger.warning("vibe_marketing_repository_selection_refused org=%s repo=%s stage=%s code=%s",
                    context.organization.pk, repo, "bind" if repo else "unlink", exc.code)
                return Response(exc.as_dict(), status=exc.status)
            # Keep historical runs, but tombstone prior setup evidence. A fresh
            # scan must establish publishing readiness for the new repository.
            reset_article_setup_config(config, github_repo=repo)
            config.article_system.pop("scan", None)
            for field in ARTICLE_SETUP_DEEP_RESET_FIELDS:
                setattr(config, field, config._meta.get_field(field).get_default())
            config.github_repo = repo
            config.daily_discovery_enabled = False
            config.save(update_fields=["github_repo", "article_system", "daily_discovery_enabled",
                *ARTICLE_SETUP_DEEP_RESET_FIELDS, "updated_at"])
            ResearchAutomation.objects.filter(organization=context.organization,
                status=ResearchAutomationStatus.ACTIVE).update(status=ResearchAutomationStatus.PAUSED)
        return Response({"githubRepo": repo, "repositoryChanged": changed,
            "requiresVerification": (changed or needs_binding) and bool(repo)})
