"""Closed, credential-free failure receipts for independent cleanup steps."""


def record_failure(op, error, *, step):
    """Retain safe provider status without copying exceptions or response bodies."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is not int:
        status = getattr(error, "status_code", None)
    if type(status) is not int:
        status = None
    code = "GITHUB_ACCESS_REQUIRED" if step in {"repository_refs", "cleanup_pr"} and status in {401, 403, 404} else "worker_unavailable"
    failure = {"version": 1, "step": step, "code": code, "retryable": status not in {401, 403, 404}}
    if status is not None:
        failure["http_status"] = status
    op.receipt.setdefault("cleanup_step_failures", {})[step] = failure
    op.receipt["last_error"] = code
    if not failure["retryable"]:
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="needs_attention", message="Previous website cleanup needs its service access checked.")
    return failure


def superseded_repository_work(op):
    """Retain obsolete repository work while original service cleanup can finish."""
    stale = op.generation != op.connection.generation or op.connection.state != "disconnected"
    if stale:
        op.receipt["repository_cleanup"] = {"status": "superseded", "reason": "website_connection_changed",
            "retained": True, "original_generation": op.generation}
    return stale


def blocked_step(op, step):
    """A permanent provider failure waits for an explicit prerequisite repair."""
    return op.receipt.get("cleanup_step_failures", {}).get(step, {}).get("retryable") is False


def retry_failure_steps(receipt):
    """An explicit current-owner retry rechecks prerequisites, keeping history and deadline."""
    history = receipt.get("cleanup_failure_history")
    history = list(history) if isinstance(history, list) else []
    failures = receipt.get("cleanup_step_failures")
    if isinstance(failures, dict) and failures:
        history.append(failures)
    return {**receipt, "cleanup_step_failures": {}, "cleanup_failure_history": history[-10:], "last_error": ""}


def settle_failure_state(op):
    """Continue independent settlement while retaining a blocked repository step."""
    if op.receipt.get("cleanup_stop_reason") in {"preview_cleanup_abandoned", "preview_cleanup_budget_invalid"}:
        return
    ignored = {"repository_refs", "cleanup_pr"} if op.receipt.get("repository_cleanup", {}).get("status") == "superseded" else set()
    failures = op.receipt.get("cleanup_step_failures", {})
    if not any(item.get("retryable") is False for step, item in failures.items() if step not in ignored):
        return
    outstanding = bool(op.payload.get("cancel_run_ids") or op.payload.get("stop_preview_run_ids")
        or op.payload.get("disable_auto_merge_prs") or op.receipt.get("refund_pending_run_ids")
        or op.receipt.get("tokens", {}).get("pending"))
    op.state = "pending" if outstanding else "needs_attention"
    op.receipt["status"] = "working" if outstanding else "needs_attention"
