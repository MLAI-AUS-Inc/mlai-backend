"""Read-only rollout diagnostics; configuration never confers site authority."""

import os
import re
from datetime import datetime, timezone

from django.conf import settings

from . import website_goal as goals
from .website_failure_scope import taxonomy_enabled


# Each row reports configuration for new work. Sticky deliveries and financial
# settlement retain their existing rules when an operator turns a flag off.
RULES = (
    ("B1_observe", ("ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED",), "taxonomy", ("CONTENT_FACTORY_ARTICLES_FAILURE_TAXONOMY_EMIT_ENABLED",), "taxonomy_enabled"),
    ("B1_policy", ("ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED",), "taxonomy", ("CONTENT_FACTORY_ARTICLES_FAILURE_TAXONOMY_EMIT_ENABLED",), "taxonomy_enabled"),
    ("B2", ("ARTICLES_WEBSITE_STATUS_V1_ENABLED", "ARTICLES_WEBSITE_CONSISTENCY_ENABLED", "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED"), "goals", (), None),
    ("B5_setup", ("CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED", "CONTENT_FACTORY_SETUP_DELIVERY_RECONCILIATION_ENABLED"), "setup_delivery", ("CONTENT_FACTORY_SETUP_DELIVERY_V1_ENABLED", "CONTENT_FACTORY_SETUP_DELIVERY_V1_SEND_ENABLED"), "setup_delivery_enrolled"),
    ("B6", ("ARTICLES_WEBSITE_EVENT_WAKES_ENABLED",), "goals", (), None),
    ("B7", ("ARTICLES_WEBSITE_DOCTOR_ENABLED", "ARTICLES_WEBSITE_STATUS_V1_ENABLED"), None, (), None),
    ("B4", ("ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED",), "goals", ("CONTENT_FACTORY_ARTICLES_GOAL_USAGE_ENABLED",), "goal_usage_enabled"),
    ("B3", ("ARTICLES_WEBSITE_STRATEGY_ENABLED", "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED"), "goals", (), None),
    ("C1", ("ARTICLES_SETUP_CONSENT_ENABLED",), "goals", (), None),
    ("C2", ("ARTICLES_AUTO_PAGE_ENABLED",), "goals", (), None),
    ("C3", ("ARTICLES_HOSTED_VERIFICATION_ENABLED", "ARTICLES_HOSTED_ATTESTATION_ENABLED"), "goals", ("CONTENT_FACTORY_ARTICLES_HOSTED_VERIFICATION_ENABLED",), None),
    ("C4", ("ARTICLES_DEPLOYMENT_DETECTION_ENABLED",), None, (), None),
    ("C5", ("ARTICLES_GOAL_NOTIFICATIONS_ENABLED",), "goals", (), None),
    ("E1", ("ARTICLES_SITE_FINGERPRINT_ENABLED",), None, ("CONTENT_FACTORY_ARTICLES_SITE_FINGERPRINT_ENABLED",), None),
    ("E2", (), None, ("CONTENT_FACTORY_ARTICLES_REPRODUCIBLE_BUILDS_ENABLED", "CONTENT_FACTORY_ARTICLES_DEPENDENCY_CACHE_ENABLED"), None),
    ("E3", (), None, ("CONTENT_FACTORY_ARTICLES_EXISTING_INVENTORY_ENABLED",), None),
    ("D1", ("ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", "ARTICLES_SETUP_CONSENT_ENABLED"), "goals", (), None),
    ("D2_recovery", ("ARTICLES_ARTICLE_RECOVERY_ENABLED", "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", "ARTICLES_SETUP_CONSENT_ENABLED", "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED"), "goals", (), None),
    ("article_delivery", ("CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED", "ARTICLES_ARTICLE_RECOVERY_ENABLED", "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED"), "goals", ("CONTENT_FACTORY_ARTICLE_DELIVERY_V1_ENABLED", "CONTENT_FACTORY_SETUP_DELIVERY_V1_SEND_ENABLED"), None),
    ("F3", ("ARTICLES_COST_ACCOUNTING_ENABLED", "ARTICLES_OPERATIONAL_ALERTS_ENABLED"), "goals", ("CONTENT_FACTORY_ARTICLES_COST_ACCOUNTING_ENABLED",), None),
)
# Cosmetic admission is observed for configuration-window identity, while the
# delivery rules remain independent of its optional formatting behavior.
OBSERVATION_ONLY_FLAGS = ("CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED",)
BACKEND_FLAGS = tuple(sorted({name for _, names, _, _, _ in RULES for name in names}
    | set(OBSERVATION_ONLY_FLAGS)))
