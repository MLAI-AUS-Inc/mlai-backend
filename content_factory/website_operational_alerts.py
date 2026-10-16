"""Fixed operational incident facts, separate from owner-action notifications."""
from datetime import timedelta
from django.conf import settings
from . import website_goal as goals, website_goal_budget as budget
from .website_doctor import number, safe_identifier

REASONS = {"wall_time", "active_compute", "model_cost", "transient_attempts", "repairable_attempts",
    "usage_unknown", "invalid_usage_report", "invalid_budget_contract"}
COUNTERS = (*budget.COUNTERS, "transient_attempts", "repairable_attempts")


def enabled(connection):
    """Operational delivery has its own rollout flag and trusted goal cohort."""
    return getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False) and goals.enrolled(connection.organization_id)


def budget_alert(op, *, now):
    """A validated saved budget stop reserves one incident, never another user task."""
    if not enabled(op.connection):
        return None
    receipt = goals.mapping(op.receipt)
    goal = goals.mapping(receipt.get("goal"))
    scope = goals.scope_for(op.connection, goals.mapping(op.payload).get("company_id"))
    saved = goals.mapping(goal.get("budget_alert"))
    reason = saved.get("reason")
    at = goals.stamp(saved.get("at"))
    if (not goals.valid_goal(goal, scope) or goal.get("goal_id") != str(op.pk)
            or not isinstance(reason, str) or reason not in REASONS or not at or at > now + timedelta(minutes=5)
            or op.state != "needs_attention" or receipt.get("status") != "stopped"
            or receipt.get("budget_stop_reason") != reason
            or saved.get("id") != goals.digest({"goal": goal["goal_id"], "reason": reason})
            or safe_identifier(goal["goal_id"]) is None or safe_identifier(scope["connection_id"]) is None):
        return None
    used, attempts = goals.mapping(goal.get("budget_used")), goals.mapping(goal.get("attempts_by_class"))
    usage = {key: number(used.get(key, used.get("model_cost") if key == "model_usd" else None),
        integer=key in {"model_calls", "tokens", "builds", "previews"}) for key in budget.COUNTERS}
    usage.update({name + "_attempts": number(attempts.get(name), integer=True) for name in ("transient", "repairable")})
    identity = {"kind": "budget", "scope": scope, "goal_id": goal["goal_id"], "occurrence": saved["id"]}
    return {"version": 1, "id": goals.digest(identity), "kind": "budget", "severity": "error", "reason": reason,
        "goal_id": goal["goal_id"], "connection_id": scope["connection_id"], "scope_digest": goals.digest(scope),
        "occurrence": saved["id"], "at": at.isoformat(), "usage": usage}


def valid(alert, scope):
    """Stored incidents contain only allowlisted codes, safe references and numbers."""
    if (not isinstance(scope, dict) or set(scope) != set(goals.SCOPE_FIELDS)
            or any(not isinstance(scope[key], str) or len(scope[key]) > 500 for key in
                ("company_id", "organization_id", "connection_id", "repository_id", "branch", "app_root"))
            or any(type(scope[key]) is not int or not budget.numeric(scope[key]) or scope[key] < minimum for key, minimum in
                (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))):
        return False
    if (not isinstance(alert, dict) or set(alert) != {"version", "id", "kind", "severity", "reason", "goal_id", "connection_id", "scope_digest", "occurrence", "at", "usage"}
            or type(alert.get("version")) is not int or alert["version"] != 1 or alert.get("kind") != "budget"
            or alert.get("severity") != "error" or not isinstance(alert.get("reason"), str) or alert["reason"] not in REASONS
            or safe_identifier(alert.get("goal_id")) is None or alert.get("connection_id") != scope.get("connection_id")
            or safe_identifier(alert.get("connection_id")) is None or alert.get("scope_digest") != goals.digest(scope)
            or not isinstance(alert.get("occurrence"), str) or not goals.DIGEST.fullmatch(alert["occurrence"])
            or alert["occurrence"] != goals.digest({"goal": alert["goal_id"], "reason": alert["reason"]})
            or not goals.stamp(alert.get("at")) or not isinstance(alert.get("usage"), dict) or set(alert["usage"]) != set(COUNTERS)):
        return False
    for key, value in alert["usage"].items():
        if value is not None and (not budget.numeric(value) or key in {"model_calls", "tokens", "builds", "previews", "transient_attempts", "repairable_attempts"} and type(value) is not int):
            return False
    return alert["id"] == goals.digest({"kind": "budget", "scope": scope,
        "goal_id": alert["goal_id"], "occurrence": alert["occurrence"]})


def text(alert):
    """Build fixed ops-only copy; no source files, provider text or owner email."""
    usage = goals.mapping(alert.get("usage"))
    measured = {key: number(usage.get(key), integer=key in {"model_calls", "tokens", "builds", "previews", "transient_attempts", "repairable_attempts"}) for key in COUNTERS}
    used = ", ".join(f"{key}={value}" for key, value in measured.items() if value is not None)
    reason = alert.get("reason") if isinstance(alert.get("reason"), str) and alert["reason"] in REASONS else "unknown"
    goal_id = safe_identifier(alert.get("goal_id")) or "unavailable"
    incident = alert.get("id")
    incident = incident if isinstance(incident, str) and goals.DIGEST.fullmatch(incident) else "unavailable"
    return (f"Articles operational alert: budget. Goal {goal_id}. Reason: {reason}. "
        f"Execution budget counters (conservative bounds): {used or 'unavailable'}. Incident: {incident}.")
