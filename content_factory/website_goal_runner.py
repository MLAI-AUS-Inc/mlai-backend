"""Goal planning around the existing prepare lease and guarded action handlers."""

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError

TERMINAL = {"completed", "cancelled", "denied", "deleted", "stopped", "needs_attention"}


def owns_goal(op):
    """A reserved ledger never silently falls through to legacy redispatch."""
    return "goal" in goals.mapping(op.receipt)


def start_goal(current, *, company_id, user, retry, target, now, force_successor=False, consent=None):
    """Select/create one current goal while the caller holds the connection lock."""
    from .website_models import WebsiteConnectionOperation
    from .website_first_article_billing import reserve_or_wait
    scope = goals.scope_for(current, company_id)
    latest = current.operations.select_for_update().filter(generation=current.generation, action="prepare").order_by("-created_at", "-pk").first()
    goal = goals.mapping(getattr(latest, "receipt", None)).get("goal")
    from . import website_setup_consent as setup_consent
    selected = None
    if setup_consent.enabled(current.organization_id) and consent is not None:
        from .billing import get_content_factory_article_cost_points
        selected = setup_consent.answers(consent, expected_points=get_content_factory_article_cost_points(current.organization.domain))
    if target == "first_article_live" and not selected and not (isinstance(goal,dict) and setup_consent.valid(goal)):
        raise WebsiteAuthorityError("website_prepare_blocked", "Review the first-article choices before starting.", status=422)
    compatible = goals.valid_goal(goal, scope) and goal.get("target") == target
    if compatible and selected is not None and setup_consent.valid(goal) and selected != goal["consent"]["answers"]:
        force_successor = True
    if compatible and selected is not None and not setup_consent.valid(goal):
        # Explicit owner submission can enroll an existing ready goal without a new run.
        goal = {**goal, "consent": setup_consent.seal(goal, selected, user_id=user.pk, now=now), "revision": goal["revision"]+1}
        latest.receipt = {**latest.receipt, "goal": goal}
        latest.save(update_fields=["receipt", "updated_at"])
    source = getattr(current, "verified_sha", "")
    if latest and compatible and latest.state not in TERMINAL and not force_successor:
        if retry and latest.state == "needs_user":
            latest.state, latest.next_attempt_at = "pending", now
            latest.receipt = {**latest.receipt, "status": "working", "user_action": None, "dispatch_outcome": "replayed"}
            latest.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
        reserve_or_wait(latest, user=user, now=now)
        return latest
    if latest and latest.state not in TERMINAL and str(latest.payload.get("company_id")) != str(company_id):
        raise WebsiteAuthorityError("website_operation_changed", "A current website goal belongs to another selected company.")
    if latest and compatible and latest.state == "completed" and not force_successor and goal.get("completed_source_sha") == source and all(
            not item["required"] or goals.valid_proof(item["name"], item.get("proof"), scope=scope, source_sha=source, now=now)
            for item in goal["milestones"]):
        return latest
    if setup_consent.enabled(current.organization_id) and selected is None:
        if compatible and setup_consent.valid(goal):
            selected = goal["consent"]["answers"]
        else:
            raise WebsiteAuthorityError("website_prepare_blocked", "Review the setup choices before starting a new articles goal.", status=422)
    if latest and latest.state not in TERMINAL and not owns_goal(latest) and not force_successor:
        # Enrollment invalidates any old claim before it can overwrite the ledger.
        enrolled_goal = goals.new_goal(str(latest.pk), scope, target=target, now=now)
        if selected is not None:
            enrolled_goal["consent"] = setup_consent.seal(enrolled_goal, selected, user_id=user.pk, now=now)
        latest.receipt = {**latest.receipt, "goal": enrolled_goal}
        if latest.next_attempt_at is None and latest.state in {"pending", "running"}:
            latest.next_attempt_at = now
        latest.save(update_fields=["receipt", "next_attempt_at", "updated_at"])
        reserve_or_wait(latest, user=user, now=now)
        return latest
    if latest and latest.state not in TERMINAL:
        from .website_prepare import _cancel_prepare_child
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.get(website_connection=current)
        _cancel_prepare_child(latest, config)
        latest.state, latest.next_attempt_at = "cancelled", None
        latest.receipt = {**latest.receipt, "status": "superseded", "user_action": None}
        latest.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
        from .website_first_article_refunds import reserve_refund
        reserve_refund(latest, now=now)
    predecessor = str(latest.pk) if latest else None
    key = goals.digest({"scope": scope, "target": target, "source": source, "predecessor": predecessor})
    op, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{current.pk}:prepare:goal:{key}", defaults={
        "connection": current, "generation": current.generation, "action": "prepare", "state": "pending", "next_attempt_at": now,
        "payload": {"company_id": str(company_id), "requested_by_user_id": str(user.pk), "attempt": 1, "user_clicks": 1},
        "receipt": {"status": "working", "current_step": "verify-access", "message": "Preparing your website for articles."}})
    if created:
        goal = goals.new_goal(str(op.pk), scope, target=target, now=now)
        if selected is not None:
            goal["consent"] = setup_consent.seal(goal, selected, user_id=user.pk, now=now)
        goal["predecessor_goal_id"] = predecessor
        goal["current_source_sha"] = source
        op.receipt = {**op.receipt, "goal": goal}
        op.save(update_fields=["receipt", "updated_at"])
    elif not goals.valid_goal(goals.mapping(op.receipt).get("goal"), scope):
        raise WebsiteAuthorityError("website_operation_changed", "The saved website goal has changed. Refresh its current status.")
    reserve_or_wait(op, user=user, now=now,predecessor=latest)
    return op


