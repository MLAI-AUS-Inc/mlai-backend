"""Server-owned observations from the original goal's actual controller path.

This bounded journal proves only which backend process admitted/advanced a goal.
It never asserts exhaustive owner actions, scheduler/fleet coverage or CF work.
"""
from copy import deepcopy

from . import website_goal as goals

KEY = "backend_execution_observations_v1"
MAX_RECORDS = 256


def _authority(goal):
    return goals.digest({key: value for key, value in goal["scope"].items()
        if key != "configuration_revision"})


def record(op, event, *, now, from_start=False):
    """Optional observation errors never change the controller's result."""
    try:
        _record(op, event, now=now, from_start=from_start)
    except Exception:
        # Settings/transport exceptions can contain credentials. Do not log them.
        return


def _record(op, event, *, now, from_start=False):
    """Append inside the existing admission/claim/outcome transaction and lease."""
    from .website_goal_audit import enabled
    if not enabled(op.connection.organization_id) or event not in {"admission", "claim", "outcome", "superseded", "authority_revoked"}:
        return
    goal = goals.mapping(op.receipt).get("goal")
    if (not goals.valid_goal(goal, goals.mapping(goal).get("scope"))
            or goal.get("goal_id") != str(op.pk)):
        return
    from .website_runtime_observation import backend_process
    process = backend_process(op.connection.organization_id)
    raw = goals.mapping(op.receipt.get(KEY))
    if not raw:
        raw = {"schema_version": 1, "basis": "original_goal_backend_execution_v1",
            "goal_id": str(op.pk), "target": goal["target"], "authority_digest": _authority(goal),
            "from_start": bool(from_start and event == "admission" and op.attempts == 0),
            "overflow": False, "records": []}
    else:
        raw = deepcopy(raw)
    if (raw.get("goal_id") != str(op.pk) or raw.get("authority_digest") != _authority(goal)
            or not isinstance(raw.get("records"), list)):
        return
    if len(raw["records"]) >= MAX_RECORDS:
        raw["overflow"] = True
    else:
        raw["records"].append({"event": event, "sequence": len(raw["records"]) + 1,
            "observed_at": now.isoformat(), "instance_id": process["instance_id"],
            "revision": process["revision"], "scope_digest": goals.digest(goal["scope"]),
            "goal_revision": goal["revision"], "attempt": op.attempts, "state": op.state,
            "configuration_scope": process["configuration_scope"],
            "configuration_digest": process["configuration_digest"], "flags": process["flags"],
            "new_work_goal_enabled": process["new_work_goal_enabled"],
            "backend_eligible": process["backend_eligible"]})
    op.receipt = {**op.receipt, KEY: raw}


def project(op):
    """Refuse absent, malformed, incomplete or capacity-limited execution proof."""
    from .website_rollout_inspection import BACKEND_FLAGS, RULES
    from .website_runtime_observation import BACKEND_CONFIGURATION_SCOPE
    import re
    goal = goals.mapping(goals.mapping(op.receipt).get("goal"))
    unknown = {"schema_version": 1, "basis": "original_goal_backend_execution_v1",
        "goal_id": str(op.pk), "target": goal.get("target"), "coverage": "partial",
        "from_start": False, "overflow": False, "records": [], "gaps": ["backend_execution_unavailable"]}
    raw = goals.mapping(op.receipt.get(KEY))
    try:
        if (set(raw) != {"schema_version", "basis", "goal_id", "target", "authority_digest", "from_start", "overflow", "records"}
                or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                or raw["basis"] != unknown["basis"] or raw["goal_id"] != str(op.pk)
                or raw["target"] != goal["target"] or raw["authority_digest"] != _authority(goal)
                or type(raw["from_start"]) is not bool or type(raw["overflow"]) is not bool
                or not isinstance(raw["records"], list) or not 1 <= len(raw["records"]) <= MAX_RECORDS):
            return unknown
        events = []
        claims = set()
        outcomes = set()
        for sequence, row in enumerate(raw["records"], 1):
            if (not isinstance(row, dict) or set(row) != {"event", "sequence", "observed_at", "instance_id", "revision",
                    "scope_digest", "goal_revision", "attempt", "state", "configuration_scope", "configuration_digest",
                    "flags", "new_work_goal_enabled", "backend_eligible"}
                    or type(row["sequence"]) is not int or row["sequence"] != sequence
                    or row["event"] not in {"admission", "claim", "outcome", "superseded", "authority_revoked"}
                    or goals.stamp(row["observed_at"]) is None
                    or not isinstance(row["instance_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["instance_id"])
                    or row["revision"] is not None and (not isinstance(row["revision"], str) or not goals.SHA.fullmatch(row["revision"]))
                    or not isinstance(row["scope_digest"], str) or not goals.DIGEST.fullmatch(row["scope_digest"])
                    or type(row["goal_revision"]) is not int or row["goal_revision"] < 0
                    or type(row["attempt"]) is not int or not 0 <= row["attempt"] <= op.attempts
                    or row["state"] not in {"pending", "running", "needs_user", "completed", "cancelled", "denied", "deleted", "stopped", "needs_attention"}
                    or row["configuration_scope"] != BACKEND_CONFIGURATION_SCOPE
                    or not isinstance(row["flags"], dict) or set(row["flags"]) != set(BACKEND_FLAGS)
                    or any(type(flag) is not bool for flag in row["flags"].values())
                    or row["configuration_digest"] != goals.digest({"scope": BACKEND_CONFIGURATION_SCOPE, "flags": row["flags"]})
                    or type(row["new_work_goal_enabled"]) is not bool
                    or not isinstance(row["backend_eligible"], dict)
                    or set(row["backend_eligible"]) != {key for key, *_ in RULES}
                    or any(value is not None and type(value) is not bool for value in row["backend_eligible"].values())):
                return unknown
            if row["event"] == "superseded" and (row["state"] != "running"
                    or sequence == len(raw["records"])
                    or raw["records"][sequence].get("event") != "claim"
                    or raw["records"][sequence].get("attempt") != row["attempt"] + 1):
                return unknown
            if row["event"] == "authority_revoked" and (row["state"] != "cancelled"
                    or op.state != "cancelled" or sequence != len(raw["records"])):
                return unknown
            events.append(row["event"])
            if row["event"] == "claim":
                if row["attempt"] in claims or row["attempt"] == 0:
                    return unknown
                claims.add(row["attempt"])
            elif row["event"] in {"outcome", "superseded", "authority_revoked"}:
                if row["attempt"] not in claims or row["attempt"] in outcomes:
                    return unknown
                outcomes.add(row["attempt"])
        gaps = []
        if not raw["from_start"] or events[0] != "admission" or events.count("admission") != 1:
            gaps.append("before_backend_observation_started")
        if raw["overflow"]:
            gaps.append("backend_execution_capacity")
        if claims != set(range(1, op.attempts + 1)) or claims != outcomes:
            gaps.append("backend_execution_attempts_incomplete")
        if op.attempts == 0:
            gaps.append("backend_execution_not_started")
        if any(row["revision"] is None for row in raw["records"]):
            gaps.append("backend_execution_revision_unknown")
        return {**deepcopy(raw), "coverage": "partial" if gaps else "complete", "gaps": gaps}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return unknown
