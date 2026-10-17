"""Owner-scoped native progress and diagnostics; reads never schedule work."""

from copy import deepcopy

from django.conf import settings
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_budget as budget
from . import cms_goal_contract as goals
from . import cms_goal_dispatch as dispatch
from . import cms_goal_evidence as evidence
from . import cms_goal_achievement as achievement
from .cms_connection_authority import validate_current
from .website_contract import WebsiteAuthorityError
from .website_doctor import safe_code, safe_identifier, number, STATES
from .website_failure_contract import validate_failure

def _public_url(value, binding, identifier):
    from .cms_inventory_contract import _public_url as validate_url, _identifier
    try:
        return validate_url(value, binding, _identifier(identifier, binding["platform"]))
    except (ValueError, TypeError, AttributeError):
        return None


def _base(binding, connection):
    return {"version": 1, "channel": "cms", "mode": "goal", "companyId": binding["company_id"],
        "connectionId": binding["connection_id"], "connectionGeneration": binding["generation"],
        "configurationRevision": connection.configuration_version, "goalId": None, "target": None,
        "operationId": None, "updatedAt": None, "nextAttemptAt": None, "step": None, "expectedTime": None,
        "state": "stopped", "title": "Set up articles", "message": "Set up articles to publish your first article.",
        "action": {"id": "cms_configure", "label": "Set up articles"}, "milestones": [], "evidence": []}


def _budget_projection(selected):
    from .website_goal import CLASSES
    used, attempts = (selected or {}).get("budget_used", {}), (selected or {}).get("attempts_by_class", {})
    return {**{public: number(used.get(private), integer=public in {"modelCalls", "tokens", "builds", "previews"})
        for public, private in (("activeSeconds", "active_seconds"), ("modelUsd", "model_cost"), ("modelCalls", "model_calls"),
            ("tokens", "tokens"), ("providerSeconds", "provider_seconds"), ("builds", "builds"), ("previews", "previews"))},
        "attemptsByClass": {name: number(attempts.get(name), integer=True) for name in CLASSES},
        "limits": {"wallSeconds": number(settings.ARTICLES_GOAL_WALL_SECONDS), "activeSeconds": number(settings.ARTICLES_GOAL_ACTIVE_SECONDS),
            "modelUsd": number(settings.ARTICLES_GOAL_MODEL_USD)}}


def _doctor(operation, value, selected, milestones, *, original_source, child=None):
    """Export bounded native facts without private policy, request or money IDs."""
    from .website_goal import CLASSES
    try: failure = validate_failure(operation.receipt.get("failure"))
    except ValueError: failure = {}
    checks = []
    for row in milestones:
        proof = row.get("proof")
        checks.append({"name": row["name"], "state": row["state"], "proof": {
            "kind": proof["kind"], "sha": None, "runId": safe_identifier(proof["source"]["run_id"]) if proof["source"] is not None else None,
            "digest": proof["subject_digest"], "at": proof["at"]} if proof else None})
    effective = operation.receipt.get("effective_failure_class")
    binding = value.scope["binding"]
    if original_source is not None and safe_identifier(original_source["run_id"]) is None:
        original_source = None
    return {"version": 1, "channel": "cms", "companyId": binding["company_id"], "connectionId": binding["connection_id"],
        "connectionGeneration": binding["generation"], "configurationRevision": operation.connection.configuration_version,
        "operationId": str(operation.pk), "goalId": value.goal["goal_id"], "observedSourceSha": None, "verifiedSourceSha": None,
        "nativeSource": {"runId": original_source["run_id"], "generation": original_source["run_generation"],
            "artifactDigest": original_source["envelope_digest"]} if original_source is not None else None,
        "attempt": number(operation.attempts, integer=True), "retryEpoch": None, "deploymentAttempts": None,
        "failure": {"code": safe_code(failure.get("code")), "class": failure.get("failure_class"),
            "effectiveClass": effective if failure and effective in CLASSES else None, "fingerprint": failure.get("failure_fingerprint")},
        "checks": checks, "children": [{"runId": original_source["run_id"], "status": child.status}]
            if original_source is not None and child is not None and child.run_id == original_source["run_id"] and child.status in STATES else [],
        "receipts": [], "strategy": None,
        "budget": _budget_projection(selected)}


