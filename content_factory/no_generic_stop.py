"""No generic stop reaches a founder (plan B10, BE runtime invariant).

A terminal failure whose code is missing, generic (``UNCLASSIFIED``,
``INTERNAL_ERROR``, ``TERMINAL_CODE_MISSING``, ``UNREGISTERED_FAILURE_CODE``),
unregistered, or a needs_user code with no founder action is ours to fix: the
founder sees "We're fixing this" with the same no-charge and refund lines as a
stop, and staff are alerted at once. This holds on both founder paths: the P2
copy ids (``registry_policy.copy_id_for``) and the P1 journey cases, which run
when ``ARTICLES_FAILURE_POLICY_V2_ENABLED`` is off or the org is outside the
cohort. Refunds are unchanged: every code that replaces ``UNCLASSIFIED`` is in
``incident_guards.NO_DELIVERY_REFUND_CODES``.

Switch: ``ARTICLES_NO_GENERIC_STOP_ENABLED`` (default on). Off restores the
``UNCLASSIFIED`` ingestion default and the "We couldn't write this one" copy.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

FIXING = "fixing"
GENERIC_CODES = frozenset({"UNCLASSIFIED", "INTERNAL_ERROR", "TERMINAL_CODE_MISSING", "UNREGISTERED_FAILURE_CODE"})
TERMINAL_STATUSES = frozenset({"failed", "blocked", "denied", "cancelled", "stopped"})
OWNER_CANCEL_REASONS = frozenset({"owner_cancelled", "draft_deleted", "article_discarded", "superseded"})


def enabled() -> bool:
    """Whether the no-generic-stop invariant is on (``ARTICLES_NO_GENERIC_STOP_ENABLED``)."""
    return bool(getattr(settings, "ARTICLES_NO_GENERIC_STOP_ENABLED", True))


def uncoded_failure_code() -> str:
    """The code a terminal callback without one is stored under.

    ``TERMINAL_CODE_MISSING`` (registered, refunded, alerted) with the switch
    on; the legacy ``UNCLASSIFIED`` with it off.
    """
    return "TERMINAL_CODE_MISSING" if enabled() else "UNCLASSIFIED"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def run_failure_code(run) -> str | None:
    """The causal code a run's saved result names, if any."""
    result = _mapping(getattr(run, "result", None))
    code = _mapping(result.get("failure")).get("code") or result.get("error_code")
    return code.strip() if isinstance(code, str) and code.strip() else None


def generic_stop_reason(code: str | None, organization=None) -> str | None:
    """The staff alert reason when ``code`` would be a generic stop, else None.

    ``codeless_terminal`` for a missing or generic code,
    ``unregistered_failure_code`` for a code the registry does not know, and
    ``our_team_item`` for a needs_user code that names no founder action.
    """
    from . import registry_policy
    if not code or code.strip().upper() in GENERIC_CODES:
        return "codeless_terminal"
    entry = registry_policy.effective_entry(code, organization) or registry_policy.effective_entry(code.upper(), organization)
    if entry is None:
        return "unregistered_failure_code"
    if entry.get("failure_class") == "needs_user" and not entry.get("action_id"):
        copy_id = entry.get("founder_copy_id")
        if not (isinstance(copy_id, str) and copy_id.startswith("ask_")):
            return "our_team_item"
    return None


def is_generic_stop(run, organization=None) -> bool:
    """Whether ``run`` is terminal (not cancelled by its owner) on a generic stop."""
    if not enabled() or run is None or getattr(run, "status", None) not in TERMINAL_STATUSES:
        return False
    result = _mapping(getattr(run, "result", None))
    if run.status == "cancelled" and (result.get("cancellation_reason") in OWNER_CANCEL_REASONS
                                      or not run_failure_code(run)):
        return False
    return generic_stop_reason(run_failure_code(run), organization) is not None


def alert_generic_stop(run) -> str | None:
    """Queue the staff alert for a generic stop at once; idempotent per run and reason."""
    organization = getattr(run, "organization", None)
    if not is_generic_stop(run, organization) or organization is None or getattr(run, "organization_id", None) is None:
        return None
    reason = generic_stop_reason(run_failure_code(run), organization)
    from .founder_journey_alerts import queue_founder_journey_alert
    try:
        return queue_founder_journey_alert(organization=organization, alert_reason=reason, run=run)
    except ValueError:
        logger.warning("generic_stop_alert_rejected run=%s reason=%s", getattr(run, "run_id", None), reason)
        return None
