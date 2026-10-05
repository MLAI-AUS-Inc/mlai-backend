"""Explicit repository selection, independent of authorization and scanning."""
import copy
import re

from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from . import vibe_marketing_views as marketing
from .article_setup_reset import ARTICLE_SETUP_DEEP_RESET_FIELDS, reset_article_setup_config
from .models import ResearchAutomation, ResearchAutomationStatus
from .website_connections import bind_website, contract_for, transition_connection
from .website_contract import WebsiteAuthorityError


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
        config = marketing._get_config(context.organization)
        config = type(config).objects.select_for_update().get(pk=config.pk)
        conflict = marketing._github_repo_company_conflict_response(context=context, requested_repo=repo)
        if conflict is not None:
            return conflict
        if repo:
            candidate = copy.copy(config)
            candidate.github_repo = repo
            access = marketing._verify_github_repository_access(context, candidate, force=True)
            if not access.get("verified"):
                return Response({"code": access.get("reasonCode", "github_access_required"),
                    "detail": "GitHub access to this repository could not be verified. Check the connection and try again."}, status=409)
        changed = repo.lower() != str(config.github_repo or "").strip().lower()
        if changed:
            try:
                if repo:
                    bind_website(config, user=request.user, repo=repo, expected=request.data)
                elif config.website_connection_id:
                    transition_connection(config, action="disconnect",
                        expected=contract_for(config.website_connection))
            except WebsiteAuthorityError as exc:
                transaction.set_rollback(True)
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
            "requiresVerification": changed and bool(repo)})