def goal_journey(op, context, config, journey, *, now):
    """Choose the existing action from current validated milestone evidence."""
    from .website_goal_evidence import collect_proofs
    scope = goals.scope_for(config.website_connection, context.company.pk)
    goal = goals.mapping(op.receipt).get("goal")
    if not goals.valid_goal(goal, scope):
        raise WebsiteAuthorityError("website_operation_changed", "Website goal authority changed. Refresh the current website.")
    from .website_setup_consent import valid as valid_consent
    if goal.get("consent") is not None and not valid_consent(goal) or goal.get("target") == "first_article_live" and not valid_consent(goal):
        raise WebsiteAuthorityError("website_prepare_blocked", "The saved setup consent has changed. Review the current goal choices.")
    try:
        proofs, source = collect_proofs(op, context, config, journey, now=now)
    except (ValueError, TypeError, OverflowError) as exc:
        raise WebsiteAuthorityError("website_prepare_blocked", "Recorded milestone proof is malformed. Review the current goal details.") from exc
    if goal.get("strategy_requires_candidate"):
        from .website_goal_evidence import _child
        child = _child(op, config.website_connection)
        original = goals.mapping(getattr(child, "run_request", None))
        pin = goals.mapping(goal.get("strategy_pin"))
        fresh_candidate = (goals.mapping(proofs.get("integrated")).get("kind") == "setup_candidate"
            and original.get("strategy_goal_id") == goal["goal_id"]
            and original.get("strategy_revision") == pin.get("revision")
            and original.get("strategy_binding") == pin.get("binding"))
        if fresh_candidate:
            goal = {**goal, "strategy_requires_candidate": False}
        else:
            proofs = {name: proof for name, proof in proofs.items() if name in {"access", "understood"}}
    goal = goals.refresh_goal(goal, proofs, scope=scope, source_sha=source, now=now)
    goal["current_source_sha"] = source
    from . import website_strategy as strategy, website_goal_budget as budget
    if budget.enabled():
        goal = account_child_usage(op, goal)
    try:
        goal = strategy.ensure_pin(goal, journey, source=source, now=now)
    except ValueError as exc:
        raise WebsiteAuthorityError("website_prepare_blocked", "The saved strategy does not match current website proof.") from exc
    if goals.complete(goal):
        goal["completed_source_sha"] = source
    op.receipt = {**op.receipt, "goal": goal}
    milestone = goals.next_milestone(goal)
    op._goal_milestone = milestone
    action = goals.ACTION_FOR.get(milestone)
    if milestone and budget.enabled():
        reason = budget.exhaustion(goal, now=now, action=None if getattr(op, "_goal_has_active_child", False) else action)
        if reason:
            if getattr(op, "_goal_has_active_child", False):
                from .website_prepare import _cancel_prepare_child
                _cancel_prepare_child(op, config)
            stop_goal(op, goal, reason=reason, now=now)
            op._goal_wait = True
            return {**journey, "capabilities": {**goals.mapping(journey.get("capabilities")), "canPublishArticle": False}, "nextAction": None}
    if milestone and goals.mapping(goal.get("strategy_pin")).get("rung") == "hosted":
        wait_for_channel(op, goal)
        op._goal_wait = True
        return {**journey, "capabilities": {**goals.mapping(journey.get("capabilities")), "canPublishArticle": False}, "nextAction": None}
    if milestone and not action:
        raise WebsiteAuthorityError("website_prepare_blocked", "This goal's article publication handler is not available yet.", status=422)
    if milestone in {"approved", "merged"}:
        if not op.receipt.get("setup_run_id"):
            raise WebsiteAuthorityError("website_prepare_blocked", "No reserved setup proposal proves this goal's approval or merge.")
        op.receipt["child_run_id"] = op.receipt["setup_run_id"]
    from .website_setup_consent import required_grants
    if milestone and milestone != "access" and valid_consent(goal):
        answers = goal["consent"]["answers"]
        grants = goals.mapping(journey.get("githubPermissions"))
        required = required_grants(ci_mode=answers["ci_mode"], strategy=goals.mapping(goal.get("strategy_pin")).get("rung"))
        missing = [name for name in required if grants.get(name) is not True]
        if missing:
            if grants.get("retryable") is True:
                raise WebsiteAuthorityError("github_unavailable", "GitHub permission checks are temporarily unavailable.", retryable=True)
            action = "permissions"
            journey = {**journey, "prerequisites": {**goals.mapping(journey.get("prerequisites")), "permissions": {
                "status": "blocked", "reasonCode": "github_workflow_permission_required", "reason": "Approve the GitHub access shown in your articles setup choices."}}}
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
    from .article_publication_policy import journey_can_publish
    return journey_can_publish(journey)


