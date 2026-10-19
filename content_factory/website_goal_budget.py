"""Pure per-goal budget evaluation with replay-safe cumulative usage snapshots."""
from copy import deepcopy
from datetime import timedelta
import math
from django.conf import settings
from . import website_goal as goals

COUNTERS = ("model_calls", "tokens", "provider_seconds", "active_seconds", "model_usd", "builds", "previews")
BOUND_SEMANTICS = "reservation_upper_bounds_v1"
BOUND_COUNTERS = frozenset({"tokens", "provider_seconds", "active_seconds", "model_usd"})


def enabled():
    """Budgets are an independent server-side rollout policy."""
    return getattr(settings, "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED", False)


def numeric(value):
    """Accept finite nonnegative measured values, never booleans or NaN."""
    try:
        return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1e12
    except OverflowError:
        return False


def active_limit(goal):
    """Honor only a bound original operator fixture cap that lowers normal policy."""
    ordinary = getattr(settings, "ARTICLES_GOAL_ACTIVE_SECONDS", 7200)
    if "sandbox_budget_profile" not in goal:
        return ordinary
    profile = goal["sandbox_budget_profile"]
    from .website_sandbox_faults import configured
    expected = {"version", "origin", "goal_id", "authority_digest", "manifest_digest", "case_id", "active_seconds", "digest"}
    if (not configured() or not isinstance(profile, dict) or set(profile) != expected
            or type(profile.get("version")) is not int or profile["version"] != 1
            or profile.get("origin") != "isolated_acceptance_operator_v1"
            or profile.get("goal_id") != goal.get("goal_id")
            or profile.get("authority_digest") != goals.digest({key: value for key, value in goals.mapping(goal.get("scope")).items() if key != "configuration_revision"})
            or not isinstance(profile.get("manifest_digest"), str) or not goals.DIGEST.fullmatch(profile["manifest_digest"])
            or not isinstance(profile.get("case_id"), str) or not 1 <= len(profile["case_id"]) <= 100
            or not numeric(ordinary) or not numeric(profile.get("active_seconds"))
            or not 0 < profile["active_seconds"] <= min(7200, ordinary)
            or profile.get("digest") != goals.digest({key: value for key, value in profile.items() if key != "digest"})):
        return None
    return profile["active_seconds"]


def valid_usage(report, run_id):
    """A report is cumulative for an exact original run, never a transport delta."""
    return (isinstance(report, dict) and type(report.get("version")) is int and report["version"] == 1
        and report.get("run_id") == run_id and isinstance(run_id, str) and 0 < len(run_id) <= 100
        and type(report.get("generation")) is int and report["generation"] >= 0
        and type(report.get("state_version")) is int and report["state_version"] >= 0
        and (report.get("counter_semantics") is None or type(report["counter_semantics"]) is str and report["counter_semantics"] == BOUND_SEMANTICS)
        and all(key in report and (report[key] is None or numeric(report[key])) for key in COUNTERS)
        and all(report[key] is None or type(report[key]) is int for key in ("model_calls", "tokens", "builds", "previews")))


def _usage_peaks(goal, reports):
    """Retain only peaks needed across a settlement or a missing-meter gap."""
    peaks = goal.get("usage_high_water", {})
    if not isinstance(peaks, dict) or not set(peaks).issubset(reports):
        raise ValueError("Stored usage upper bounds are malformed")
    for row in peaks.values():
        if (not isinstance(row, dict) or not set(row).issubset(COUNTERS)
                or any(not numeric(value) or key in {"model_calls", "tokens", "builds", "previews"} and type(value) is not int
                    for key, value in row.items())):
            raise ValueError("Stored usage upper bounds are malformed")
    return peaks


