"""Short-lived installation-grant evidence for articles preparation."""
from urllib.parse import quote

from django.core.cache import cache

from integrations.models import GitHubInstallation
from integrations import http_client
from integrations.services.github_app import (
    GitHubAppTokenError, GitHubCIEvidencePermissionRequired,
    GitHubPermissionLookupUnavailable, GitHubWorkflowPermissionRequired,
)


def _cache_key(identifier):
    from django.conf import settings
    version = "v2" if getattr(settings, "ARTICLES_SETUP_CONSENT_ENABLED", False) else "v1"
    return f"website-installation-grants-{version}:{identifier}"


def invalidate_installation_grants(identifier):
    """Forget grants when an authenticated installation webhook accepts updates."""
    cache.delete(f"website-installation-grants-v1:{identifier}")
    cache.delete(f"website-installation-grants-v2:{identifier}")


def installation_grants(connection):
    """Read approved App/installation grants without issuing a repository token."""
    from integrations.services.github_app import _require_installation_profile_permissions
    identifier = str(connection.installation_id or "")
    cached = cache.get(_cache_key(identifier))
    if isinstance(cached, dict):
        return dict(cached)
    stored = GitHubInstallation.objects.filter(installation_id=identifier).order_by("-updated_at").first()
    url = f"https://github.com/settings/installations/{quote(identifier, safe='')}"
    if stored and stored.account_type.lower() == "organization" and stored.account_login:
        url = f"https://github.com/organizations/{quote(stored.account_login, safe='')}/settings/installations/{quote(identifier, safe='')}"
    result = {"workflows": False, "checks": False, "statuses": False, "settingsUrl": url}
    try:
        from django.conf import settings
        if getattr(settings, "ARTICLES_SETUP_CONSENT_ENABLED", False):
            repository = _require_installation_profile_permissions(identifier, profile="repository")
            result.update(contents=repository.get("contents") == "write", pull_requests=repository.get("pull_requests") == "write")
        try:
            workflow = _require_installation_profile_permissions(identifier, profile="workflow_files")
            result["workflows"] = workflow.get("workflows") == "write"
        except GitHubWorkflowPermissionRequired:
            pass
        try:
            ci = _require_installation_profile_permissions(identifier, profile="ci_evidence")
            result.update(checks=ci.get("checks") in {"read", "write"}, statuses=ci.get("statuses") in {"read", "write"})
        except GitHubCIEvidencePermissionRequired:
            pass
    except (GitHubPermissionLookupUnavailable, http_client.RequestException) as exc:
        # Provider outages must not be diagnosed as an owner's missing grant.
        from .website_github_failure import taxonomy_active, github_failure
        if taxonomy_active():
            error = github_failure(exc, legacy_code="github_unavailable", legacy_message="GitHub permission checks are temporarily unavailable.", legacy_retryable=True)
            result.update(reasonCode=error.code, reason=str(error), retryable=error.retryable)
            return result
        result.update(reasonCode="github_unavailable", reason="GitHub permission checks are temporarily unavailable.", retryable=True)
        return result
    except GitHubAppTokenError as exc:
        from .website_github_failure import taxonomy_active, github_failure
        if taxonomy_active():
            error = github_failure(exc, legacy_code="github_access_required", legacy_message="Check the selected GitHub installation.")
            result.update(reasonCode=error.code, reason=str(error), retryable=error.retryable)
            return result
        result.update(reasonCode="github_access_required", reason="Check the selected GitHub installation.", retryable=False)
        return result
    cache.set(_cache_key(identifier), result, timeout=60)
    return result
