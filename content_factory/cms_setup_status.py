"""Read-only preparatory progress without inventing a native goal or proof."""

from django.conf import settings

from . import cms_setup_authority as authority
from . import cms_goal_budget as budget
from .cms_connection_authority import validate_current
from .website_contract import WebsiteAuthorityError
from .website_doctor import safe_code, number
from .website_failure_contract import validate_failure
from .website_goal import CLASSES


def _finish(result, context, config, user, binding):
    try:
        for row in (context.organization, context.company, context.profile, user, config, config.website_connection):
            row.refresh_from_db()
        return result if validate_current(context, config, config.website_connection, user=user) == binding else None
    except (WebsiteAuthorityError, ValueError, TypeError, AttributeError):
        return None


def project(operation, context, config, *, user, binding, now):
    """Expose only the selected owner's real setup identity and actual schedule."""
    from .cms_goal_status import _base, _budget_projection
    try:
        if validate_current(context, config, config.website_connection, user=user) != binding:
            return None
    except WebsiteAuthorityError:
        return None
    result = _base(binding, config.website_connection)
    result.update(mode="setup", target="first_article_live", operationId=str(operation.pk),
        updatedAt=operation.updated_at.isoformat(), step="cms_setup", title="Article setup needs verification",
        message="The saved setup needs verification.", action={"id": "inspect_goal", "label": "View details", "operationId": str(operation.pk)})
    try:
        intent = authority.historical(operation)
        if intent["binding"] != binding:
            return None
        selected = budget.validate_at(operation.receipt.get("native_budget"), started_at=intent["accepted_at"], operation_id=str(operation.pk), now=now)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return _finish(result, context, config, user, binding)
    allowed = False
    try:
        authority.current(operation, user=user, states={operation.state})
        allowed = True
    except WebsiteAuthorityError:
        pass
    try:
        failure = validate_failure(operation.receipt.get("failure"))
    except ValueError:
        failure = {}
    effective = operation.receipt.get("effective_failure_class")
    if operation.state in {"pending", "running"} and allowed and operation.next_attempt_at is not None:
        result.update(state="working", title="Setting up articles", message="Preparing the CMS publishing settings.", action=None,
            nextAttemptAt=operation.next_attempt_at.isoformat())
    elif (operation.state == "needs_user" and allowed and failure.get("failure_class") == "needs_user" and
            (operation.receipt.get("user_action") or {}).get("id") == "connect_cms"):
        result.update(state="needs_you", title="Restore CMS access", message="Restore access to your CMS to continue.",
            action={"id": "connect_cms", "label": "Reconnect CMS", "operationId": str(operation.pk)},
            nextAttemptAt=operation.next_attempt_at.isoformat() if operation.next_attempt_at else None)
    elif not authority.enabled(config.organization_id):
        result.update(title="Article setup is paused", message="Article setup is currently paused.")
    elif operation.state in {"stopped", "cancelled", "needs_attention"}:
        result.update(title="Article setup stopped", message="View the recorded setup details.")
    if getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
        result["doctor"] = {"version": 1, "channel": "cms", "companyId": binding["company_id"], "connectionId": binding["connection_id"],
            "connectionGeneration": binding["generation"], "configurationRevision": config.website_connection.configuration_version,
            "operationId": str(operation.pk), "goalId": None, "observedSourceSha": None, "verifiedSourceSha": None, "nativeSource": None,
            "attempt": number(operation.attempts, integer=True), "retryEpoch": None, "deploymentAttempts": None,
            "failure": {"code": safe_code(failure.get("code")), "class": failure.get("failure_class"),
                "effectiveClass": effective if failure and effective in CLASSES else None, "fingerprint": failure.get("failure_fingerprint")},
            "checks": [], "children": [], "receipts": [], "strategy": None, "budget": _budget_projection(selected)}
    return _finish(result, context, config, user, binding)
