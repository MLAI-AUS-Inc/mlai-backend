"""Read-only proof of original expected stops and exact reservation settlement.

A disconnected fixture retains its historical goal generation. This observer
may read that obligation without restoring repository authority or resuming it.
"""
from django.db import connection as database, transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_runtime_observation import HasObservationServiceKey, ObservationUnavailable, select_original


def billing_observation(op, *, spend, refund, settlement_operation):
    """Validate recorded charge/refund identity without acquiring financial locks."""
    from .website_first_article_billing import _reservation
    from .website_first_article_settlement import _saved, _binding
    unknown = {"coverage": "partial", "status": "unknown", "gaps": ["billing_lineage_unproved"]}
    try:
        billing = _reservation(op, op.receipt["goal"])
        key, points = billing["client_request_id"], billing["cost_points"]
        if points:
            if (spend is None or str(spend.pk) != billing["spend_ledger_id"]
                    or str(spend.user_id) != billing["payer_user_id"] or spend.kind != "SPEND"
                    or spend.source != "CONTENT_FACTORY" or spend.reference_type != "CONTENT_FACTORY"
                    or spend.reference_id != key or spend.idempotency_key != "content_factory:charge:" + key
                    or spend.delta_microroo != -points * 1_000_000):
                return unknown
        elif spend is not None:
            return unknown
        refunded = billing["status"] == "refunded"
        if refund is not None and (spend is None or str(refund.user_id) != billing["payer_user_id"]
                or refund.kind != "REFUND" or refund.source != "CONTENT_FACTORY"
                or refund.reference_type != "CONTENT_FACTORY" or refund.reference_id != key
                or refund.idempotency_key != "content_factory:refund:" + key
                or refund.delta_microroo != points * 1_000_000
                or refund.refund_of_id != spend.pk):
            return unknown
        if refunded != (refund is not None or points == 0 and refunded):
            return unknown
        settled = _saved(op, _binding(op, billing))
        settled_complete = bool(settled and settled["history_complete"]
            and settled["dispatch"] != "unknown"
            and all(item["state"] in {"no_value", "retained"} for item in settled["children"]))
        completed = bool(settlement_operation is not None and settlement_operation.action == "goal-refund"
            and settlement_operation.connection_id == op.connection_id and settlement_operation.generation == op.generation
            and settlement_operation.payload.get("goal_operation_id") == str(op.pk)
            and settlement_operation.payload.get("goal_id") == str(op.pk)
            and settlement_operation.state == "completed"
            and settlement_operation.receipt.get("goal_operation_id") == str(op.pk))
        return {"coverage": "complete", "gaps": [], "status": billing["status"], "cost_points": points,
            "reservation_digest": billing["digest"],
            "payer_digest": goals.digest(billing["payer_user_id"]),
            "request_digest": goals.digest(key), "spend_digest": goals.digest(str(spend.pk)) if spend else None,
            "refund_digest": goals.digest(str(refund.pk)) if refund else None,
            "settlement_digest": settled.get("digest"), "settlement_status": settled.get("status"),
            "history_complete": settled_complete, "dispatch": settled.get("dispatch"),
            "children": [{key: row[key] for key in ("run_id", "state", "stop_receipt_id") if key in row}
                for row in settled.get("children", [])],
            "settlement_completed": completed,
            "credit_proven": bool(refunded and settled_complete and completed and settled["status"] == "refundable")}
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError):
        return unknown


