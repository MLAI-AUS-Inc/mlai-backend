"""Bounded runner orchestration, independent of Django and provider clients."""

import logging
from time import monotonic

logger = logging.getLogger(__name__)
FAILURE_STATUSES = frozenset({"failed", "error", "halted", "aborted_ownership_unconfirmed"})


def runner_failed(result):
    if not isinstance(result, dict):
        return True
    status = result.get("status")
    if not isinstance(status, str) or status in FAILURE_STATUSES:
        return True
    failed_count = result.get("failed", 0)
    if isinstance(failed_count, (int, float)) and failed_count > 0:
        return True
    queued_run = result.get("queued_run")
    return queued_run is not None and runner_failed(queued_run)


def run_runners(runners):
    """Run every selector even if another fails; retain the wire result shape."""
    results, failures = {}, []
    for name, runner in runners:
        started = monotonic()
        try:
            result = runner()
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                raise ValueError("Scheduled runner must return a status dictionary")
            results[name] = result
        except Exception as exc:
            logger.exception("Scheduled %s runner failed.", name)
            results[name] = {"status": "failed", "error": str(exc)}
        failed = runner_failed(results[name])
        if failed:
            failures.append(name)
        logger.log(
            logging.ERROR if failed else logging.INFO,
            "scheduled_runner name=%s status=%s duration_seconds=%.3f",
            name, results[name]["status"], monotonic() - started,
        )
    return results, failures
