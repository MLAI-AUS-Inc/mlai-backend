"""Native goal accounting uses the common bounded policy without Git proofs."""

from copy import deepcopy
from uuid import UUID

from . import website_goal_budget as policy
from . import cms_goal_contract as goals
from .website_goal import CLASSES
from .website_contract import WebsiteAuthorityError

FIELDS = {"version", "started_at", "revision", "attempts_by_class", "budget_used", "usage_reports", "owner_wait_completed_seconds"}
OPTIONAL = {"owner_wait_started_at", "usage_high_water", "missing_usage_run_ids", "usage_invalid", "fingerprint_counts", "failure_policy_occurrences", "setup_usage", "access_usage"}


def initial(goal):
    """Start one independent native budget at the original accepted goal time."""
    return initial_at(goal["started_at"])


def initial_at(started_at):
    """Start real preparatory accounting before a native goal or policy exists."""
    started_at = goals._time(started_at).isoformat()
    return {"version": 1, "started_at": started_at, "revision": 0, "attempts_by_class": {name: 0 for name in CLASSES},
        "budget_used": {}, "usage_reports": {}, "owner_wait_completed_seconds": 0}


def validate(value, goal, *, now):
    """A missing or malformed private budget never grants new model/provider work."""
    return validate_at(value, started_at=goal["started_at"], operation_id=goal["goal_id"], now=now)


def _setup_usage(value, operation_id):
    usage = value.get("setup_usage")
    if "setup_usage" in value and usage is None:
        raise ValueError("Invalid setup usage")
    if usage is not None and (not isinstance(usage, dict) or set(usage) != {"version", "operation_id", "counter_semantics", "active_seconds", "provider_seconds"} or
            type(usage["version"]) is not int or usage["version"] != 1 or usage["operation_id"] != operation_id or
            str(UUID(operation_id)) != operation_id or usage["counter_semantics"] != policy.BOUND_SEMANTICS or
            any(not policy.numeric(usage[key]) for key in ("active_seconds", "provider_seconds"))):
        raise ValueError("Invalid setup usage")
    return usage


def _backend_usage(value, operation_id):
    setup = _setup_usage(value, operation_id)
    totals = {key: setup[key] if setup is not None else 0 for key in ("active_seconds", "provider_seconds")}
    access = value.get("access_usage", {})
    if not isinstance(access, dict) or len(access) > 64:
        raise ValueError("Invalid access usage")
    for identifier, usage in access.items():
        if (not isinstance(identifier, str) or str(UUID(identifier)) != identifier or not isinstance(usage, dict) or
                set(usage) != {"version", "operation_id", "counter_semantics", "active_seconds", "provider_seconds"} or
                type(usage["version"]) is not int or usage["version"] != 1 or usage["operation_id"] != identifier or
                usage["counter_semantics"] != policy.BOUND_SEMANTICS or any(not policy.numeric(usage[key]) for key in totals)):
            raise ValueError("Invalid access usage")
        for key in totals:
            totals[key] += usage[key]
    return totals


def validate_at(value, *, started_at, operation_id, now):
    """Validate the original accepted lifetime without inventing a native goal."""
    try:
        if (not isinstance(value, dict) or not FIELDS <= set(value) or set(value) - FIELDS - OPTIONAL or
                type(value["version"]) is not int or value["version"] != 1 or value["started_at"] != started_at or
                type(value["revision"]) is not int or value["revision"] < 0 or
                not isinstance(value["attempts_by_class"], dict) or set(value["attempts_by_class"]) != set(CLASSES) or
                any(type(count) is not int or not 0 <= count <= 10**6 for count in value["attempts_by_class"].values()) or
                not isinstance(value["budget_used"], dict) or
                any(item is not None and not policy.numeric(item) for item in value["budget_used"].values()) or
                not isinstance(value["usage_reports"], dict) or len(value["usage_reports"]) > 64 or
                any(not policy.valid_usage({"version": 1, "run_id": identifier, **row}, identifier)
                    for identifier, row in value["usage_reports"].items()) or
                "missing_usage_run_ids" in value and (not isinstance(value["missing_usage_run_ids"], list) or
                    len(value["missing_usage_run_ids"]) > 64 or any(not isinstance(item, str) or not 0 < len(item) <= 100
                        for item in value["missing_usage_run_ids"])) or
                "usage_invalid" in value and type(value["usage_invalid"]) is not bool):
            raise ValueError("Invalid native budget")
        goals._time(value["started_at"])
        policy.owner_wait_seconds(value, now=now)
        policy._usage_peaks(value, value["usage_reports"])
        usage = _backend_usage(value, operation_id)
        if any(value["budget_used"].get(key) is not None and value["budget_used"][key] < usage[key]
                for key in ("active_seconds", "provider_seconds")):
            raise ValueError("Backend usage was erased")
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WebsiteAuthorityError("website_operation_changed", "The original native articles budget could not be verified.") from None


