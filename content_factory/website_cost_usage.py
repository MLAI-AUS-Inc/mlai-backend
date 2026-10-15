"""Pure, allowlisted accounting snapshots; these never confer website authority."""
from copy import deepcopy
from . import website_goal as goals, website_goal_budget as budget

BASES = {"model_calls": "admitted_calls", "tokens": "provider_reported", "provider_seconds": "settled_elapsed",
    "active_seconds": "admitted_activity_elapsed", "model_usd": "configured_token_rate_upper_bound",
    "builds": "workflow_step_attempts", "previews": "workflow_step_attempts"}
INTEGER = {"model_calls", "tokens", "builds", "previews"}


def _nullable(value, *, integer=False):
    return value is None or budget.numeric(value) and (not integer or type(value) is int)


def accounting(value, usage):
    """Validate versioned measurement bases without retaining arbitrary worker text."""
    if not isinstance(value, dict) or set(value) != {"version", *BASES} or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unknown accounting projection")
    for key, basis in BASES.items():
        metric = value[key]
        if not isinstance(metric, dict) or metric.get("basis") != basis:
            raise ValueError("Unknown accounting basis")
        if key in {"tokens", "provider_seconds"}:
            if set(metric) != {"basis", "total", "known_subtotal", "measured_calls", "total_calls"}:
                raise ValueError("Invalid accounting coverage")
            total, known, measured, calls = (metric[name] for name in ("total", "known_subtotal", "measured_calls", "total_calls"))
            if not all(_nullable(item, integer=key in INTEGER) for item in (total, known)) or not all(_nullable(item, integer=True) for item in (measured, calls)):
                raise ValueError("Invalid measured usage")
            if calls != usage["model_calls"] or (calls is None and any(item is not None for item in (total, known, measured))):
                raise ValueError("Accounting call identity changed")
            if calls is not None and (measured is None or measured > calls or known is None
                    or measured == 0 and known != 0
                    or (total != known if measured == calls else total is not None)
                    or usage[key] is not None and known > usage[key]):
                raise ValueError("Accounting coverage contradicts its total")
        elif key in {"builds", "previews"}:
            if (set(metric) != {"basis", "attempts", "invocations"} or metric["invocations"] is not None
                    or not _nullable(metric["attempts"], integer=True) or metric["attempts"] != usage[key]):
                raise ValueError("Workflow attempts are not invocation measurements")
        else:
            field = "upper_bound" if key == "model_usd" else "total"
            if set(metric) != {"basis", field} or not _nullable(metric[field], integer=key in INTEGER):
                raise ValueError("Invalid accounting value")
            if key in {"model_calls", "model_usd"} and metric[field] != usage[key]:
                raise ValueError("Accounting contradicts its budget snapshot")
    return deepcopy(value)


def merge(previous, report, *, run_id, now):
    """Keep one original-run observation, with replay and settlement validation."""
    if not budget.valid_usage(report, run_id):
        reason = "usage_unavailable" if report is None else "invalid_usage"
        prior = goals.mapping(previous)
        if prior:
            return {**deepcopy(prior), "incomplete_reason": reason}
        return {"version": 1, "usage": None, "accounting": None, "budget_peaks": {},
            "incomplete_reason": reason, "observed_at": now.isoformat()}
    selected = {key: report[key] for key in ("version", "run_id", "generation", "state_version", *budget.COUNTERS)}
    if report.get("counter_semantics") is not None:
        selected["counter_semantics"] = report["counter_semantics"]
    reason, measured = "unavailable", None
    if report.get("accounting") is not None:
        try:
            measured, reason = accounting(report["accounting"], selected), None
        except ValueError:
            reason = "invalid_accounting"
    prior = goals.mapping(previous)
    old = goals.mapping(prior.get("usage"))
    identity = (selected["generation"], selected["state_version"])
    if old and not budget.valid_usage(old, run_id):
        raise ValueError("Stored accounting usage is invalid")
    old_identity = (old["generation"], old["state_version"]) if old else None
    if old_identity and identity < old_identity:
        return deepcopy(previous)
    candidate = {"version": 1, "usage": selected, "accounting": measured, "incomplete_reason": reason}
    if old_identity == identity:
        if all(candidate[key] == prior.get(key) for key in ("usage", "accounting")):
            return deepcopy(previous)
        return {**deepcopy(prior), "incomplete_reason": "conflicting_snapshot"}
    journal = {"revision": 0, "usage_reports": {run_id: {key: item for key, item in old.items() if key not in {"version", "run_id"}}} if old else {},
        "usage_high_water": {run_id: prior["budget_peaks"]} if prior.get("budget_peaks") else {}}
    try:
        charged = budget.account_usage(journal, selected, run_id=run_id)
    except ValueError:
        if old:
            return {**deepcopy(prior), "incomplete_reason": "counter_reset"}
        raise
    return {**candidate, "budget_peaks": charged.get("usage_high_water", {}).get(run_id, {}), "observed_at": now.isoformat()}