def finish_goal(op, *, now=None):
    """Attach deduplicated class accounting without changing causal outcomes."""
    from django.utils import timezone
    now = now or timezone.now()
    goal = goals.mapping(op.receipt.get("goal"))
    if not goals.valid_goal(goal, goals.scope_for(op.connection, op.payload.get("company_id"))):
        return
    from . import website_goal_budget as budget
    if budget.enabled():
        try:
            goal = budget.update_owner_wait(goal, waiting=op.state == "needs_user" and bool(goals.mapping(op.receipt.get("user_action")).get("id")), now=now)
            op.receipt["goal"] = goal
        except ValueError:
            stop_goal(op, goal, reason="invalid_budget_contract", now=now)
            return
    if op.receipt.get("strategy_observation_invalid"):
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="stopped", message="Strategy evidence does not match the saved website goal.",
            user_action={"id": "inspect_goal", "label": "Review goal evidence"})
        return
    failure = goals.mapping(op.receipt.get("failure"))
    classification = failure.get("failure_class") or op.receipt.get("failure_class")
    occurrence = op.receipt.get("failure_occurrence_id")
    if occurrence and classification:
        try:
            fresh = occurrence not in goal.get("failure_occurrences", [])
            goal = goals.record_failure(goal, failure_class=classification,
                fingerprint=failure.get("failure_fingerprint"), occurrence=occurrence)
            from . import website_goal_budget as budget, website_strategy as strategy
            effective = classification
            if budget.enabled():
                goal, effective, fresh = budget.record_policy_occurrence(goal, occurrence=occurrence,
                    fingerprint=failure.get("failure_fingerprint"), classification=classification)
                op.receipt["effective_failure_class"] = effective
            reason = budget.exhaustion(goal, now=now) if budget.enabled() else None
            if reason:
                stop_goal(op, goal, reason=reason, now=now)
                return
            from .website_article_recovery import schedule as schedule_article_recovery
            if schedule_article_recovery(op,goal,classification=effective,fresh=fresh,occurrence=occurrence,now=now):
                return
            if fresh and effective == "repairable" and (budget.enabled() or strategy.enabled()):
                goal = retry_goal_action(op, goal, now=now, occurrence=occurrence)
            elif fresh and strategy.enabled() and effective == "strategy" and goal.get("strategy_pin"):
                goal = strategy.advance(goal, source=goal.get("current_source_sha"), now=now, reason="strategy_exhausted")
                goal["strategy_requires_candidate"] = True
                if goal["strategy_pin"]["rung"] == "hosted":
                    wait_for_channel(op, goal)
                else:
                    goal = retry_goal_action(op, goal, now=now, occurrence=occurrence, step="setup")
                    op.receipt["message"] = "Trying the next articles integration strategy."
            elif fresh and strategy.enabled() and effective == "unsupported" and classification in {"transient", "repairable", "strategy"} and strategy.valid_pin(goal.get("strategy_pin")):
                goal = strategy.set_pin(goal, len(strategy.RUNGS) - 1, source=goal.get("current_source_sha"),
                    fingerprint=goal["strategy_pin"]["fingerprint_digest"], now=now, reason="fingerprint_guard")
                wait_for_channel(op, goal)
            elif fresh and strategy.enabled() and effective in {"strategy", "unsupported"} and not goal.get("strategy_pin"):
                goal = strategy.set_pin(goal, len(strategy.RUNGS) - 1, source=goal.get("current_source_sha"),
                    fingerprint=goals.digest({"scope": goal["scope"], "failure": failure.get("failure_fingerprint")}), now=now, reason="inventory_exhausted")
                wait_for_channel(op, goal)
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
            from .website_goal_scope import rebind_source_scope
            rebound = rebind_source_scope(goal, connection, company_id=company_id, now=now)
            if rebound is None:
                return None
            goal = rebound
        goal = retain_child_references(goal, latest.receipt)
        _cancel_prepare_child(latest, config)
        goal = goals.refresh_goal(goal, {}, scope=goal["scope"], source_sha=connection.verified_sha, now=now)
        goal["current_source_sha"] = connection.verified_sha
        receipt = {key: value for key, value in latest.receipt.items() if key not in
            {"scan_run_id", "setup_run_id", "child_run_id", "hosted_verification_run_id", "completed_steps", "child_resume_pending"}}
        latest.receipt = {**receipt, "goal": goal, "status": "working", "current_step": "scan", "user_action": None,
            "last_completed": None, "reason_code": "", "message": "Rechecking the changed website source."}
        latest.payload = {**{key: value for key, value in latest.payload.items() if key != "run_id"},
            "source_reverify": True, "attempt": latest.payload.get("attempt", 1) + 1,
            "retry_epoch": goals.digest({"goal_id": goal["goal_id"], "source": connection.verified_sha, "revision": goal["revision"]})}
        latest.state, latest.next_attempt_at = "pending", now
        latest.save(update_fields=["receipt", "payload", "state", "next_attempt_at", "updated_at"])
        return latest