def account_usage(goal, report, *, run_id):
    """Deduplicate snapshots; settled bounds retain their conservative peak."""
    if not valid_usage(report, run_id):
        raise ValueError("Usage report is malformed")
    result = deepcopy(goal)
    reports = result.get("usage_reports", {})
    if not isinstance(reports, dict) or len(reports) > 64:
        raise ValueError("Stored usage journal is malformed")
    for identifier, snapshot in reports.items():
        if not isinstance(snapshot, dict) or not valid_usage({"version": 1, "run_id": identifier, **snapshot}, identifier):
            raise ValueError("Stored usage report is malformed")
    peaks = _usage_peaks(result, reports)
    previous = reports.get(run_id)
    identity = (report["generation"], report["state_version"])
    if previous:
        old_identity = (previous["generation"], previous["state_version"])
        if identity <= old_identity:
            if identity == old_identity and (any(report[key] != previous[key] for key in COUNTERS)
                    or report.get("counter_semantics") != previous.get("counter_semantics")):
                raise ValueError("The same usage report has contradictory counters")
            return result
    elif len(reports) >= 64:
        raise ValueError("The bounded usage journal is full")
    current_peaks = dict(peaks.get(run_id, {}))
    permits_settlement = (report.get("counter_semantics") == BOUND_SEMANTICS
        and previous is not None and previous.get("counter_semantics") == BOUND_SEMANTICS)
    for key in COUNTERS:
        prior_values = [value for value in (current_peaks.get(key), (previous or {}).get(key)) if value is not None]
        prior_peak = max(prior_values) if prior_values else None
        value = report[key]
        if prior_peak is not None and value is not None and value < prior_peak and not (permits_settlement and key in BOUND_COUNTERS):
            raise ValueError("Cumulative usage counters moved backwards")
        if prior_peak is not None and (value is None or value < prior_peak):
            current_peaks[key] = prior_peak
        else:
            current_peaks.pop(key, None)
    if current_peaks:
        peaks[run_id] = current_peaks
    else:
        peaks.pop(run_id, None)
    reports[run_id] = {key: report[key] for key in (*COUNTERS, "generation", "state_version")}
    if report.get("counter_semantics") is not None:
        reports[run_id]["counter_semantics"] = report["counter_semantics"]
    result["usage_reports"] = reports
    if peaks:
        result["usage_high_water"] = peaks
    else:
        result.pop("usage_high_water", None)
    totals = {key: sum(max(item[key], peaks.get(identifier, {}).get(key, item[key])) for identifier, item in reports.items())
        if all(item[key] is not None for item in reports.values()) else None for key in COUNTERS}
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


def owner_wait_seconds(goal, *, now):
    """Validate and measure required owner waits without forgiving compute/provider time."""
    started = goals.stamp(goal.get("started_at"))
    closed = goal.get("owner_wait_completed_seconds", 0)
    opened = goals.stamp(goal.get("owner_wait_started_at"))
    if (not started or not numeric(closed) or goal.get("owner_wait_started_at") is not None and not opened
        or opened and not started <= opened <= now):
        raise ValueError("Invalid owner wait ledger")
    paused = closed + ((now - opened).total_seconds() if opened else 0)
    if paused > max(0, (now - started).total_seconds()):
        raise ValueError("Owner wait exceeds the goal lifetime")
    return paused


def update_owner_wait(goal, *, waiting, now):
    """Open/close one durable owner interval; repeated state observations are inert."""
    owner_wait_seconds(goal, now=now)
    opened = goals.stamp(goal.get("owner_wait_started_at"))
    if waiting == bool(opened):return goal
    result = deepcopy(goal)
    if waiting:
        result["owner_wait_started_at"] = now.isoformat()
    else:
        result["owner_wait_completed_seconds"] = result.get("owner_wait_completed_seconds", 0) + (now - opened).total_seconds()
        result.pop("owner_wait_started_at", None)
    result["revision"] += 1
    return result


def exhaustion(goal, *, now, action=None):
    """Stop before new work at exact limits; unknown measurements stay unknown."""
    if goal.get("usage_invalid"):
        return "invalid_usage_report"
    if action in {"setup", "scan", "ci-attestation", "first-article-generation"} and goal.get("missing_usage_run_ids"):
        return "usage_unknown"
    started = goals.stamp(goal.get("started_at"))
    limits = {"wall": getattr(settings, "ARTICLES_GOAL_WALL_SECONDS", 172800),
        "active": active_limit(goal),
        "usd": getattr(settings, "ARTICLES_GOAL_MODEL_USD", 25),
        "transient": getattr(settings, "ARTICLES_GOAL_TRANSIENT_ATTEMPTS", 20),
        "repairable": getattr(settings, "ARTICLES_GOAL_REPAIRABLE_ATTEMPTS", 6)}
    if not all(numeric(value) and value > 0 for value in limits.values()) or not started or started > now + timedelta(minutes=5):
        return "invalid_budget_contract"
    try:
        paused = owner_wait_seconds(goal, now=now)
    except ValueError:
        return "invalid_budget_contract"
    if (now - started).total_seconds() - paused >= limits["wall"]:
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
