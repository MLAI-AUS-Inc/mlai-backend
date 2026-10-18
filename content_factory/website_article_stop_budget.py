"""Bounded original-effect cleanup after a goal's new-work budget is spent."""
from copy import deepcopy
from datetime import timedelta

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError

KEY = "article_budget_stop_cleanup"
MAX_PASSES = 32


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article cleanup budget changed.")


def _saved(parent, request):
    from . import website_article_source_stop as stops
    from .website_failure_contract import validate_failure, FailureContractError
    saved = parent.receipt.get(KEY)
    if saved is None: return None
    try:
        fields = {"schema_version", "goal_operation_id", "request_digest", "source_fence_id", "reason", "passes", "state",
            "broker_cycle_pending", "last_failure", "quiet", "inventory_complete", "effect_authority"}
        if (not isinstance(saved, dict) or set(saved) != fields or type(saved["schema_version"]) is not int
                or saved["schema_version"] != 1 or saved["goal_operation_id"] != str(parent.pk)
                or saved["request_digest"] != request["digest"] or saved["source_fence_id"] != request["source_fence_id"]
                or not isinstance(saved["reason"], str) or not 0 < len(saved["reason"]) <= 64
                or saved["reason"] != parent.receipt.get("budget_stop_reason")
                or type(saved["passes"]) is not int or not 1 <= saved["passes"] <= MAX_PASSES
                or not isinstance(saved["state"], str) or saved["state"] not in {"pending", "observed", "exhausted"}
                or saved["state"] == "pending" and saved["passes"] >= MAX_PASSES
                or type(saved["broker_cycle_pending"]) is not bool
                or any(saved[key] is not False for key in ("quiet", "inventory_complete", "effect_authority"))): _deny()
        if saved["last_failure"] is not None: validate_failure(saved["last_failure"])
        row = stops._operation(parent.receipt[stops.KEY])
        receipt = stops.validate_receipt(parent.receipt.get("article_source_stop"), request)
        if row is None or row.receipt.get("local_stop") != receipt: _deny()
        return deepcopy(saved)
    except (KeyError, ValueError, TypeError, AttributeError, FailureContractError):
        _deny()


def advance(parent, config, request, *, reason, now):
    """Retain only bounded stop work; the prepare loop cannot execute new work."""
    from . import website_article_source_stop as stops
    from .website_first_article import _save_fields
    from .website_goal_runner import stop_goal
    parent._article_budget_cleanup_pending = False
    if not isinstance(reason, str) or not 0 < len(reason) <= 64: _deny()
    previous = _saved(parent, request)
    if previous is not None and previous["state"] in {"observed", "exhausted"}:
        stop_goal(parent, parent.receipt["goal"], reason=previous["reason"], now=now)
        return True
    reason = previous["reason"] if previous is not None else reason
    passes = (previous["passes"] if previous else 0) + 1
    if passes > MAX_PASSES: _deny()
    value = {"schema_version": 1, "goal_operation_id": str(parent.pk), "request_digest": request["digest"],
        "source_fence_id": request["source_fence_id"], "reason": reason, "passes": passes,
        "state": "exhausted" if passes >= MAX_PASSES else "pending",
        "broker_cycle_pending": previous["broker_cycle_pending"] if previous else False, "last_failure": None,
        "quiet": False, "inventory_complete": False, "effect_authority": False}
    # A crash consumes this pass before any remote request. The original five
    # minute prepare claim and every purpose's own lease still apply.
    _save_fields(parent, {KEY: deepcopy(value), "budget_stop_reason": reason, "status": "stopped"})
    more, cycle_pending = True, value["broker_cycle_pending"]
    try:
        retirement = stops._retire_original_credentials(parent, config, request, now=now)
        if retirement is None:
            cycle_pending = True
            broker_pending = True
        else:
            cycle_pending = cycle_pending or any(row["pending"] > 0 for row in retirement["operations"])
            broker_pending = retirement["next_inventory_id"] is not None or cycle_pending
            if retirement["next_inventory_id"] is None: cycle_pending = False
        publication_pending = stops._remove_owned_publication_effects(parent, config, now=now)
        more = broker_pending or publication_pending
    except WebsiteAuthorityError as error:
        if not error.retryable: raise
        from .website_failure_contract import classify_failure, validate_failure
        value["last_failure"] = validate_failure(error.failure) if error.failure else classify_failure(
            "website_prepare_dependency_transient", "article_source_cleanup")
    if stops._fresh(parent, config) != request["source_fence"]: _deny()
    value.update(broker_cycle_pending=cycle_pending,
        state="exhausted" if more and passes >= MAX_PASSES else "pending" if more else "observed")
    _save_fields(parent, {KEY: value})
    stop_goal(parent, parent.receipt["goal"], reason=reason, now=now)
    if more and passes < MAX_PASSES:
        parent.state, parent.next_attempt_at = "pending", now + timedelta(minutes=1)
        parent._article_budget_cleanup_pending = True
        # The scheduler gets another cleanup pass; owner-facing status and the
        # original budget remain stopped. No ordinary event can revive it.
    return True
