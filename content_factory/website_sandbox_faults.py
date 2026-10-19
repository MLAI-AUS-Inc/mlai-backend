"""Fixture-only effects at real controller boundaries in an isolated sandbox.

This is a client of the separate acceptance sidecar, not a product control API.
No request field, model output or environment default can select a target.
"""
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from django.utils import timezone
import requests

from . import website_goal as goals, website_zoo_scope as scope

FAULTS = {"merge_delay": "merge_observation", "deployment_delay": "deployment_observation",
    "disconnect_refund": "refund_settlement", "budget_exhaustion": "goal_budget"}


class SandboxFaultUnavailable(ValueError):
    """Static acceptance diagnostic; never exposes file, URL or credential data."""


def configured():
    """Default off without filesystem, database or network work."""
    return (os.environ.get("APP_ENV") == "sandbox"
        and os.environ.get("ARTICLES_SANDBOX_FAULT_MODE") == "isolated-acceptance")


def _configuration():
    if not configured():
        return None
    path = os.environ.get("CONTENT_FACTORY_SANDBOX_FAULT_CONFIG", "")
    if not path or not Path(path).is_absolute():
        raise SandboxFaultUnavailable("sandbox_fault_manifest_unavailable")
    config = scope.read_observation(path)
    expiry = goals.stamp(config.get("expires_at")) if isinstance(config, dict) else None
    now = timezone.now()
    if (not isinstance(config, dict) or type(config.get("schema_version")) is not int
            or config["schema_version"] != 1
            or not re.fullmatch(r"articles-acceptance-[a-z0-9-]{1,64}", str(config.get("deployment_id", "")))
            or not goals.DIGEST.fullmatch(str(config.get("manifest_digest", "")))
            or not goals.SHA.fullmatch(str(config.get("controller_revision", "")))
            or expiry is None or not 0 < (expiry - now).total_seconds() <= 86400
            or not isinstance(config.get("cases"), list) or not 1 <= len(config["cases"]) <= 256):
        raise SandboxFaultUnavailable("sandbox_fault_manifest_invalid")
    base = os.environ.get("ONE_CLICK_SANDBOX_CONTROL_URL", "")
    token = os.environ.get("ONE_CLICK_SANDBOX_CONTROL_TOKEN", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or len(base) > 2048 or len(token) < 32):
        raise SandboxFaultUnavailable("sandbox_fault_transport_unavailable")
    return config, base.rstrip("/"), token


def admit_budget_profile(op, goal):
    """Freeze a lower operator-only fixture cap before the original reservation."""
    if not configured():
        return goal
    try:
        configured_values = _configuration()
    except Exception:
        return goal
    if configured_values is None:
        return goal
    config, _base, _token = configured_values
    connection = op.connection
    matches = [case for case in config["cases"] if isinstance(case, dict)
        and case.get("fault") == "budget_exhaustion"
        and all(str(case.get(key)) == str(expected) for key, expected in {
            "company_id": op.payload.get("company_id"), "website_connection_id": connection.pk,
            "connection_generation": connection.generation, "repository_id": connection.repository_id,
            "github_repo": connection.github_repo, "domain": connection.organization.domain}.items())]
    if not matches:
        return goal
    from . import website_goal_budget as budget
    from .website_owner_transport import required
    from django.conf import settings
    case = matches[0]
    profile = case.get("goal_budget_profile")
    if (len(matches) != 1 or case.get("sandbox") is not True or case.get("max_injections") != 1
            or not scope.identifier(case.get("case_id")) or not required(connection.organization_id)
            or not budget.enabled() or not re.fullmatch(r"[A-Za-z0-9_.-]+/mlai-articles-fixture-[a-z0-9-]+", connection.github_repo)
            or not isinstance(profile, dict) or set(profile) != {"active_seconds"}
            or not budget.numeric(profile["active_seconds"])
            or not 0 < profile["active_seconds"] <= min(7200, settings.ARTICLES_GOAL_ACTIVE_SECONDS)):
        from .website_contract import WebsiteAuthorityError
        raise WebsiteAuthorityError("website_prepare_blocked", "The isolated acceptance budget profile is invalid.")
    frozen = {"version": 1, "origin": "isolated_acceptance_operator_v1", "goal_id": goal["goal_id"],
        "authority_digest": goals.digest({key: value for key, value in goal["scope"].items() if key != "configuration_revision"}), "manifest_digest": config["manifest_digest"],
        "case_id": case["case_id"], "active_seconds": profile["active_seconds"]}
    return {**goal, "sandbox_budget_profile": {**frozen, "digest": goals.digest(frozen)}}