def child_observation(op, child, *, child_operation):
    """Reduce only an owned current-source typed prerequisite or budget refusal."""
    unknown = {"coverage": "partial", "gaps": ["policy_child_unproved"], "failure": None, "preflight": None}
    if child is None or child_operation is None:
        return unknown
    from .website_failure_contract import validate_failure
    from .website_zoo_scope import _lineage
    request = goals.mapping(child.run_request)
    scope = op.receipt["goal"]["scope"]
    if (child_operation.action != "workflow" or child_operation.connection_id != op.connection_id
            or child_operation.generation != op.generation or child_operation.payload.get("run_id") != child.run_id
            or str(child_operation.pk) != request.get("operation_id")
            or type(request.get("operation_attempt")) is not int
            or request["operation_attempt"] != child_operation.payload.get("attempt", 1)
            or str(child.organization_id) != scope["organization_id"]
            or child.github_repo != op.connection.github_repo or child.domain != op.connection.organization.domain
            or request.get("website_connection_id") != str(op.connection_id)
            or type(request.get("connection_generation")) is not int or request["connection_generation"] != op.generation
            or str(request.get("repository_id")) != scope["repository_id"]
            or request.get("deletion_epoch") != scope["deletion_epoch"]
            or request.get("expected_source_sha") != op.receipt["goal"].get("current_source_sha")
            or _lineage(request, str(op.pk)) is None or child.status not in {"failed", "blocked"}):
        return unknown
    result = goals.mapping(child.result)
    try:
        failure = validate_failure(result.get("failure"))
    except (ValueError, TypeError, KeyError):
        return unknown
    if failure["code"] not in {"MISSING_CONFIG", "RUN_BUDGET_EXHAUSTED"}:
        return unknown
    preflight = None
    if failure["code"] == "MISSING_CONFIG":
        from re import fullmatch
        def names(value, *, nonempty=False):
            return (isinstance(value, list) and (1 if nonempty else 0) <= len(value) <= 64
                and all(isinstance(name, str) and fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) for name in value)
                and value == sorted(set(value)))
        candidates = [result, goals.mapping(result.get("directory_build_report")),
            goals.mapping(goals.mapping(result.get("article_system_setup")).get("directory_build_report")),
            goals.mapping(goals.mapping(result.get("diagnostics")).get("directory_build_report")),
            goals.mapping(goals.mapping(goals.mapping(result.get("article_system_setup")).get("diagnostics")).get("directory_build_report"))]
        reports = [candidate for candidate in candidates if isinstance(candidate.get("build_input_preflight"), dict)]
        if not reports or any(candidate["build_input_preflight"] != reports[0]["build_input_preflight"] for candidate in reports):
            return unknown
        build = reports[0]
        report = build["build_input_preflight"]
        fields = {"schema_version", "status", "required_names", "registry_auth_names", "missing_names"}
        if (set(report) != fields or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("status") != "needs_input" or build.get("stage") != "input_preflight"
                or any(not names(report.get(key), nonempty=True) for key in ("missing_names", "registry_auth_names"))
                or not names(report.get("required_names"))
                or not set(report["registry_auth_names"]) <= set(report["required_names"])
                or not set(report["missing_names"]) <= set(report["required_names"])
                or not set(report["missing_names"]) & set(report["registry_auth_names"])
                or build.get("missing_requirements") != report["missing_names"]):
            return unknown
        preflight = {key: report[key] for key in fields}
    return {"coverage": "complete", "gaps": [], "run_id": child.run_id, "workflow": child.workflow,
        "run_state": child.status, "source_sha": request["expected_source_sha"],
        "request_scope_digest": goals.digest({key: request[key] for key in (
            "website_connection_id", "connection_generation", "repository_id", "deletion_epoch", "expected_source_sha")}),
        "failure": {key: failure[key] for key in ("code", "failure_class", "stage", "retryable",
            "failure_fingerprint", "registry_sha256")}, "preflight": preflight}


