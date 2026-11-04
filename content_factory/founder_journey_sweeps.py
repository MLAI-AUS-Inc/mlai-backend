"""P1 founder-journey minute sweeps (IF-P1-21).

The scheduler (``run_scheduled_discovery``) calls :func:`run_founder_journey_sweeps`
once per tick while ``MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED`` is on. Each
entry is ``"module:function"``, imported lazily: a module that is not there
yet (a sibling package that has not merged) is skipped silently. Each sweep
runs in its own try/except, also checks its own flag, and a failing sweep
never stops the next one or the scheduler.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SWEEPS = (
    "content_factory.startup_first_profile:sweep",
    "content_factory.founder_notification_outbox:enqueue_ready_drafts",
    "content_factory.founder_notification_outbox:drain",
)


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _summary(result):
    if isinstance(result, dict):
        status = result.get("status")
        return status if isinstance(status, str) else "completed"
    return "completed"


def run_founder_journey_sweeps(now=None, sweeps=SWEEPS) -> dict:
    """Run every founder-journey sweep once and return a status per entry. Never raises."""
    now = now or timezone.now()
    results = {}
    for entry in sweeps:
        module_name, _, function_name = str(entry).partition(":")
        if not module_name or not function_name or not _module_available(module_name):
            results[entry] = "missing"
            continue
        try:
            function = getattr(importlib.import_module(module_name), function_name, None)
            if function is None:
                results[entry] = "missing"
                continue
            results[entry] = _summary(function(now))
        except Exception as exc:  # noqa: BLE001 - one sweep must never stop the next
            logger.warning("founder_journey_sweep_failed sweep=%s error=%s", entry, type(exc).__name__, exc_info=True)
            results[entry] = "error"
    return {"status": "completed", "sweeps": results,
            "errors": sum(1 for value in results.values() if value == "error")}


def scheduled_founder_journey_sweeps() -> dict:
    """The scheduler entry: run the sweeps only while V2 is on."""
    if not getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED", False):
        return {"status": "disabled"}
    return run_founder_journey_sweeps()