def attribution(operation, run):
    """Use the run's immutable original goal, never its latest reusing caller."""
    goal = goals.mapping(goals.mapping(operation.receipt).get("goal"))
    scope = goals.mapping(goal.get("scope"))
    saved = goals.mapping(run.run_request)
    contract = goals.mapping(saved.get("goal_budget"))
    if (not goals.valid_goal(goal, scope) or goal["goal_id"] != str(operation.pk)
            or any(type(scope.get(key)) is not int or scope[key] < minimum for key, minimum in
                (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))
            or any(not isinstance(scope.get(key), str) for key in
                ("company_id", "organization_id", "connection_id", "repository_id", "branch", "app_root"))
            or type(contract.get("version")) is not int or contract["version"] != 1
            or contract.get("goal_id") != goal["goal_id"] or contract.get("scope_digest") != goals.digest(scope)
            or str(run.organization_id) != scope["organization_id"]
            or str(operation.connection_id) != scope["connection_id"] or operation.generation != scope["connection_generation"]
            or str(operation.connection.organization_id) != scope["organization_id"]
            or str(goals.mapping(operation.payload).get("company_id")) != scope["company_id"]
            or saved.get("website_connection_id") != scope["connection_id"]
            or type(saved.get("connection_generation")) is not int or saved["connection_generation"] != scope["connection_generation"]
            or str(saved.get("repository_id")) != scope["repository_id"]
            or saved.get("branch") != scope["branch"] or saved.get("app_root") != scope["app_root"]
            or type(saved.get("deletion_epoch")) is not int or saved["deletion_epoch"] != scope["deletion_epoch"]):
        raise ValueError("Original accounting attribution does not match")
    return {"version": 1, "run_id": run.run_id, "goal_id": goal["goal_id"], "scope": deepcopy(scope)}


def aggregate(records, *, now, truncated=False, incomplete=False):
    """Describe retained observations and coverage; an empty ledger is unmeasured."""
    normalized = []
    for raw in records:
        record = deepcopy(goals.mapping(raw))
        usage = goals.mapping(record.get("usage"))
        try:
            if record.get("incomplete_reason") is not None and not isinstance(record["incomplete_reason"], str):
                raise ValueError("Invalid stored accounting state")
            if not budget.valid_usage(usage, usage.get("run_id")):
                raise ValueError("Invalid stored usage")
            if record.get("accounting") is not None:
                record["accounting"] = accounting(record["accounting"], usage)
            elif record.get("incomplete_reason") is None:
                record["incomplete_reason"] = "unavailable"
        except ValueError:
            record["incomplete_reason"] = "invalid_snapshot"
        normalized.append(record)
    records = normalized
    metrics = {}
    for key, basis in BASES.items():
        values, subtotals = [], []
        for record in records:
            usage = goals.mapping(record.get("usage"))
            quality = goals.mapping(record.get("accounting"))
            metric = goals.mapping(quality.get(key))
            if record.get("incomplete_reason") not in {None, "unavailable"}:
                value = subtotal = None
            elif key == "model_calls":
                value = subtotal = usage.get(key)
            elif key == "model_usd":
                value = subtotal = usage.get(key)
            elif key in {"builds", "previews"}:
                value = subtotal = metric.get("invocations")
            else:
                value = metric.get("total")
                subtotal = metric.get("known_subtotal", value)
            values.append(value if _nullable(value, integer=key in INTEGER) else None)
            subtotals.append(subtotal if _nullable(subtotal, integer=key in INTEGER) else None)
        known = [value for value in subtotals if value is not None]
        metrics[key] = {"basis": "invocations" if key in {"builds", "previews"} else basis,
            "total": sum(values) if values and not truncated and not incomplete and all(value is not None for value in values) else None,
            "knownSubtotal": sum(known) if known else None, "measuredRuns": sum(value is not None for value in values),
            "recordedRuns": len(records)}
    return {"version": 1, "history": "retained_recorded_usage", "coverage": "observed_goal_meter_runs", "observedAt": now.isoformat(),
        "recordedRuns": len(records), "incompleteRuns": sum(record.get("incomplete_reason") is not None for record in records),
        "truncated": truncated, "coverageIncomplete": truncated or incomplete, "metrics": metrics}