def resolve(row, target):
    """Resolve exact company ownership; historical reading grants no new authority."""
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnectionOperation
    from workflow_runs.models import ContentFactoryRun
    from roo.models import Ledger
    company_id = row["started_binding"]["companyId"]
    generation = row["started_binding"]["connectionGeneration"]
    company = VibeRaisingCompany.objects.only("id", "organization_id").filter(pk=company_id).first()
    if company is None or company.organization_id is None:
        raise ObservationUnavailable("original_goal_unavailable")
    config = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(
        organization_id=company.organization_id).first()
    connection = getattr(config, "website_connection", None)
    if connection is None or str(connection.pk) != row["started_binding"]["connectionId"]:
        raise ObservationUnavailable("original_goal_unavailable")
    op = WebsiteConnectionOperation.objects.filter(pk=row["original_goal_id"], connection=connection,
        generation=generation, action="prepare", payload__company_id=company_id).first()
    goal = goals.mapping(goals.mapping(getattr(op, "receipt", None)).get("goal"))
    saved = goals.mapping(goal.get("scope"))
    current = goals.scope_for(connection, company_id)
    if (op is None or goal.get("goal_id") != str(op.pk) or goal.get("target") != target
            or not goals.valid_goal(goal, saved) or saved.get("connection_generation") != generation
            or any(saved.get(key) != value for key, value in current.items()
                if key not in {"configuration_revision", "connection_generation"})
            or connection.generation not in {generation, generation + 1}
            or saved.get("configuration_revision") != connection.configuration_version - (connection.generation - generation)
            or connection.generation != generation and (connection.state != "disconnected" or op.state != "cancelled")):
        raise ObservationUnavailable("original_goal_unavailable")
    from .website_goal_audit import transport_coverage
    from .website_execution_observation import project as backend_execution
    from . import website_zoo_scope as scope_reader
    references = [op.receipt.get(key) for key in scope_reader.RUN_FIELDS]
    history = goal.get("child_run_history", [])
    runs_complete = (isinstance(history, list) and len(history) <= scope_reader.MAX_RUNS
        and all(scope_reader.identifier(key, run=True) for key in history)
        and all(key is None or scope_reader.identifier(key, run=True) for key in references))
    ids = sorted(set(history + [key for key in references if key is not None])) if runs_complete else []
    locators = [scope_reader._run_locator(key, company=company, connection=connection, operation=op,
        historical_policy=True) for key in ids] if len(ids) <= scope_reader.MAX_RUNS else []
    original = {"case_id": "selected-original-policy", "state": "bound", "reason": None,
        "company_id": str(company.pk), "organization_id": str(company.organization_id),
        "original_goal_id": str(op.pk), "target": target, "scope_digest": goals.digest(saved),
        "policy_historical_scope_v1": connection.generation == generation + 1, "runs": [item for item in locators if item is not None],
        "runs_complete": bool(ids) and len(ids) == len(locators) and all(item is not None for item in locators)}
    raw = goals.mapping(op.receipt.get("first_article_billing"))
    spend = Ledger.objects.filter(pk=raw.get("spend_ledger_id")).first() if raw.get("spend_ledger_id") else None
    refund = Ledger.objects.filter(idempotency_key="content_factory:refund:" + str(raw.get("client_request_id"))).first() if raw else None
    settlement = WebsiteConnectionOperation.objects.filter(connection=connection, generation=generation,
        action="goal-refund", payload__goal_operation_id=str(op.pk)).first()
    billing = billing_observation(op, spend=spend, refund=refund, settlement_operation=settlement)
    child_id = op.receipt.get("child_run_id")
    child = ContentFactoryRun.objects.filter(run_id=child_id, organization_id=company.organization_id).first() if child_id else None
    request = goals.mapping(getattr(child, "run_request", None))
    child_operation = WebsiteConnectionOperation.objects.filter(pk=request.get("operation_id"), connection=connection,
        generation=generation, action="workflow").first() if request.get("operation_id") else None
    refusal = child_observation(op, child, child_operation=child_operation)
    code = goals.mapping(refusal.get("failure")).get("code")
    from . import website_goal_budget as budget
    used = goals.mapping(goal.get("budget_used"))
    alert = goals.mapping(goal.get("budget_alert"))
    limit = budget.active_limit(goal)
    budget_proven = bool(op.receipt.get("budget_stop_reason") == alert.get("reason") == "active_compute"
        and goals.stamp(alert.get("at")) is not None and budget.numeric(limit) and limit > 0
        and budget.numeric(used.get("active_seconds")) and used["active_seconds"] >= limit
        and op.state == "needs_attention" and op.next_attempt_at is None and op.receipt.get("status") == "stopped")
    budget_observation = {"reason": op.receipt.get("budget_stop_reason"), "active_limit": limit,
        "active_seconds": used.get("active_seconds") if budget.numeric(used.get("active_seconds")) else None,
        "profile_digest": goals.mapping(goal.get("sandbox_budget_profile")).get("digest"),
        "controller_stop_proven": budget_proven, "usage_semantics": "original_goal_cumulative_usage_v1"}
    policy = ("disconnect_refund" if connection.state == "disconnected" and op.state == "cancelled"
        else "budget_exhaustion" if budget_proven or code == "RUN_BUDGET_EXHAUSTED"
        else "required_build_input" if code == "MISSING_CONFIG" else None)
    policy_satisfied = bool(policy == "disconnect_refund" and billing.get("credit_proven")
        or policy == "budget_exhaustion" and budget_proven and billing.get("history_complete")
        and billing.get("settlement_completed") and billing.get("settlement_status") in {"refundable", "retained"}
        or policy == "required_build_input" and refusal["coverage"] == "complete"
        and op.state in {"needs_user", "needs_attention", "stopped"} and op.next_attempt_at is None
        and op.receipt.get("reason_code") == code and op.receipt.get("failure_taxonomy_policy_applied") is True
        and op.receipt.get("retryable") is False)
    return {"version": 1, "basis": "original_goal_policy_v1", "goal_id": str(op.pk), "target": target,
        "scope_digest": goals.digest(saved), "original_generation": generation,
        "current_generation": connection.generation, "operation_state": op.state,
        "operation_attempt": op.attempts, "policy": policy, "policy_satisfied": policy_satisfied,
        "billing": billing, "refusal": refusal, "budget": budget_observation,
        "owner_transports": transport_coverage(op), "scope_evidence": original, "backend_execution": backend_execution(op),
        "observed_at": timezone.now().isoformat(),
        "coverage": "complete" if policy_satisfied else "partial",
        "gaps": [] if policy_satisfied else ["original_policy_not_yet_proven"]}