def wait_for_channel(op, goal):
    """Keep unavailable hosted fallback explicit until E5 supplies its handler."""
    op.state, op.next_attempt_at = "needs_user", None
    op.receipt = {**op.receipt, "goal": goal, "status": "needs_you", "channel_options": ["hosted", "portable"],
        "message": "The repository strategies are exhausted. Choose an articles channel.",
        "user_action": {"id": "choose_article_channel", "label": "Choose articles channel"}}


def stop_goal(op, goal, *, reason, now):
    """Record one bounded budget stop and durable alert reference, without sending."""
    if not goal.get("budget_alert"):
        goal["budget_alert"] = {"id": goals.digest({"goal": goal["goal_id"], "reason": reason}),
            "reason": reason, "at": now.isoformat(), "state": "pending"}
    op.state, op.next_attempt_at = "needs_attention", None
    op.receipt = {**op.receipt, "goal": goal, "status": "stopped", "budget_stop_reason": reason,
        "message": "The articles goal reached its execution budget. Review its recorded usage.",
        "user_action": {"id": "inspect_goal", "label": "Review goal budget"}}


def account_child_usage(op, goal):
    """Read metering only from original scoped children; missing reports stay unknown."""
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import deletion_epoch
    from . import website_goal_budget as budget
    result = goal
    op._goal_has_active_child = False
    identifiers = set(result.get("child_run_history") or []) | {value for key in ("child_run_id", "scan_run_id", "setup_run_id", "first_article_run_id", "first_article_publish_run_id")
        if isinstance(value := goals.mapping(op.receipt).get(key), str) and value}
    for identifier in sorted(identifiers):
        child = ContentFactoryRun.objects.filter(run_id=identifier, organization_id=op.connection.organization_id).first()
        saved = goals.mapping(getattr(child, "run_request", None))
        if not child or (saved.get("website_connection_id") != str(op.connection_id)
                or saved.get("connection_generation") != op.generation or saved.get("repository_id") != op.connection.repository_id
                or saved.get("deletion_epoch") != deletion_epoch(op.connection)):
            continue
        if child.status not in {"failed", "completed", "blocked", "cancelled", "denied"}:
            op._goal_has_active_child = True
        report = goals.mapping(child.result).get("usage")
        if report is None:
            if child.status not in {"failed", "completed", "blocked", "cancelled", "denied"}:
                continue
            missing = list(result.get("missing_usage_run_ids") or [])
            if identifier not in missing:
                result = {**result, "missing_usage_run_ids": (missing + [identifier])[-64:]}
            continue
        try:
            result = budget.account_usage(result, report, run_id=identifier)
            result["missing_usage_run_ids"] = [value for value in result.get("missing_usage_run_ids", []) if value != identifier]
        except ValueError:
            result = {**result, "usage_invalid": True}
    return result


