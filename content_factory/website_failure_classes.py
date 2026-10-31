"""Compatibility failure policy until both services emit the shared taxonomy."""

FAILURE_CLASSES = frozenset({"transient", "waiting", "repairable", "strategy", "needs_user", "unsupported"})
CODE_CLASSES = {
    "DIRECTORY_STYLE_REVIEW_FAILED": "repairable",
    "WORKFLOW_FILE_NOT_PREFLIGHTED": "repairable",
    "ARTICLE_DIRECTORY_AGENT_BUDGET": "strategy",
    "article_system_setup_preview_not_exact": "repairable",
    "worker_unavailable": "transient",
    "github_unavailable": "transient",
    "HOSTED_PREVIEW_UNAVAILABLE": "transient",
    "website_prepare_unavailable": "transient",
    "CONNECTION_SERVICE_TRANSIENT": "transient",
    "live_route_unavailable": "waiting",
    "manual_merge_required": "needs_user",
    "code_review_ready": "needs_user",
}
# P2 (IF-P2-02): the new codes that only P2 sites emit are classified through
# the registry's pre-P2 view (`by_code`, where they read as registered). The
# five new codes a pre-P2 path already emits (HOSTED_PREVIEW_UNAVAILABLE, the
# three scaffold-blocker literals and render_dependency_contract_violation)
# keep today's codeless classification: HOSTED_PREVIEW_UNAVAILABLE through
# CODE_CLASSES above, the other four through the fallbacks below.
P2_REGISTRY_CLASSIFIED_CODES = frozenset({
    "ARTICLE_STATIC_SOURCE_READ_INCOMPLETE", "BRANCH_PROTECTION_APPROVAL_REQUIRED", "CONNECTION_BINDING_CHANGED",
    "CONNECTION_REPOSITORY_CHANGED", "DEPLOYMENT_NOT_DETECTED", "HOSTED_PROXY_UNVERIFIED", "SCAN_QUEUE_NOT_STARTED",
    "SELF_HEAL_ATTEMPTS_EXHAUSTED", "SELF_HEAL_DEADLINE_REACHED", "SITE_TEST_CHANGE_REQUIRED", "TERMINAL_CODE_MISSING",
    "pull_request_daily_cap", "source_repin_limit_reached", "source_repin_refused",
})


def _registry_class(code):
    """The pre-P2 registry class of a P2-only new code, or None."""
    if code not in P2_REGISTRY_CLASSIFIED_CODES:
        return None
    from .website_failure_contract import load_registry
    entry = load_registry()["by_code"].get(code)
    return entry["failure_class"] if entry is not None else None


def legacy_failure_class(code, result=None, *, retryable=False, status=None):
    """Prefer explicit classes, then deterministic codes over legacy retry flags."""
    result = result if isinstance(result, dict) else {}
    nested = result.get("article_system_setup")
    nested = nested if isinstance(nested, dict) else {}
    supplied = result.get("failure_class") or nested.get("failure_class")
    if isinstance(supplied, str) and supplied in FAILURE_CLASSES:
        return supplied
    code = str(code or "")
    if code in CODE_CLASSES:
        return CODE_CLASSES[code]
    registered = _registry_class(code)
    if registered is not None:
        return registered
    if code.startswith("ADOPTED_") or code.startswith("DIRECTORY_") and code.endswith("_MISSING"):
        return "repairable"
    provider_status = result.get("provider_status_code") or result.get("content_factory_status_code") or status
    if isinstance(provider_status, int) and 500 <= provider_status <= 599:
        return "transient"
    return "transient" if retryable else "repairable"


def failure_class(code, result=None, *, retryable=False, status=None):
    """Decode B1 before policy; legacy receipts retain the bounded A7 adapter."""
    from .website_failure_scope import taxonomy_enabled
    from .website_failure_contract import resolve_failure
    decision = resolve_failure(code, result, retryable=retryable, status=status,
        apply=taxonomy_enabled(), observe=taxonomy_enabled(observe=True))
    return decision["failure_class"]