def _review_action(operation, context, config, *, user, publish_mode):
    """An owner action must still identify its actual current private stage."""
    from . import cms_article_approval as approvals
    from .website_connections import contract_for
    choice = operation.receipt.get("user_action")
    if not isinstance(choice, dict): return None
    if choice.get("id") == "connect_cms":
        return {"id": "connect_cms", "label": "Reconnect CMS", "operationId": str(operation.pk)}
    if choice.get("id") != "review_draft" or publish_mode != "review": return None
    stage, value = approvals.owner_stage(context, config, user=user,
        data={**contract_for(config.website_connection), "stage_id": choice.get("stageId")})
    if (stage.payload.get("review_policy") != "review" or str(stage.pk) != choice.get("stageId") or
            choice.get("runId") != operation.payload["source_run_id"] or value["source_run_id"] != choice["runId"] or
            safe_identifier(value["source_run_id"]) is None or
            value["revision"] != choice.get("reviewRevision")):
        return None
    return {"id": "review_draft", "label": "Review first article", "operationId": str(operation.pk),
        "runId": value["source_run_id"], "stageId": str(stage.pk), "reviewRevision": value["revision"]}


def project(operation, context, config, *, user, binding, now):
    """Recheck owning evidence rather than publish copied completion booleans."""
    from .website_models import WebsiteConnectionOperation
    result = _base(binding, config.website_connection)
    result.update(operationId=str(operation.pk), updatedAt=operation.updated_at.isoformat(), action={
        "id": "inspect_goal", "label": "View details", "operationId": str(operation.pk)})
    try:
        value = authority.historical(operation)
        if value.scope["binding"] != binding: return None
        selected = budget.validate(operation.receipt.get("native_budget"), value.goal, now=now)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result.update(title="Article setup needs attention", message="The saved articles goal needs verification.")
        return result
    current, references, original_source, allowed, actual = value.goal, {}, None, False, None
    achieved, proof_at = None, now
    try:
        authority.current(operation, user=user, states={operation.state})
        allowed = True
        original = dispatch.original_dispatch(operation)
        run = dispatch.original_run(operation, original) if original is not None else None
        actual = run if run is not None and isinstance(run.result, dict) and type(run.result.get("generation")) is int and run.run_id != original.client_request_id else None
        if operation.state == "completed" and "publication_achievement" in operation.receipt:
            achieved = achievement.read(operation, user=user, now=now)
            current, proof_at, original_source = achieved["goal"], achieved["completed_at"], achieved["goal"]["source"]
        else:
            proofs, original_source, references = evidence.collect(current, context, config, user=user, run=actual,
                artifact_operation_id=original.payload["request"]["publishing_channel_artifact"]["artifact_operation_id"] if original is not None else None,
                capture_operation_id=operation.payload["capture_operation_id"], now=now)
            if original_source is not None: current = goals.bind_source(current, original_source, expected_scope=value.scope, now=now)
            current = goals.refresh(current, proofs, expected_scope=value.scope, now=now)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        current = goals.refresh(current, {}, expected_scope=value.scope, now=now)
    candidate_proofs = {row["name"]: row["proof"] for row in current["milestones"]}
    milestones = [{"name": row["name"], "state": "passed" if goals.milestone_passed(row["name"], row["proof"], proofs=candidate_proofs,
        expected_scope=value.scope, original_source=current["source"], now=proof_at) else "pending" if row["required"] else "not_required",
        "required": row["required"], "proof": deepcopy(row["proof"])} for row in current["milestones"]]
    for row in milestones:
        if row["state"] != "passed": row["proof"] = None
    result.update(goalId=current["goal_id"], target="first_article_live", milestones=[{k: row[k] for k in ("name", "state", "required")} for row in milestones],
        step=next((row["name"] for row in milestones if row["required"] and row["state"] != "passed"), None),
        evidence=[{"id": "milestone:" + row["name"], "result": "passed" if row["state"] in {"passed", "not_required"} else "pending",
            "at": row["proof"]["at"] if row["proof"] else None, "message": row["name"].replace("_", " ")} for row in milestones])
    if achieved is not None:
        public_url = _public_url(achieved["public_url"], binding, achieved["provider_id"])
        if public_url is not None:
            result.update(state="done", title="First article published", message="Your first article was published. The checklist records the completed setup.",
                action=None, publicUrl=public_url, publishedAt=achieved["published_at"])
    elif operation.state == "completed" and allowed and goals.complete(current, expected_scope=value.scope, now=now):
        row = WebsiteConnectionOperation.objects.filter(pk=references.get("first_article_live"), action="cms-publish", state="completed",
            connection_id=operation.connection_id, generation=operation.generation).first()
        public_url = _public_url(row.receipt.get("publicUrl") if row is not None else None, binding,
            (row.payload.get("post_state") or {}).get("provider_id") if row is not None else None)
        if public_url is not None:
            result.update(state="done", title="First article live", message="Your first article is live.", action=None, publicUrl=public_url)
    elif operation.state == "needs_user":
        try: action = _review_action(operation, context, config, user=user,
            publish_mode=value.consent["answers"]["publish_mode"]) if allowed or (operation.receipt.get("user_action") or {}).get("id") == "connect_cms" else None
        except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError): action = None
        if action is not None:
            result.update(state="needs_you", title="Your action is needed", message="Review your first article." if action["id"] == "review_draft" else "Restore access to your CMS.", action=action)
            if action["id"] == "connect_cms" and operation.next_attempt_at is not None:
                result["nextAttemptAt"] = operation.next_attempt_at.isoformat()
    elif operation.state in {"pending", "running"} and allowed and operation.next_attempt_at is not None:
        result.update(state="working", title="Setting up articles", message="Preparing your first article.", action=None,
            nextAttemptAt=operation.next_attempt_at.isoformat())
    if result["state"] == "stopped":
        result.update(title="Article setup needs attention", message="Review the recorded articles goal details.")
    from .cms_unpublish_status import apply as withdrawal_progress
    withdrawal_progress(result, operation, context, config, user=user)
    from .cms_access_status import apply as access_progress
    access_progress(result, operation)
    if getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
        result["doctor"] = _doctor(operation, value, selected, milestones, original_source=original_source, child=actual)
    # A credential refusal may still permit the same owner to read diagnostics;
    # a changed selected company/owner never does. Recheck without a provider call.
    for row in (context.organization, context.company, context.profile, user, config, config.website_connection): row.refresh_from_db()
    if validate_current(context, config, config.website_connection, user=user) != binding:
        return None
    return result