def retry_goal_action(op, goal, *, now, occurrence, step=None):
    """Retry a repairable action on its rung with a distinct, durable attempt key."""
    goal = retain_child_references(goal, op.receipt)
    step = step or op.receipt.get("current_step") or "setup"
    op.payload = {**op.payload, "attempt": op.payload.get("attempt", 1) + 1,
        "retry_epoch": goals.digest({"goal": goal["goal_id"], "strategy": goal.get("strategy_pin"), "occurrence": occurrence})}
    op.payload.pop("run_id", None)
    cleared = {"child_resume_pending"}
    if step in {"scan", "setup"}:
        cleared.update({"child_run_id", "completed_steps"})
    if step == "scan":
        cleared.add("scan_run_id")
    elif step == "setup":
        cleared.add("setup_run_id")
    op.receipt = {key: value for key, value in op.receipt.items() if key not in cleared}
    op.receipt["goal"] = goal
    op.receipt.update(status="working", current_step=step, user_action=None,
        message="Repairing the articles setup on the saved integration strategy.")
    op.state, op.next_attempt_at = "pending", now
    return goal


def retain_child_references(goal, receipt):
    """Preserve cumulative metering references before replacing a child attempt."""
    history=list(goal.get("child_run_history") or [])
    for key in ("child_run_id","scan_run_id","setup_run_id","hosted_verification_run_id","first_article_run_id","first_article_publish_run_id"):
        identifier=goals.mapping(receipt).get(key)
        if isinstance(identifier,str) and identifier not in history:
            if len(history)>=64:
                raise ValueError("The bounded child accounting journal is full")
            history.append(identifier)
    return {**goal,"child_run_history":history}
