"""Goal planning around the existing prepare lease and guarded action handlers."""

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError

TERMINAL = {"completed", "cancelled", "denied", "deleted", "stopped", "needs_attention"}


def owns_goal(op):
    """A reserved ledger never silently falls through to legacy redispatch."""
    return "goal" in goals.mapping(op.receipt)


def start_goal(current, *, company_id, user, retry, target, now, force_successor=False):
    """Select/create one current goal while the caller holds the connection lock."""
    from .website_models import WebsiteConnectionOperation
    scope = goals.scope_for(current, company_id)
    latest = current.operations.select_for_update().filter(generation=current.generation, action="prepare").order_by("-created_at", "-pk").first()
    goal = goals.mapping(getattr(latest, "receipt", None)).get("goal")
    compatible = goals.valid_goal(goal, scope) and goal.get("target") == target
    source = getattr(current, "verified_sha", "")
    if latest and compatible and latest.state not in TERMINAL and not force_successor:
        if retry and latest.state == "needs_user":
            latest.state, latest.next_attempt_at = "pending", now
            latest.receipt = {**latest.receipt, "status": "working", "user_action": None, "dispatch_outcome": "replayed"}
            latest.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
        return latest
    if latest and latest.state not in TERMINAL and str(latest.payload.get("company_id")) != str(company_id):
        raise WebsiteAuthorityError("website_operation_changed", "A current website goal belongs to another selected company.")
    if latest and compatible and latest.state == "completed" and not force_successor and goal.get("completed_source_sha") == source and all(
            not item["required"] or goals.valid_proof(item["name"], item.get("proof"), scope=scope, source_sha=source, now=now)
            for item in goal["milestones"]):
        return latest
    if latest and latest.state not in TERMINAL and not owns_goal(latest) and not force_successor:
        # Enrollment invalidates any old claim before it can overwrite the ledger.
        latest.receipt = {**latest.receipt, "goal": goals.new_goal(str(latest.pk), scope, target=target, now=now)}
        if latest.next_attempt_at is None and latest.state in {"pending", "running"}:
            latest.next_attempt_at = now
        latest.save(update_fields=["receipt", "next_attempt_at", "updated_at"])
        return latest
    if latest and latest.state not in TERMINAL:
        from .website_prepare import _cancel_prepare_child
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.get(website_connection=current)
        _cancel_prepare_child(latest, config)
        latest.state, latest.next_attempt_at = "cancelled", None
        latest.receipt = {**latest.receipt, "status": "superseded", "user_action": None}
        latest.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
    predecessor = str(latest.pk) if latest else None
    key = goals.digest({"scope": scope, "target": target, "source": source, "predecessor": predecessor})
    op, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{current.pk}:prepare:goal:{key}", defaults={
        "connection": current, "generation": current.generation, "action": "prepare", "state": "pending", "next_attempt_at": now,
        "payload": {"company_id": str(company_id), "requested_by_user_id": str(user.pk), "attempt": 1, "user_clicks": 1},
        "receipt": {"status": "working", "current_step": "verify-access", "message": "Preparing your website for articles."}})
    if created:
        goal = goals.new_goal(str(op.pk), scope, target=target, now=now)
        goal["predecessor_goal_id"] = predecessor
        goal["current_source_sha"] = source
        op.receipt = {**op.receipt, "goal": goal}
        op.save(update_fields=["receipt", "updated_at"])
    elif not goals.valid_goal(goals.mapping(op.receipt).get("goal"), scope):
        raise WebsiteAuthorityError("website_operation_changed", "The saved website goal has changed. Refresh its current status.")
    return op