def _select(op, context, run_id, boundary):
    configured_values = _configuration()
    if configured_values is None:
        return None
    config, base, token = configured_values
    connection = op.connection
    goal = goals.mapping(op.receipt.get("goal"))
    stopped_budget = (boundary == "goal_budget" and op.state == "needs_attention"
        and op.receipt.get("budget_stop_reason") == "active_compute")
    if (op.action != "prepare" or op.state != "running" and not stopped_budget or op.generation != connection.generation
            or connection.state != "connected" or goal.get("goal_id") != str(op.pk)
            or not goals.valid_goal(goal, goals.scope_for(connection, context.company.pk))
            or str(op.payload.get("company_id")) != str(context.company.pk)
            or str(context.organization.pk) != str(connection.organization_id)
            or not scope.identifier(run_id, run=True)):
        raise SandboxFaultUnavailable("sandbox_fault_original_goal_mismatch")
    matches = []
    for case in config["cases"]:
        if not isinstance(case, dict) or FAULTS.get(case.get("fault")) != boundary:
            continue
        if any(str(case.get(key)) != str(expected) for key, expected in {
            "company_id": context.company.pk, "website_connection_id": connection.pk,
            "connection_generation": connection.generation, "repository_id": connection.repository_id,
            "github_repo": connection.github_repo, "domain": connection.organization.domain}.items()):
            continue
        if (not scope.identifier(case.get("case_id")) or case.get("sandbox") is not True
                or type(case.get("max_injections")) is not int or case["max_injections"] != 1
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/mlai-articles-fixture-[a-z0-9-]+", connection.github_repo)
                or connection.organization.domain in {"vyavos.com", "talathrive.com", "talathrive.com.au", "mlai.au"}
                or type(case.get("connection_generation")) is not int or type(case.get("repository_id")) is not int):
            raise SandboxFaultUnavailable("sandbox_fault_fixture_scope_invalid")
        parameters = case.get("parameters", {})
        if (not isinstance(parameters, dict) or set(parameters) - {"delay_seconds", "failure_count", "http_status"}
                or any(type(value) is not int for value in parameters.values())
                or not 0 <= parameters.get("delay_seconds", 5) <= 120):
            raise SandboxFaultUnavailable("sandbox_fault_parameters_invalid")
        matches.append(case)
    if len(matches) != 1:
        return None
    # The ordinary saved operation, not the local fixture file, selects the run.
    if scope._run_locator(run_id, company=context.company, connection=connection, operation=op) is None:
        raise SandboxFaultUnavailable("sandbox_fault_owned_run_unavailable")
    case = matches[0]
    binding = {key: case[key] for key in ("company_id", "website_connection_id", "connection_generation", "repository_id")}
    binding.update(goal_id=str(op.pk), manifest_digest=config["manifest_digest"])
    return case, base, token, goals.digest(binding)


def _post(base, token, action, case_id, payload):
    """Use one fixed sidecar path with bounded HTTPS and no ambient credentials."""
    with requests.Session() as client:
        client.trust_env = False
        with client.post(base + "/v1/" + action + "/" + case_id,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                json=payload, stream=True, allow_redirects=False, timeout=(3, 8)) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise SandboxFaultUnavailable("sandbox_fault_control_unavailable")
            raw = bytearray()
            started = time.monotonic()
            for chunk in response.iter_content(chunk_size=4096):
                if len(raw) + len(chunk) > 65536 or time.monotonic() - started > 10:
                    raise SandboxFaultUnavailable("sandbox_fault_control_bounds")
                raw.extend(chunk)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SandboxFaultUnavailable("sandbox_fault_control_invalid")
    return value


