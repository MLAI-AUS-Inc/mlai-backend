"""Explicit repository selection, independent of authorization and scanning."""
import logging
import re

from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView
from organizations.models import Organization

from . import vibe_marketing_views as marketing
from .article_setup_reset import ARTICLE_SETUP_DEEP_RESET_FIELDS, reset_article_setup_config
from .models import OrganizationContentConfig, ResearchAutomation, ResearchAutomationStatus
from .website_connections import bind_verified_website, contract_for, prepare_website_binding, transition_connection
from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)

# Founder-facing reasons a bound repository failed re-verification; other codes keep the generic text.
REVERIFY_REFUSAL_DETAILS = {
    "github_access_required": "MLAI's GitHub app can no longer read {repo}. In GitHub, give the app access to this repository, then save again.",
    "website_disconnected": "This website was disconnected. Reconnect it before saving {repo} again.",
    "github_unavailable": "GitHub did not respond while checking {repo}. Wait a minute, then save again.",
}
REVERIFY_REFUSAL_FALLBACK = "GitHub access to {repo} could not be verified. Check the connection and try again."


def _selection_state(config):
    connection = config.website_connection
    return (
        str(config.github_repo or "").strip().casefold(),
        str(config.github_installation_id or ""),
        marketing._repository_observation_scope(connection) if connection else None,
    )


class VibeMarketingGitHubRepositoryView(APIView):
    """Validate founder access before replacing a startup's repository."""

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
        organization = context.organization
        config = OrganizationContentConfig.objects.select_related("website_connection").filter(
            organization=organization,
        ).first() or OrganizationContentConfig(organization=organization)
        conflict = marketing._github_repo_company_conflict_response(context=context, requested_repo=repo)
        if conflict is not None:
            return conflict
        selection = _selection_state(config)
        changed = repo.casefold() != selection[0]
        needs_binding = bool(repo and (changed or not config.website_connection_id))
        stage = "bind" if needs_binding else "reverify" if repo else "unlink"
        prepared = None
        try:
            if needs_binding:
                prepared = prepare_website_binding(config, user=request.user, repo=repo)
            elif repo:
                # Re-saving only checks the existing connection, and keeps its setup.
                access = marketing._verify_github_repository_access(context, config, force=True)
                if not access.get("verified"):
                    # The failure-contract audit pins this exact code expression.
                    body = {"code": access.get("reasonCode", "github_access_required")}
                    logger.warning("vibe_marketing_repository_selection_refused org=%s repo=%s stage=reverify code=%s",
                        organization.pk, repo, body["code"])
                    body["detail"] = REVERIFY_REFUSAL_DETAILS.get(body["code"], REVERIFY_REFUSAL_FALLBACK).format(repo=repo)
                    return Response(body, status=409)

            with transaction.atomic():
                # Recheck membership and the selection after the unlocked GitHub read.
                locked_org = Organization.objects.select_for_update().filter(pk=organization.pk).first()
                if locked_org is None or locked_org.domain != organization.domain:
                    raise _selection_changed()
                current_context, error = marketing._resolve_context_or_response(request)
                if error is not None:
                    return error
                if current_context.organization.pk != organization.pk:
                    raise _selection_changed()
                config = marketing._get_config(locked_org)
                config = OrganizationContentConfig.objects.select_for_update().get(pk=config.pk)
                if _selection_state(config) != selection:
                    raise _selection_changed()
                conflict = marketing._github_repo_company_conflict_response(context=current_context, requested_repo=repo)
                if conflict is not None:
                    return conflict
                if changed or needs_binding:
                    self._save_selection(config, request, repo, prepared)
        except WebsiteAuthorityError as exc:
            logger.warning("vibe_marketing_repository_selection_refused org=%s repo=%s stage=%s code=%s",
                organization.pk, repo, stage, exc.code)
            return Response(exc.as_dict(), status=exc.status)
        return Response({"githubRepo": repo, "repositoryChanged": changed,
            "requiresVerification": (changed or needs_binding) and bool(repo)})

    def _save_selection(self, config, request, repo, prepared):
        if prepared is not None:
            bind_verified_website(config, prepared, user=request.user, expected=request.data)
        elif config.website_connection_id:
            transition_connection(config, action="disconnect",
                expected=request.data if request.data.get("website_connection_id") else contract_for(config.website_connection))
        # A fresh scan establishes publishing readiness for the new repository.
        reset_article_setup_config(config, github_repo=repo)
        config.article_system.pop("scan", None)
        for field in ARTICLE_SETUP_DEEP_RESET_FIELDS:
            setattr(config, field, config._meta.get_field(field).get_default())
        config.github_repo = repo
        config.daily_discovery_enabled = False
        config.save(update_fields=["github_repo", "article_system", "daily_discovery_enabled",
            *ARTICLE_SETUP_DEEP_RESET_FIELDS, "updated_at"])
        ResearchAutomation.objects.filter(organization_id=config.organization_id,
            status=ResearchAutomationStatus.ACTIVE).update(status=ResearchAutomationStatus.PAUSED)


def _selection_changed():
    return WebsiteAuthorityError("website_connection_changed", "The website connection changed. Refresh and retry.")