CF_FLAGS = tuple(sorted({name for _, _, _, names, _ in RULES for name in names}))
SCOPED_PREDICATES = ("taxonomy_enabled", "setup_delivery_enrolled", "goal_usage_enabled")


def _member(name, organization_id):
    values = getattr(settings, name, ())
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",") if item.strip()]
    return isinstance(values, (list, tuple, set, frozenset)) and str(organization_id) in {str(item) for item in values}


def _flag(name):
    value = getattr(settings, name, False)
    return value if type(value) is bool else None


def _snapshot(value, *, organization_id, run_id, now):
    """Whitelist a recent operator observation, never credentials or raw config."""
    unknown = {"source": "unobserved", "reason": "not_supplied", "revision": None,
        "observed_at": None, "flags": {name: None for name in CF_FLAGS},
        "run_predicates": {name: None for name in SCOPED_PREDICATES}}
    if value is None:
        return unknown
    try:
        observed = datetime.fromisoformat(value["observed_at"])
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or observed.tzinfo is None or not isinstance(value["flags"], dict)):
            raise ValueError
        if not -30 <= (now - observed).total_seconds() <= 300:
            return {**unknown, "reason": "stale_observation"}
        if str(value["organization_id"]) != str(organization_id):
            return {**unknown, "reason": "organization_mismatch"}
        flags = {name: value["flags"].get(name) if type(value["flags"].get(name)) is bool else None for name in CF_FLAGS}
        predicates = value.get("run_predicates")
        scoped = (isinstance(run_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id))
            and value.get("run_id") == run_id and isinstance(predicates, dict))
        return {"source": "operator_snapshot", "reason": None, "observed_at": observed.isoformat(),
            "revision": value.get("revision") if isinstance(value.get("revision"), str)
                and re.fullmatch(r"[0-9a-f]{40}", value["revision"]) else None,
            "flags": flags, "run_predicates": {name: predicates.get(name)
                if scoped and type(predicates.get(name)) is bool else None for name in SCOPED_PREDICATES}}
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return {**unknown, "reason": "invalid_observation"}


def rollout_report(organization_id, *, cf_snapshot=None, run_id=None, now=None):
    """Inspect actual backend settings and separately observed CF configuration.

    A configured row is eligible configuration for new work, not proof of a
    deployed release, active goal, site permission, or successful milestone.
    CF run predicates remain unknown without an exact observed run scope.
    """
    now = now or datetime.now(timezone.utc)
    flags = {name: _flag(name) for name in BACKEND_FLAGS}
    enrollment = {
        "taxonomy": _member("ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS", organization_id),
        "goals": _member("ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS", organization_id),
        "setup_delivery": _member("CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS", organization_id),
    }
    # Reuse the policy's actual combined predicate rather than inventing goal
    # eligibility from the runner switch or one enrollment list in isolation.
    goal_enrolled = goals.enrolled(organization_id)
    cf = _snapshot(cf_snapshot, organization_id=organization_id, run_id=run_id, now=now)
    rows = {}
    for step, names, scope, counterparts, predicate in RULES:
        backend = [flags[name] for name in names]
        if scope == "goals": backend.append(goal_enrolled)
        elif scope is not None: backend.append(enrollment[scope])
        if step.startswith("B1_"):
            backend.append(taxonomy_enabled(organization_id, observe=step == "B1_observe"))
        worker = [cf["flags"][name] for name in counterparts]
        if predicate: worker.append(cf["run_predicates"][predicate])
        values = backend + worker
        state = "disabled" if False in values else "unknown" if None in values else "configured"
        rows[step] = {"state": state, "backend_eligible": False if False in backend else None if None in backend else True,
            "backend_flags": list(names), "enrollment": scope, "cf_flags": list(counterparts), "cf_run_predicate": predicate}
    # Deploy exports the full SHA as APP_RELEASE; settings.APP_RELEASE shortens
    # it for display, so inspect the raw runtime value. Do not conceal an invalid
    # primary revision by substituting a conflicting compatibility fallback.
    revision = os.environ.get("APP_RELEASE", os.environ.get("APP_COMMIT_SHA", ""))
    return {"schema_version": 1, "observed_at": now.isoformat(), "organization_id": str(organization_id),
        "backend_revision": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None,
        "backend_flags": flags, "enrollment": enrollment, "goal_enrolled": goal_enrolled,
        "content_factory": cf, "steps": rows,
        "semantics": "new_work_configuration_only; not_permission_or_outcome; sticky_work_and_refunds_may_continue"}
