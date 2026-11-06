"""Read-only original-goal scope evidence for a future trusted zoo collector.

This module emits no runtime eligibility, flags or qualification. A recorded
night selects logical cases; saved backend ownership selects their worker runs.
"""
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from . import website_goal as goals

MAX_BYTES = 2 * 1024 * 1024
MAX_RUNS = 64
RUN_FIELDS = ("child_run_id", "scan_run_id", "setup_run_id", "merged_setup_run_id",
    "hosted_verification_run_id", "first_article_run_id", "first_article_publish_run_id")
SCOPE_FIELDS = ("domain", "github_repo", "website_connection_id", "connection_generation", "repository_id",
    "operation_id", "operation_attempt", "deletion_epoch", "client_request_id")
OPTIONAL_FIELDS = ("app_root", "branch", "expected_source_sha", "configuration_revision", "connection_target_id")
WORKFLOWS = {"repo_scan", "article_system_setup", "article_generation", "article_publish", "publish_article",
    "direct_generate", "confirmed_topic", "hosted_native_verification"}


class ScopeInputError(ValueError):
    """Fixed diagnostic; never includes input text, paths or provider data."""


def identifier(value, *, run=False):
    """Admit only bounded opaque IDs, excluding credential-shaped prefixes."""
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}" if run
        else r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,109}", value) is not None
        and not value.startswith(("ghp_", "github_pat_", "sk-", "xoxb-", "xoxp-", "AKIA", "ASIA")))


