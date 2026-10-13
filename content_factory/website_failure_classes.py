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
    "live_route_unavailable": "waiting",
    "manual_merge_required": "needs_user",
    "code_review_ready": "needs_user",
}


def failure_class(code, result=None, *, retryable=False, status=None):
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
    if code.startswith("ADOPTED_") or code.startswith("DIRECTORY_") and code.endswith("_MISSING"):
        return "repairable"
    provider_status = result.get("provider_status_code") or result.get("content_factory_status_code") or status
    if isinstance(provider_status, int) and 500 <= provider_status <= 599:
        return "transient"
    return "transient" if retryable else "repairable"
