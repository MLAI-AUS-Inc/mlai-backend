"""Fixed operational incident facts, separate from owner-action notifications."""
from datetime import timedelta
from django.conf import settings
from . import website_goal as goals, website_goal_budget as budget
from .website_doctor import number, safe_identifier

REASONS = {"wall_time", "active_compute", "model_cost", "transient_attempts", "repairable_attempts",
    "usage_unknown", "invalid_usage_report", "invalid_budget_contract"}
REPAIR_REASON = "setup_delivery_projection_repaired"
STALL_REASONS = {"waiting_policy_deadline", "deployment_policy_deadline", "transient_policy_deadline"}
STAGES = frozenset(goals.ACTION_FOR.values()) | {"permissions", "prepare", "publish"}
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


def repair_alert(connection, run, envelope, *, scope, goal_id=None, now):
    """Describe an accepted pull repair without inventing original goal attribution."""
    if not enabled(connection) or not isinstance(scope, dict) or not isinstance(envelope, dict):
        return None
    execution = goals.mapping(envelope.get("execution"))
    original = goals.mapping(envelope.get("scope"))
    projection = goals.mapping(goals.mapping(envelope.get("body")).get("state_projection"))
    if (envelope.get("run_id") != run.run_id or str(run.organization_id) != str(connection.organization_id)
            or scope.get("connection_id") != str(connection.pk)
            or scope.get("organization_id") != str(connection.organization_id)
            or original.get("website_connection_id") != str(connection.pk)
            or type(original.get("repository_id")) is not int or original["repository_id"] != connection.repository_id
            or any(type(execution.get(key)) is not int or execution[key] < 0 for key in ("generation", "state_version"))
            or any(projection.get(key) != execution[key] for key in ("generation", "state_version"))
            or not isinstance(envelope.get("state_sha256"), str) or not goals.DIGEST.fullmatch(envelope["state_sha256"])):
        return None
    occurrence = goals.digest({"run_id": run.run_id, "execution": {key: execution[key] for key in ("generation", "state_version")},
        "state_sha256": envelope["state_sha256"]})
    alert = {"version": 1, "kind": "repair", "severity": "warning", "reason": REPAIR_REASON,
        "goal_id": goal_id, "connection_id": str(connection.pk), "scope_digest": goals.digest(scope),
        "occurrence": occurrence, "at": now.isoformat(), "usage": {key: None for key in COUNTERS}}
    alert["id"] = goals.digest({"kind": "repair", "scope": scope, "goal_id": goal_id, "occurrence": occurrence})
    return alert if valid(alert, scope) else None


def record_policy_stall(op, *, step, reason, started_at, deadline, now):
    """Capture a deadline crossed by the owning policy, without another timeout."""
    if not enabled(op.connection):
        return
    goal = goals.mapping(op.receipt.get("goal"))
    scope = goals.scope_for(op.connection, goals.mapping(op.payload).get("company_id"))
    started = goals.stamp(started_at)
    if (not goals.valid_goal(goal, scope) or goal.get("goal_id") != str(op.pk)
            or reason not in STALL_REASONS or not started or not started < deadline <= now):
        return
    stage = step if step in STAGES else "prepare"
    occurrence = {"goal_id": str(op.pk), "scope_digest": goals.digest(scope), "stage": stage,
        "reason": reason, "started_at": started.isoformat(), "deadline": deadline.isoformat()}
    identity = goals.digest(occurrence)
    previous = goals.mapping(op.receipt.get("stage_policy_stall"))
    op.receipt["stage_policy_stall"] = previous if previous.get("id") == identity else {
        **occurrence, "id": identity, "at": now.isoformat()}


def stall_alert(op, *, now):
    """Project only the server's saved causal policy-expiry observation."""
    if not enabled(op.connection):
        return None
    goal = goals.mapping(op.receipt.get("goal"))
    scope = goals.scope_for(op.connection, goals.mapping(op.payload).get("company_id"))
    saved = goals.mapping(op.receipt.get("stage_policy_stall"))
    if (set(saved) != {"goal_id", "scope_digest", "stage", "reason", "started_at", "deadline", "id", "at"}
            or not goals.valid_goal(goal, scope) or saved.get("goal_id") != goal.get("goal_id")
            or saved.get("goal_id") != str(op.pk) or saved.get("scope_digest") != goals.digest(scope)
            or not isinstance(saved.get("stage"), str) or saved["stage"] not in STAGES
            or not isinstance(saved.get("reason"), str) or saved["reason"] not in STALL_REASONS
            or any(not isinstance(saved.get(key), str) for key in ("at", "started_at", "deadline"))
            or not goals.stamp(saved.get("at")) or goals.stamp(saved["at"]) > now + timedelta(minutes=5)):
        return None
    occurrence = {key: saved[key] for key in ("goal_id", "scope_digest", "stage", "reason", "started_at", "deadline")}
    if saved["id"] != goals.digest(occurrence):
        return None
    alert = {"version": 1, "kind": "stall", "severity": "warning", "reason": saved["reason"],
        "goal_id": str(op.pk), "connection_id": scope["connection_id"], "scope_digest": goals.digest(scope),
        "occurrence": saved["id"], "at": saved["at"], "usage": {key: None for key in COUNTERS},
        **{key: saved[key] for key in ("stage", "started_at", "deadline")}}
    alert["id"] = goals.digest({"kind": "stall", "scope": scope, "goal_id": str(op.pk), "occurrence": saved["id"]})
    return alert if valid(alert, scope) else None