def uuid_id(value):
    """Require the actual canonical UUID shape used by company/goal models."""
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _open_no_follow(path):
    """Open each directory and the final file relative to a verified descriptor."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def read_observation(path):
    """Read one bounded regular artifact without following any path link."""
    descriptor = None
    try:
        descriptor = _open_no_follow(path)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino)
                != (after.st_size, after.st_mtime_ns, after.st_ino)):
            raise ValueError
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ScopeInputError("Invalid or unavailable bounded zoo observation.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def select_cases(value):
    """Freeze autonomous identities and the separate declared policy cohort."""
    if not isinstance(value, dict):
        raise ScopeInputError("Invalid zoo observation identity.")
    identity = (value.get("mode"), value.get("acceptance_profile"), value.get("schema_version"))
    if any(not isinstance(item, (str, int)) for item in identity):
        raise ScopeInputError("Invalid zoo observation identity.")
    target = {("ready-sandbox", "ready", 1): "ready",
        ("one-shot-sandbox", "first-article", 3): "first_article_live"}.get(
            identity)
    if (not target or type(value.get("schema_version")) is not int or not identifier(value.get("run_id"))
            or not goals.DIGEST.fullmatch(str(value.get("manifest_digest") or ""))):
        raise ScopeInputError("Invalid zoo observation identity.")
    cases, policies, rows = value.get("cases"), value.get("policy_cases"), value.get("results")
    if (not isinstance(cases, list) or not 1 <= len(cases) <= 256 or not isinstance(policies, list)
            or len(policies) > 512 or any(not identifier(key) for key in policies)
            or len(set(policies)) != len(policies) or not isinstance(rows, list) or len(rows) > len(cases)):
        raise ScopeInputError("Invalid frozen zoo cohorts.")
    selected = {}
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "framework", "fault"}
                or not identifier(case.get("id")) or case["id"] in selected
                or not identifier(case.get("framework")) or not identifier(case.get("fault"))):
            raise ScopeInputError("Invalid frozen zoo cohorts.")
        selected[case["id"]] = None
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("case_id"), str) or row["case_id"] not in selected
                or selected[row["case_id"]] is not None):
            raise ScopeInputError("Invalid original-goal result coverage.")
        selected[row["case_id"]] = row
    return target, selected, sorted(policies)


def _unknown(case_id, reason):
    return {"case_id": case_id, "state": "unknown", "reason": reason, "runs": [], "runs_complete": False}


def _lineage(request, original_goal):
    result = {}
    for key in ("first_article_goal_id", "first_article_goal_operation_id", "strategy_goal_id"):
        if key in request:
            if request[key] != original_goal:
                return None
            result[key] = request[key]
    if "goal_budget" in request:
        budget = goals.mapping(request["goal_budget"])
        if budget.get("goal_id") != original_goal or not goals.DIGEST.fullmatch(str(budget.get("scope_digest") or "")):
            return None
        result["goal_budget"] = {key: budget[key] for key in ("goal_id", "scope_digest")}
    return result or None


def _run_locator(run_id, *, company, connection, operation):
    from django.db.models import F
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnectionOperation
    from .website_operations import deletion_epoch
    from .setup_delivery_enrollment import delivery_workflow
    run = ContentFactoryRun.objects.annotate(scope_generation=F("result__generation"),
        scope_state_version=F("result__state_version")).only("run_id", "workflow", "domain", "github_repo",
            "organization_id", "run_request").filter(run_id=run_id, organization_id=company.organization_id).first()
    request = goals.mapping(getattr(run, "run_request", None))
    if (run is None or run.workflow not in WORKFLOWS or run.github_repo != connection.github_repo
            or run.domain != connection.organization.domain or request.get("website_connection_id") != str(connection.pk)
            or type(request.get("connection_generation")) is not int or request["connection_generation"] != connection.generation
            or type(request.get("repository_id")) is not int or request["repository_id"] != connection.repository_id
            or type(request.get("deletion_epoch")) is not int or request["deletion_epoch"] != deletion_epoch(connection)):
        return None
    lineage = _lineage(request, str(operation.pk))
    if lineage is None or any(key not in request for key in SCOPE_FIELDS) or not uuid_id(request.get("operation_id")):
        return None
    child_op = WebsiteConnectionOperation.objects.filter(pk=request["operation_id"], connection_id=connection.pk,
        generation=connection.generation, action="workflow").first()
    payload = goals.mapping(getattr(child_op, "payload", None))
    if (child_op is None or payload.get("run_id") != run_id
            or type(request.get("operation_attempt")) is not int
            or request["operation_attempt"] != payload.get("attempt", 1)):
        return None
    scope = {key: request[key] for key in (*SCOPE_FIELDS, *OPTIONAL_FIELDS) if key in request}
    if request.get("domain") != run.domain or request.get("github_repo") != run.github_repo:
        return None
    if (any(not isinstance(scope[key], str) or not 0 < len(scope[key]) <= 255
            for key in ("domain", "github_repo", "client_request_id"))
            or request["operation_attempt"] < 1):
        return None
    # Only digests cross the operator boundary: even a valid original root,
    # target ID, client request key, domain or repository may contain private data.
    execution = {key: getattr(run, "scope_" + key, None)
        if type(getattr(run, "scope_" + key, None)) is int and getattr(run, "scope_" + key) >= 0 else None
        for key in ("generation", "state_version")}
    try:
        return {"run_id": run_id, "workflow": delivery_workflow(run), "scope_fields": sorted(scope),
            "request_scope_digest": goals.digest(scope), "lineage_fields": sorted(lineage),
            "goal_lineage_digest": goals.digest(lineage), "execution": execution}
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def _resolve_case(case_id, row, *, target):
    """Read exact saved ownership; missing or contradictory lineage stays unknown."""
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnectionOperation
    if row is None:
        return _unknown(case_id, "not_observed")
    binding = goals.mapping(row.get("started_binding"))
    if (not uuid_id(binding.get("companyId")) or not uuid_id(binding.get("connectionId"))
            or type(binding.get("connectionGeneration")) is not int or binding["connectionGeneration"] < 1
            or not uuid_id(row.get("original_goal_id"))):
        return _unknown(case_id, "original_binding_unavailable")
    company = VibeRaisingCompany.objects.only("id", "organization_id").filter(pk=binding["companyId"]).first()
    if company is None or company.organization_id is None:
        return _unknown(case_id, "company_unavailable")
    config = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(
        organization_id=company.organization_id).only("website_connection_id", "website_connection__id",
            "website_connection__organization_id", "website_connection__generation",
            "website_connection__repository_id", "website_connection__github_repo", "website_connection__branch",
            "website_connection__app_root", "website_connection__configuration_version", "website_connection__blockers",
            "website_connection__organization__domain").first()
    connection = getattr(config, "website_connection", None)
    if (connection is None or connection.organization_id != company.organization_id
            or str(connection.pk) != binding["connectionId"] or connection.generation != binding["connectionGeneration"]):
        return _unknown(case_id, "selected_connection_changed")
    operation = WebsiteConnectionOperation.objects.filter(pk=row["original_goal_id"], connection_id=connection.pk,
        generation=connection.generation, action="prepare").first()
    goal = goals.mapping(goals.mapping(getattr(operation, "receipt", None)).get("goal"))
    scope = goals.scope_for(connection, company.pk)
    if (operation is None or goals.mapping(operation.payload).get("company_id") != str(company.pk)
            or goal.get("goal_id") != str(operation.pk) or goal.get("target") != target or not goals.valid_goal(goal, scope)):
        return _unknown(case_id, "original_goal_unproved")
    history = goal.get("child_run_history", [])
    references = [goals.mapping(operation.receipt).get(key) for key in RUN_FIELDS]
    if (not isinstance(history, list) or len(history) > MAX_RUNS or any(not identifier(key, run=True) for key in history)
            or any(key is not None and not identifier(key, run=True) for key in references)):
        return _unknown(case_id, "child_references_unproved")
    run_ids = sorted(set(history + [key for key in references if key is not None]))
    if len(run_ids) > MAX_RUNS:
        return _unknown(case_id, "child_reference_capacity")
    locators = [_run_locator(key, company=company, connection=connection, operation=operation) for key in run_ids]
    complete = bool(run_ids) and all(item is not None for item in locators)
    return {"case_id": case_id, "state": "bound", "reason": None if complete else "child_lineage_unproved",
        "company_id": str(company.pk), "organization_id": str(company.organization_id),
        "original_goal_id": str(operation.pk), "target": target, "scope_digest": goals.digest(scope),
        "runs": [item for item in locators if item is not None], "runs_complete": complete}


def resolve_case(case_id, row, *, target):
    """Malformed saved scope cannot leak provider text or acquire an association."""
    try:
        return _resolve_case(case_id, row, target=target)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return _unknown(case_id, "saved_scope_unavailable")


def scope_report(value):
    """Produce attribution evidence only; callers must provide a read-only transaction."""
    from django.utils import timezone
    target, cases, policies = select_cases(value)
    return {"schema_version": 1, "basis": "backend_saved_original_goal_scope_v1", "scope_evidence_only": True,
        "observed_at": timezone.now().isoformat(),
        "observation_run_id": value["run_id"], "manifest_digest": value["manifest_digest"],
        "policy_cases": policies, "target": target,
        "cases": [resolve_case(key, cases[key], target=target) for key in sorted(cases)]}
