"""Evidence-sensitive GitHub boundary mapping without provider text or credentials."""

from .website_contract import WebsiteAuthorityError


def taxonomy_active():
    """Whether the receiver observes or applies the reviewed failure contract."""
    from .website_failure_scope import taxonomy_enabled
    return taxonomy_enabled() or taxonomy_enabled(observe=True)


def github_failure(exc, *, legacy_code, legacy_message, legacy_retryable=False,
                   source=False, response=None):
    """Map proved causes, preserving the exact Phase A view while both flags are off.

    A generic 401/403/404 is insufficient evidence of a missing customer grant.
    Malformed provider data and platform configuration cannot create owner actions.
    """
    if not taxonomy_active():
        return WebsiteAuthorityError(legacy_code, legacy_message, status=503 if legacy_retryable else 409,
                                     retryable=legacy_retryable)
    from integrations import http_client
    from integrations.services.github_app import (
        GitHubInstallationAccessRequired, GitHubPlatformConfigurationInvalid,
        GitHubPermissionPayloadInvalid, GitHubPermissionIdentityMismatch,
        GitHubPermissionLookupUnavailable,
    )
    observed = response if response is not None else getattr(exc, "response", None)
    status = getattr(observed, "status_code", None)
    headers = getattr(observed, "headers", {}) or {}
    transient = (type(status) is int and (status == 429 or 500 <= status <= 599
                 or status == 403 and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0")))
    if isinstance(exc, GitHubPermissionPayloadInvalid):
        code = "github_permission_payload_invalid"
    elif isinstance(exc, GitHubPermissionIdentityMismatch):
        code = "github_permission_identity_mismatch"
    elif isinstance(exc, GitHubInstallationAccessRequired):
        code = "github_access_required"
    elif isinstance(exc, GitHubPlatformConfigurationInvalid):
        code = "github_platform_credentials_invalid"
    elif transient or isinstance(exc, (http_client.exceptions.Timeout, http_client.exceptions.ConnectionError, GitHubPermissionLookupUnavailable)):
        code = "github_source_transient" if source else "github_repository_transient"
    elif type(status) is int and status != 200:
        code = "github_access_unverified"
    else:
        # These contexts had distinct retryable v0 codes. Give each canonical
        # cause its own fixed entry so every versioned input has one exact v0 view.
        code = {"github_source_unavailable": "github_source_payload_invalid",
                "github_ci_evidence_unavailable": "github_ci_payload_invalid"}.get(
                    legacy_code, "github_provider_payload_invalid")
    error = WebsiteAuthorityError(code, "GitHub verification could not establish the required evidence.",
                                  status=503 if code in {"github_source_transient", "github_repository_transient"} else 409)
    error.findings = [{"provider_operation": "source_read" if source else "repository_access",
                       **({"http_status": status} if type(status) is int and 100 <= status <= 599 else {})}]
    if error.taxonomy_policy or error.taxonomy_observe:
        from .website_failure_contract import classify_failure
        error.failure = classify_failure(code, "source_read" if source else "repository_access", error.findings)
    if error.taxonomy_observe and not error.taxonomy_policy:
        # Shadow mode observes the new cause while exposing the exact original
        # local Phase A code/boolean. No canonical payload has been emitted here.
        error.code, error.retryable = legacy_code, legacy_retryable
        error.legacy_projection = {"code": legacy_code, "retryable": legacy_retryable}
    return error