def valid(alert, scope):
    """Stored incidents contain only allowlisted codes, safe references and numbers."""
    if (not isinstance(scope, dict) or set(scope) != set(goals.SCOPE_FIELDS)
            or any(not isinstance(scope[key], str) or len(scope[key]) > 500 for key in
                ("company_id", "organization_id", "connection_id", "repository_id", "branch", "app_root"))
            or any(type(scope[key]) is not int or not budget.numeric(scope[key]) or scope[key] < minimum for key, minimum in
                (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))):
        return False
    fields = {"version", "id", "kind", "severity", "reason", "goal_id", "connection_id", "scope_digest", "occurrence", "at", "usage"}
    if isinstance(alert, dict) and alert.get("kind") == "stall":
        fields |= {"stage", "started_at", "deadline"}
    if (not isinstance(alert, dict) or set(alert) != fields
            or type(alert.get("version")) is not int or alert["version"] != 1
            or not isinstance(alert.get("kind"), str) or alert["kind"] not in {"budget", "repair", "stall"}
            or alert.get("severity") != ("error" if alert["kind"] == "budget" else "warning")
            or not isinstance(alert.get("reason"), str) or alert["reason"] not in (
                REASONS if alert["kind"] == "budget" else STALL_REASONS if alert["kind"] == "stall" else {REPAIR_REASON})
            or (safe_identifier(alert.get("goal_id")) is None and not (alert["kind"] == "repair" and alert.get("goal_id") is None))
            or alert.get("connection_id") != scope.get("connection_id")
            or safe_identifier(alert.get("connection_id")) is None or alert.get("scope_digest") != goals.digest(scope)
            or not isinstance(alert.get("occurrence"), str) or not goals.DIGEST.fullmatch(alert["occurrence"])
            or alert["kind"] == "budget" and alert["occurrence"] != goals.digest({"goal": alert["goal_id"], "reason": alert["reason"]})
            or not goals.stamp(alert.get("at")) or not isinstance(alert.get("usage"), dict) or set(alert["usage"]) != set(COUNTERS)):
        return False
    if alert["kind"] == "stall":
        started, deadline, at = (goals.stamp(alert.get(key)) for key in ("started_at", "deadline", "at"))
        if (not isinstance(alert.get("stage"), str) or alert["stage"] not in STAGES
                or not started or not deadline or not started < deadline <= at
                or alert["occurrence"] != goals.digest({key: alert[key] for key in
                    ("goal_id", "scope_digest", "stage", "reason", "started_at", "deadline")})):
            return False
    for key, value in alert["usage"].items():
        if value is not None and (not budget.numeric(value) or key in {"model_calls", "tokens", "builds", "previews", "transient_attempts", "repairable_attempts"} and type(value) is not int):
            return False
    return alert["id"] == goals.digest({"kind": alert["kind"], "scope": scope,
        "goal_id": alert["goal_id"], "occurrence": alert["occurrence"]})


def text(alert):
    """Build fixed ops-only copy; no source files, provider text or owner email."""
    usage = goals.mapping(alert.get("usage"))
    measured = {key: number(usage.get(key), integer=key in {"model_calls", "tokens", "builds", "previews", "transient_attempts", "repairable_attempts"}) for key in COUNTERS}
    used = ", ".join(f"{key}={value}" for key, value in measured.items() if value is not None)
    reason = alert.get("reason") if isinstance(alert.get("reason"), str) and alert["reason"] in REASONS | STALL_REASONS | {REPAIR_REASON} else "unknown"
    goal_id = safe_identifier(alert.get("goal_id")) or "unavailable"
    incident = alert.get("id")
    incident = incident if isinstance(incident, str) and goals.DIGEST.fullmatch(incident) else "unavailable"
    kind = alert.get("kind") if isinstance(alert.get("kind"), str) and alert["kind"] in {"budget", "repair", "stall"} else "unknown"
    connection_id = safe_identifier(alert.get("connection_id")) or "unavailable"
    stage = alert.get("stage") if isinstance(alert.get("stage"), str) and alert["stage"] in STAGES else "unavailable"
    return (f"Articles operational alert: {kind}. Goal {goal_id}. Website {connection_id}. Reason: {reason}. "
        f"Stage: {stage}. "
        f"Execution budget counters (conservative bounds): {used or 'unavailable'}. Incident: {incident}.")