def account_usage(value, report, *, run_id):
    """Keep real backend setup bounds separate from original CF usage snapshots."""
    selected = deepcopy(value)
    metadata = selected.get("setup_usage")
    backend = _backend_usage(selected, metadata.get("operation_id") if isinstance(metadata, dict) else None)
    carried = "setup_usage" in selected or "access_usage" in selected
    if carried:
        # The shared meter sums only real CF reports. Remove and then add the
        # separate backend source once, including on repeated/stale callbacks.
        for key in ("active_seconds", "provider_seconds"):
            if selected["budget_used"].get(key) is not None:
                if selected["budget_used"][key] < backend[key]:
                    raise ValueError("Backend usage was erased")
                selected["budget_used"][key] -= backend[key]
    selected = policy.account_usage(selected, report, run_id=run_id)
    if carried:
        for key in ("active_seconds", "provider_seconds"):
            if selected["budget_used"].get(key) is not None:
                selected["budget_used"][key] += backend[key]
    return selected


def reserve_access_usage(value, *, started_at, operation_id, access_operation_id, active_seconds=0, provider_seconds=0, now):
    """Record a real private credential job's admission bounds beside CF usage.

    Its owning worker must validate the actual job/parent and fence each lease
    or GET transaction. This meter grants no permission and fabricates no run.
    Lost work remains counted; later original CF reports cannot erase it.
    """
    result = validate_at(value, started_at=started_at, operation_id=operation_id, now=now)
    try:
        if (not isinstance(access_operation_id, str) or str(UUID(access_operation_id)) != access_operation_id or
                any(not policy.numeric(item) for item in (active_seconds, provider_seconds))):
            raise ValueError("Invalid access allowance")
        sources = deepcopy(result.get("access_usage", {}))
        if access_operation_id not in sources and len(sources) >= 64:
            raise ValueError("Access allowance sources exhausted")
        previous = sources.get(access_operation_id) or {"version": 1, "operation_id": access_operation_id,
            "counter_semantics": policy.BOUND_SEMANTICS, "active_seconds": 0, "provider_seconds": 0}
        sources[access_operation_id] = {**previous, "active_seconds": previous["active_seconds"] + active_seconds,
            "provider_seconds": previous["provider_seconds"] + provider_seconds}
        result["access_usage"] = sources
        for key, amount in (("active_seconds", active_seconds), ("provider_seconds", provider_seconds)):
            if key not in result["budget_used"] and not result["usage_reports"]:
                result["budget_used"][key] = 0
            if result["budget_used"].get(key) is not None:
                result["budget_used"][key] += amount
        result["revision"] += 1
        return validate_at(result, started_at=started_at, operation_id=operation_id, now=now)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WebsiteAuthorityError("website_operation_changed", "The original native access accounting changed.") from None


def reserve_setup_usage(value, *, started_at, operation_id, active_seconds=0, provider_seconds=0, now):
    """Record actual backend lease/read allowances, separately from any CF run.

    These are conservative admission bounds, not claims of measured CPU or
    network time. A crashed allowance remains spent; a callback cannot reset it.
    The private setup worker owns the transaction/CAS for each reservation.
    """
    result = validate_at(value, started_at=started_at, operation_id=operation_id, now=now)
    if result["usage_reports"] or any(not policy.numeric(item) for item in (active_seconds, provider_seconds)):
        raise WebsiteAuthorityError("website_operation_changed", "The original native setup accounting changed.")
    previous = result.get("setup_usage") or {"version": 1, "operation_id": operation_id, "counter_semantics": policy.BOUND_SEMANTICS,
        "active_seconds": 0, "provider_seconds": 0}
    usage = {**previous, "active_seconds": previous["active_seconds"] + active_seconds,
        "provider_seconds": previous["provider_seconds"] + provider_seconds}
    result["setup_usage"] = usage
    for key, amount in (("active_seconds", active_seconds), ("provider_seconds", provider_seconds)):
        if key not in result["budget_used"]:
            result["budget_used"][key] = 0
        if result["budget_used"][key] is not None:
            result["budget_used"][key] += amount
    result["revision"] += 1
    return validate_at(result, started_at=started_at, operation_id=operation_id, now=now)


def exhaustion(operation, *, now, action=None):
    """Re-evaluate elapsed time and recorded usage at every work admission."""
    goal = goals.validate_goal(operation.receipt["goal"], operation.receipt["goal"]["scope"])
    value = validate(operation.receipt.get("native_budget"), goal, now=now)
    return policy.exhaustion(value, now=now, action=action)