def stage(op, context, *, run_id, boundary, forward, disconnect=None, observe_budget=None):
    """Apply one claimed real effect, then continue the unchanged domain path.

The disconnect closure is source-owned and calls the existing lifecycle method.
It cannot be selected by an HTTP caller or carry a caller-supplied command.
"""
    if not configured():
        return forward()
    try:
        selected = _select(op, context, run_id, boundary)
    except Exception:
        return forward()
    if selected is None:
        return forward()
    case, base, token, binding_digest = selected
    payload = {"goal_id": str(op.pk), "run_id": run_id, "boundary": boundary}
    try:
        claim = _post(base, token, "claim", case["case_id"], payload)
        if (set(claim) != {"version", "claim_id", "fault", "parameters", "case_id", "goal_id", "run_id", "boundary", "binding_digest"}
                or type(claim.get("version")) is not int or claim["version"] != 1
                or not re.fullmatch(r"[a-f0-9]{32}", str(claim.get("claim_id", "")))
                or any(claim.get(key) != value for key, value in payload.items())
                or claim.get("case_id") != case["case_id"] or claim.get("fault") != case["fault"]
                or claim.get("binding_digest") != binding_digest
                or claim.get("parameters") != case.get("parameters", {})):
            raise SandboxFaultUnavailable("sandbox_fault_claim_mismatch")
    except Exception:
        return forward()
    completion = {**payload, "claim_id": claim["claim_id"]}
    try:
        if case["fault"] == "disconnect_refund":
            if disconnect is None:
                raise SandboxFaultUnavailable("sandbox_fault_boundary_unavailable")
            facts = disconnect()
        elif case["fault"] == "budget_exhaustion":
            if observe_budget is None:
                raise SandboxFaultUnavailable("sandbox_fault_boundary_unavailable")
            facts = observe_budget()
        else:
            delay = max(1, case.get("parameters", {}).get("delay_seconds", 5))
            started = time.monotonic()
            time.sleep(delay)
            elapsed = time.monotonic() - started
            if elapsed < delay:
                raise SandboxFaultUnavailable("sandbox_fault_delay_unobserved")
            facts = {"observed": True, "delay_seconds": math.floor(elapsed)}
        _post(base, token, "complete", case["case_id"], {**completion, "facts": facts})
    except BaseException:
        try:
            _post(base, token, "abort", case["case_id"], completion)
        except Exception:
            pass
        raise
    return None if case["fault"] == "disconnect_refund" else forward()


def disconnect_before_article(op, context, config):
    """Revoke the selected paid fixture before article dispatch; settle normally."""
    if not configured() or op.receipt.get("first_article_run_id") or not op.receipt.get("first_article_billing"):
        return False
    from .website_first_article import article_run
    if article_run(op, config) is not None:
        return False
    run_id = op.receipt.get("setup_run_id") or op.receipt.get("merged_setup_run_id")
    if not run_id:
        return False
    disconnected = False
    def effect():
        nonlocal disconnected
        from .website_connections import transition_connection, contract_for
        before = op.connection.generation
        lifecycle = transition_connection(config, action="disconnect", expected=contract_for(op.connection),
            idempotency_key="acceptance-disconnect:" + str(op.pk))
        config.refresh_from_db()
        current = config.website_connection
        if current.state != "disconnected" or current.generation != before + 1:
            raise SandboxFaultUnavailable("sandbox_fault_disconnect_unobserved")
        disconnected = True
        return {"observed": True, "before_generation": before, "after_generation": current.generation,
            "settlement_id": str(lifecycle.pk)}
    stage(op, context, run_id=run_id, boundary="refund_settlement", forward=lambda: None, disconnect=effect)
    return disconnected


def observe_budget_settlement(op, settlement_operation):
    """Complete a real whole-goal budget effect only after its settlement commits."""
    if not configured() or op.receipt.get("budget_stop_reason") != "active_compute":
        return
    from . import website_goal_budget as budget
    from .website_first_article_billing import _reservation
    from .website_first_article_settlement import _saved, _binding
    goal = goals.mapping(op.receipt.get("goal"))
    used = goals.mapping(goal.get("budget_used"))
    limit = budget.active_limit(goal)
    if (op.state != "needs_attention" or op.next_attempt_at is not None
            or not budget.numeric(limit) or limit <= 0
            or not budget.numeric(used.get("active_seconds")) or used["active_seconds"] < limit
            or settlement_operation.state != "completed"
            or settlement_operation.payload.get("goal_operation_id") != str(op.pk)):
        return
    billing = _reservation(op, goal)
    proof = _saved(op, _binding(op, billing))
    if (not proof.get("history_complete") or proof.get("status") not in {"refundable", "retained"}
            or proof.get("dispatch") == "unknown" or any(row.get("state") not in {"no_value", "retained"}
                for row in proof.get("children", []))):
        return
    from founder_tools.models import VibeRaisingCompany
    from types import SimpleNamespace
    company = VibeRaisingCompany.objects.filter(pk=op.payload.get("company_id"),
        organization_id=op.connection.organization_id).first()
    if company is None:
        return
    context = SimpleNamespace(company=company, organization=op.connection.organization)
    run_id = next((op.receipt[key] for key in ("first_article_publish_run_id", "first_article_run_id", "setup_run_id",
        "scan_run_id", "child_run_id") if op.receipt.get(key)), None)
    if run_id is None:
        return
    stage(op, context, run_id=run_id, boundary="goal_budget", forward=lambda: None,
        observe_budget=lambda: {"observed": True, "settlement_id": str(settlement_operation.pk)})
