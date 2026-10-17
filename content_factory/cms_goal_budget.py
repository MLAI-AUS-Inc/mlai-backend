"""Native goal accounting uses the common bounded policy without Git proofs."""

from copy import deepcopy

from . import website_goal_budget as policy
from . import cms_goal_contract as goals
from .website_goal import CLASSES
from .website_contract import WebsiteAuthorityError

FIELDS = {"version", "started_at", "revision", "attempts_by_class", "budget_used", "usage_reports", "owner_wait_completed_seconds"}
OPTIONAL = {"owner_wait_started_at", "usage_high_water", "missing_usage_run_ids", "usage_invalid", "fingerprint_counts", "failure_policy_occurrences"}


def initial(goal):
    """Start one independent native budget at the original accepted goal time."""
    return {"version": 1, "started_at": goal["started_at"], "revision": 0, "attempts_by_class": {name: 0 for name in CLASSES},
        "budget_used": {}, "usage_reports": {}, "owner_wait_completed_seconds": 0}


def validate(value, goal, *, now):
    """A missing or malformed private budget never grants new model/provider work."""
    try:
        if (not isinstance(value, dict) or not FIELDS <= set(value) or set(value) - FIELDS - OPTIONAL or
                type(value["version"]) is not int or value["version"] != 1 or value["started_at"] != goal["started_at"] or
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
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WebsiteAuthorityError("website_operation_changed", "The original native articles budget could not be verified.") from None


def exhaustion(operation, *, now, action=None):
    """Re-evaluate elapsed time and recorded usage at every work admission."""
    goal = goals.validate_goal(operation.receipt["goal"], operation.receipt["goal"]["scope"])
    value = validate(operation.receipt.get("native_budget"), goal, now=now)
    return policy.exhaustion(value, now=now, action=action)