def goal_journey(op, context, config, journey, *, now):
    """Choose the existing action from current validated milestone evidence."""
    from .website_goal_evidence import collect_proofs
    scope = goals.scope_for(config.website_connection, context.company.pk)
    goal = goals.mapping(op.receipt).get("goal")
    if not goals.valid_goal(goal, scope):
        raise WebsiteAuthorityError("website_operation_changed", "Website goal authority changed. Refresh the current website.")
    try:
        proofs, source = collect_proofs(op, context, config, journey, now=now)
    except (ValueError, TypeError, OverflowError) as exc:
        raise WebsiteAuthorityError("website_prepare_blocked", "Recorded milestone proof is malformed. Review the current goal details.") from exc
    goal = goals.refresh_goal(goal, proofs, scope=scope, source_sha=source, now=now)
    goal["current_source_sha"] = source
    if goals.complete(goal):
        goal["completed_source_sha"] = source
    op.receipt = {**op.receipt, "goal": goal}
    milestone = goals.next_milestone(goal)
    op._goal_milestone = milestone
    action = goals.ACTION_FOR.get(milestone)
    if milestone and not action:
        raise WebsiteAuthorityError("website_prepare_blocked", "This goal's article publication handler is not available yet.", status=422)
    if milestone in {"approved", "merged"}:
        if not op.receipt.get("setup_run_id"):
            raise WebsiteAuthorityError("website_prepare_blocked", "No reserved setup proposal proves this goal's approval or merge.")
        op.receipt["child_run_id"] = op.receipt["setup_run_id"]
    prerequisite = goals.mapping(goals.mapping(journey.get("prerequisites")).get("permissions"))
    if action in {"setup", "ci-attestation", "verify", "merge"} and prerequisite.get("status") not in {None, "complete"}:
        action = "permissions"
    projected = {**journey, "capabilities": {**goals.mapping(journey.get("capabilities")), "canPublishArticle": goals.complete(goal)},
        "nextAction": {"id": action, "milestone": milestone} if action else None}
    return projected


def ready(op, journey):
    """Preserve legacy behavior only for operations with no reserved goal."""
    if owns_goal(op):
        return goals.complete(goals.mapping(op.receipt).get("goal"))
    return goals.mapping(journey.get("capabilities")).get("canPublishArticle") is True


def finish_goal(op):
    """Attach deduplicated class accounting without changing causal outcomes."""
    goal = goals.mapping(op.receipt.get("goal"))
    if not goals.valid_goal(goal, goals.scope_for(op.connection, op.payload.get("company_id"))):
        return
    failure = goals.mapping(op.receipt.get("failure"))
    classification = failure.get("failure_class") or op.receipt.get("failure_class")
    occurrence = op.receipt.get("failure_occurrence_id")
    if occurrence and classification:
        try:
            goal = goals.record_failure(goal, failure_class=classification,
                fingerprint=failure.get("failure_fingerprint"), occurrence=occurrence)
            op.receipt = {**op.receipt, "goal": goal}
        except ValueError:
            op.state, op.next_attempt_at = "needs_attention", None
            op.receipt = {**op.receipt, "status": "stopped", "reason_code": "prepare_no_progress", "retryable": False,
                "message": "The goal occurrence journal is full. Review the recorded goal before starting a new one.",
                "user_action": {"id": "open_run", "label": "Review goal", "run_id": op.receipt.get("setup_run_id")}}


def wake_goal_for_source(config, *, company_id, user, now):
    """Preserve terminal history and active-goal budgets on an accepted source change."""
    from .website_connections import authority_guard, contract_for
    from .website_prepare import start_prepare, _cancel_prepare_child
    with authority_guard(contract_for(config.website_connection), action="read") as connection:
        latest = connection.operations.select_for_update().filter(generation=connection.generation,
            action="prepare").order_by("-created_at", "-pk").first()
        if latest is None or not owns_goal(latest):
            return None
        goal = goals.mapping(latest.receipt.get("goal"))
        if not goals.enrolled(connection.organization_id) or latest.state in {"cancelled", "denied", "deleted"}:
            return None
        if goal.get("current_source_sha") == connection.verified_sha:
            return latest
        if latest.state in TERMINAL:
            return start_prepare(config, company_id=company_id, user=user, force_successor=True, target=goal.get("target", "ready"))
        if not goals.valid_goal(goal, goals.scope_for(connection, company_id)):
            return None
        _cancel_prepare_child(latest, config)
        goal = goals.refresh_goal(goal, {}, scope=goal["scope"], source_sha=connection.verified_sha, now=now)
        goal["current_source_sha"] = connection.verified_sha
        receipt = {key: value for key, value in latest.receipt.items() if key not in
            {"scan_run_id", "setup_run_id", "child_run_id", "completed_steps", "child_resume_pending"}}
        latest.receipt = {**receipt, "goal": goal, "status": "working", "current_step": "scan", "user_action": None,
            "last_completed": None, "reason_code": "", "message": "Rechecking the changed website source."}
        latest.payload = {**{key: value for key, value in latest.payload.items() if key != "run_id"},
            "source_reverify": True, "attempt": latest.payload.get("attempt", 1) + 1,
            "retry_epoch": goals.digest({"goal_id": goal["goal_id"], "source": connection.verified_sha, "revision": goal["revision"]})}
        latest.state, latest.next_attempt_at = "pending", now
        latest.save(update_fields=["receipt", "payload", "state", "next_attempt_at", "updated_at"])
        return latest