def for_context(context, config, *, user, now=None):
    """Select only the current authenticated founder's native goal; no effects."""
    if (not getattr(settings, "ARTICLES_CMS_STATUS_ENABLED", False) or not authority.enabled(context.organization.pk) or
            user is None or config.website_connection is None): return None
    try:
        for row in (context.organization, context.company, context.profile, user, config, config.website_connection): row.refresh_from_db()
        binding = validate_current(context, config, config.website_connection, user=user)
        operation = config.website_connection.operations.filter(action__in=[authority.ACTION, "cms-setup"], generation=binding["generation"],
            payload__company_id=binding["company_id"]).order_by("-created_at", "-pk").first()
        if operation is None:
            from .cms_setup_quote import for_context as setup_quote
            result = _base(binding, config.website_connection)
            quote = setup_quote(context, config, user=user, expected_binding=binding)
            if quote is not None: result["nativeSetup"] = quote
            else: result.update(action=None, title="Article setup unavailable", message="Article setup is currently unavailable.")
            # A start may have committed while the quote was being read. Show
            # that actual operation instead of offering another setup action.
            started = config.website_connection.operations.filter(action__in=[authority.ACTION, "cms-setup"], generation=binding["generation"],
                payload__company_id=binding["company_id"]).order_by("-created_at", "-pk").first()
            if started is not None:
                from .cms_setup_status import project as setup_status
                return (setup_status if started.action == "cms-setup" else project)(started, context, config, user=user, binding=binding, now=now or timezone.now())
            for row in (context.organization, context.company, context.profile, user, config, config.website_connection): row.refresh_from_db()
            if validate_current(context, config, config.website_connection, user=user) != binding: return None
            return result
        from .cms_setup_status import project as setup_status
        return (setup_status if operation.action == "cms-setup" else project)(operation, context, config, user=user, binding=binding, now=now or timezone.now())
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
