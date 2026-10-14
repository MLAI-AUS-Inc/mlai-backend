"""Pure per-goal budget evaluation with replay-safe cumulative usage snapshots."""
from copy import deepcopy
from datetime import timedelta
import math
from django.conf import settings
from . import website_goal as goals

COUNTERS = ("model_calls", "tokens", "provider_seconds", "active_seconds", "model_usd", "builds", "previews")


def enabled():
    """Budgets are an independent server-side rollout policy."""
    return getattr(settings, "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED", False)


def numeric(value):
    """Accept finite nonnegative measured values, never booleans or NaN."""
    try:
        return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1e12
    except OverflowError:
        return False


def valid_usage(report, run_id):
    """A report is cumulative for an exact original run, never a transport delta."""
    return (isinstance(report, dict) and type(report.get("version")) is int and report["version"] == 1
        and report.get("run_id") == run_id and isinstance(run_id, str) and 0 < len(run_id) <= 100
        and type(report.get("generation")) is int and report["generation"] >= 1
        and type(report.get("state_version")) is int and report["state_version"] >= 0
        and all(key in report and (report[key] is None or numeric(report[key])) for key in COUNTERS)
        and all(report[key] is None or type(report[key]) is int for key in ("model_calls", "tokens", "builds", "previews")))


def account_usage(goal, report, *, run_id):
    """Deduplicate snapshots and refuse backwards counters or stale reports."""
    if not valid_usage(report, run_id):
        raise ValueError("Usage report is malformed")
    result = deepcopy(goal)
    reports = goals.mapping(result.get("usage_reports"))
    previous = reports.get(run_id)
    identity = (report["generation"], report["state_version"])
    if previous:
        old_identity = (previous["generation"], previous["state_version"])
        if identity <= old_identity:
            if identity == old_identity and any(report[key] != previous[key] for key in COUNTERS):
                raise ValueError("The same usage report has contradictory counters")
            return result
        if any(previous[key] is not None and report[key] is not None and report[key] < previous[key] for key in COUNTERS):
            raise ValueError("Cumulative usage counters moved backwards")
    elif len(reports) >= 64:
        raise ValueError("The bounded usage journal is full")
    reports[run_id] = {key: report[key] for key in (*COUNTERS, "generation", "state_version")}
    result["usage_reports"] = reports
    totals = {key: sum(item[key] for item in reports.values()) if all(item[key] is not None for item in reports.values()) else None for key in COUNTERS}
    result["budget_used"] = {**goals.mapping(result.get("budget_used")), **totals, "model_cost": totals["model_usd"]}
    result["revision"] += 1
    return result


def record_policy_occurrence(goal, *, occurrence, fingerprint, classification):
    """Preserve B1 causality while deriving the effective loop policy separately."""
    if not isinstance(occurrence, str) or not 0 < len(occurrence) <= 160 or classification not in goals.CLASSES:
        raise ValueError("Failure policy occurrence is malformed")
    if classification == "waiting":
        return goal, classification, False
    result = deepcopy(goal)
    decisions = goals.mapping(result.get("failure_policy_occurrences"))
    if occurrence in decisions:
        return result, decisions[occurrence], False
    if len(decisions) >= goals.MAX_OCCURRENCES:
        raise ValueError("The bounded failure policy journal is full")
    counts = goals.mapping(result.get("fingerprint_counts"))
    count = 0
    if isinstance(fingerprint, str) and goals.FINGERPRINT.fullmatch(fingerprint) and classification != "waiting":
        count = counts.get(fingerprint, 0) + 1
        counts[fingerprint] = count
    effective = classification
    if classification == "transient" and count >= 2:
        effective = "repairable" if count == 2 else "strategy" if count == 3 else "unsupported"
    elif classification == "repairable" and count >= 2:
        effective = "strategy" if count == 2 else "unsupported"
    elif classification == "strategy" and count >= 2:
        effective = "unsupported"
    decisions[occurrence] = effective
    result["failure_policy_occurrences"], result["fingerprint_counts"] = decisions, counts
    result["revision"] += 1
    return result, effective, True


def exhaustion(goal, *, now, action=None):
    """Stop before new work at exact limits; unknown measurements stay unknown."""
    if goal.get("usage_invalid"):
        return "invalid_usage_report"
    if action in {"setup", "scan", "ci-attestation"} and goal.get("missing_usage_run_ids"):
        return "usage_unknown"
    started = goals.stamp(goal.get("started_at"))
    limits = {"wall": getattr(settings, "ARTICLES_GOAL_WALL_SECONDS", 172800),
        "active": getattr(settings, "ARTICLES_GOAL_ACTIVE_SECONDS", 7200),
        "usd": getattr(settings, "ARTICLES_GOAL_MODEL_USD", 25),
        "transient": getattr(settings, "ARTICLES_GOAL_TRANSIENT_ATTEMPTS", 20),
        "repairable": getattr(settings, "ARTICLES_GOAL_REPAIRABLE_ATTEMPTS", 6)}
    if not all(numeric(value) and value > 0 for value in limits.values()) or not started or started > now + timedelta(minutes=5):
        return "invalid_budget_contract"
    if (now - started).total_seconds() >= limits["wall"]:
        return "wall_time"
    used = goals.mapping(goal.get("budget_used"))
    for key, limit, reason in (("active_seconds", limits["active"], "active_compute"), ("model_cost", limits["usd"], "model_cost")):
        value = used.get(key)
        if value is not None and not numeric(value):
            return "invalid_budget_contract"
        if value is not None and value >= limit:
            return reason
    counts = goals.mapping(goal.get("attempts_by_class"))
    for name in ("transient", "repairable"):
        if type(counts.get(name)) is not int or counts[name] < 0:
            return "invalid_budget_contract"
        if counts[name] >= limits[name]:
            return name + "_attempts"
    pin = goals.mapping(goal.get("strategy_pin"))
    if action == "setup" and pin.get("rung") in {"model_adapter", "agent"} and goal.get("usage_reports") and (used.get("model_cost") is None or used.get("active_seconds") is None):
        return "usage_unknown"
    return None