def read_policy(row, target):
    """An explicit read-only repeatable snapshot cannot trigger settlement work."""
    if (database.vendor != "postgresql" or database.in_atomic_block
            or database.connection is not None and not database.get_autocommit()):
        raise ObservationUnavailable("readonly_snapshot_unavailable")
    with transaction.atomic():
        with database.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return resolve(row, target)


class WebsitePolicyObservationView(APIView):
    """Read exact original policy/financial proof, including a disconnected goal."""
    authentication_classes = []
    permission_classes = [HasObservationServiceKey]

    def get(self, request):
        try:
            row, target = select_original(request.query_params)
            value = read_policy(row, target)
            from .website_github_action_archive import read_bound_archive
            from .website_execution_transport import fetch
            original = value.get("scope_evidence", {})
            archive = read_bound_archive(original)
            executions = fetch(original)
            latest = read_policy(row, target)
            if {key: item for key, item in value.items() if key != "observed_at"} != {key: item for key, item in latest.items() if key != "observed_at"}:
                raise ObservationUnavailable("original_observation_changed")
            value = {**value, "provider_action_archive": archive, "content_factory_executions": executions}
        except ObservationUnavailable as exc:
            return Response({"code": str(exc)}, status=409, headers={"Cache-Control": "no-store"})
        except Exception:
            return Response({"code": "original_policy_observation_unavailable"}, status=503,
                headers={"Cache-Control": "no-store"})
        return Response(value, headers={"Cache-Control": "no-store"})
