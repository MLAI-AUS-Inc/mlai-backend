"""The P2 self-heal sweep runner (IF-P2-08).

``run_scheduled_discovery`` calls :func:`scheduled_self_heal_sweeps` once per
tick, after the dispatch outbox step and whatever
``MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED`` says. Each entry names a runner in a
P2 package by module and function and is imported lazily, so a package that
has not merged yet reports ``"missing"``. Every runner has the signature
``(now=None, limit=None) -> dict``, checks its own flag first and returns
``{"skipped": "flag_off"}`` while off. A runner that raises is logged
``self_heal_sweep_failed name=<name>``, reported as ``"error"`` and never stops
the next one. The order matters: policy before refunds, everything before
the alert rules.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import sys

from django.utils import timezone

logger = logging.getLogger(__name__)

SWEEPS: tuple[tuple[str, str, str], ...] = (
    ("failure_policy", "content_factory.failure_policy_engine", "run_policy_sweep"),                # be-policy
    ("no_delivery_refunds", "content_factory.no_delivery_refunds", "run_refund_sweep"),             # be-runs
    ("merge_worker", "content_factory.publish_merge_worker", "run_merge_sweep"),                    # be-merge
    ("deployment_watch", "content_factory.publish_merge_worker", "run_deployment_watch_sweep"),     # be-merge
    ("hosted_auto_register", "content_factory.hosted_auto_register", "run_hosted_sweep"),           # be-publish
    ("founder_transitions", "content_factory.founder_transitions", "run_transition_sweep"),         # be-notify
    ("team_requests", "founder_tools.startup_team", "run_team_request_sweep"),                      # be-team (held)
    ("staff_alert_rules", "content_factory.staff_alert_rules", "run_alert_rule_sweep"),             # be-staff
    ("self_heal_report", "content_factory.failure_policy_engine", "run_dry_run_report_sweep"),      # be-policy
)
MISSING = "missing"
ERROR = "error"


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _runner(module_name: str, function_name: str):
    if not _module_available(module_name):
        return None
    module = importlib.import_module(module_name)
    function = getattr(module, function_name, None)
    return function if callable(function) else None


def run_self_heal_sweeps(now=None, sweeps=SWEEPS) -> dict[str, dict | str]:
    """Run every self-heal sweep once, in order, and return each one's result. Never raises.

    The value per name is the runner's own dict, ``"missing"`` when its
    package is not there, or ``"error"`` when it raised.
    """
    now = now or timezone.now()
    results: dict[str, dict | str] = {}
    for name, module_name, function_name in sweeps:
        try:
            function = _runner(module_name, function_name)
        except Exception:  # noqa: BLE001 - a broken import must not stop the next sweep
            logger.warning("self_heal_sweep_failed name=%s stage=import", name, exc_info=True)
            results[name] = ERROR
            continue
        if function is None:
            results[name] = MISSING
            continue
        try:
            result = function(now=now)
            summary = result if isinstance(result, dict) else {"result": str(type(result).__name__)}
            summary = json.loads(json.dumps(summary, default=str, allow_nan=False))
        except Exception:  # noqa: BLE001 - one sweep must never stop the next
            logger.warning("self_heal_sweep_failed name=%s", name, exc_info=True)
            results[name] = ERROR
            continue
        results[name] = summary
    return results


def scheduled_self_heal_sweeps() -> dict:
    """The scheduler entry: one tick of every sweep, in the scheduler's status shape."""
    sweeps = run_self_heal_sweeps()
    return {"status": "completed", "sweeps": sweeps, "errors": sum(1 for value in sweeps.values() if value == ERROR)}
