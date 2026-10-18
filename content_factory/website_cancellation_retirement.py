"""Retire repeated cancel work without claiming absent effects or erasing obligations."""
from datetime import timedelta

from . import website_goal as goals
from .article_publication_evidence import cancellation_receipt, original_scope, original_goal, publication_state, useful_delivery
from .run_state import ARTICLE_WORKFLOWS

KEY = "cancel_work_v1"
LIMIT = 3
WINDOW = timedelta(hours=24)
FIELDS = {"version", "operation_id", "connection_id", "generation", "started_at", "deadline", "attempts", "run_ids"}
OBLIGATIONS = ("unconfirmed_cancel_run_ids", "article_effect_obligation_run_ids", "cancel_cleanup_obligation_run_ids")


def applicable(op):
    return op.action == "cancel-operation"


def valid(op):
    value = goals.mapping(op.receipt.get(KEY))
    start, end = goals.stamp(value.get("started_at")), goals.stamp(value.get("deadline"))
    return (set(value) == FIELDS and value.get("version") == 1 and type(value["version"]) is int
        and value["operation_id"] == str(op.pk) and value["connection_id"] == str(op.connection_id)
        and value["generation"] == op.generation and type(value["generation"]) is int
        and type(value["attempts"]) is int and 0 <= value["attempts"] <= LIMIT
        and isinstance(value["run_ids"], list) and 0 < len(value["run_ids"]) <= 200
        and all(isinstance(item, str) and 0 < len(item) <= 100 for item in value["run_ids"])
        and start and end and end - start == WINDOW)


def claim(op, *, now):
    """Persist the budget before remote work, independently of preview cleanup."""
    op._cancel_work_allowed = False
    if not applicable(op) or not op.payload.get("cancel_run_ids"):
        return
    if KEY not in op.receipt:
        op.receipt[KEY] = {"version": 1, "operation_id": str(op.pk), "connection_id": str(op.connection_id),
            "generation": op.generation, "started_at": now.isoformat(), "deadline": (now + WINDOW).isoformat(),
            "attempts": 0, "run_ids": list(op.payload["cancel_run_ids"])}
    if valid(op) and op.receipt[KEY]["attempts"] < LIMIT and now < goals.stamp(op.receipt[KEY]["deadline"]):
        op.receipt[KEY]["attempts"] += 1
        op._cancel_work_allowed = True


def can_call(op, *, now):
    value = goals.mapping(op.receipt.get(KEY))
    return bool(getattr(op, "_cancel_work_allowed", False) and valid(op) and 0 < value["attempts"] <= LIMIT and now < goals.stamp(value["deadline"])
        and goals.stamp(value["started_at"]) <= now + timedelta(minutes=5))


def run_for(op, run_id):
    """A local job must belong to this exact cancelled operation and website."""
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.filter(run_id=run_id, organization_id=op.connection.organization_id).first()
    request = goals.mapping(run.run_request) if run else {}
    binding = run_binding(run) if run else None
    owners = {str(op.payload.get("cancelled_operation_id")), *map(str, op.payload.get("cancelled_child_operation_ids", []))}
    if (run is None or binding is None or goals.mapping(op.payload.get("cancel_run_bindings")).get(run_id) != binding
        or str(run.organization_id) != str(op.connection.organization_id)
        or request.get("website_connection_id") != str(op.connection_id)
        or type(request.get("connection_generation")) is not int or request["connection_generation"] != op.generation
        or not ({request.get("operation_id"), request.get("first_article_goal_operation_id")} & owners)):
        return None
    if run.workflow in ARTICLE_WORKFLOWS and run.status == "completed" and (
            publication_state(run) in {"article", "unknown"} or useful_delivery(run)):
        return None
    return run


def run_binding(run):
    """Freeze the original cancellation identity without copying credentials/body."""
    try:
        return goals.digest({"run_id": run.run_id, "workflow": run.workflow,
            "organization_id": str(getattr(run, "organization_id", "")), "domain": getattr(run, "domain", ""),
            "github_repo": getattr(run, "github_repo", ""), "scope": original_scope(run), "goal_id": original_goal(run),
            "parent_operation_id": goals.mapping(run.run_request).get("first_article_goal_operation_id")})
    except (ValueError, TypeError):
        return None


def bindings_for(runs):
    """Record at the original intent under the owner's connection lock."""
    return {run.run_id: bound for run in list(runs)[:200] if (bound := run_binding(run)) is not None}


def expected_article_cancellation(run):
    """The worker compares this captured intent while holding its lifecycle lock."""
    return {"version": 1, "run_id": run.run_id, "goal_id": original_goal(run), "scope": original_scope(run)}


def scoped_nonarticle_request(run):
    """Use the original execution contract rather than the current operation row."""
    return {**original_scope(run), "run_id": run.run_id, "domain": run.domain, "github_repo": run.github_repo}


def _remember(op, key, run_id):
    values = op.receipt.setdefault(key, [])
    if run_id not in values:
        values.append(run_id)


def acknowledge(op, run, run_id, status, body):
    """Typed stop proof retires cancellation, including uncertain article effects."""
    if run is None or not isinstance(body, dict):
        return False
    if run.workflow in ARTICLE_WORKFLOWS:
        try:
            proof = cancellation_receipt(run, body)
        except (ValueError, TypeError):
            return False
        if status not in {200, 202} or proof is None:
            return False
        from .service_views import _apply_cancellation_receipt
        incoming = {**goals.mapping(body.get("result")), **{key: body[key]
            for key in ("article_cancellation_v1", "publication_evidence_v1") if key in body}}
        response = _apply_cancellation_receipt(run_id, {"domain": run.domain, "status": "cancelled", "result": incoming})
        if response.status_code != 200:
            return False
        op.receipt.setdefault("article_stop_proofs", {})[run_id] = {"receipt_id": proof["receipt_id"],
            "generation": proof["generation"], "state_version": proof["state_version"], "outcome": proof["outcome"]}
        if proof["outcome"] != "no_article_effect" or proof["delivered_value"] is None:
            _remember(op, "article_effect_obligation_run_ids", run_id)
        if body.get("cleanup_pending") is True or body.get("cleanupPending") is True:
            _remember(op, "cancel_cleanup_obligation_run_ids", run_id)
        return True
    request = goals.mapping(run.run_request)
    if type(request.get("operation_attempt")) is not int or request["operation_attempt"] < 1:
        return False
    # These observed non-article responses identify the exact fenced job/attempt.
    # Generic 200/404, a wrong endpoint and an untyped article response do not.
    direct = (body.get("run_id") == run_id and body.get("workflow") == run.workflow
        and body.get("status") == "cancelled" and type(body.get("operation_attempt")) is int
        and body.get("operation_id") == request.get("operation_id")
        and body["operation_attempt"] == request.get("operation_attempt"))
    scoped = (type(body.get("schema_version")) is int and body["schema_version"] == 1
        and type(body.get("operation_attempt")) is int and body.get("status") == "cancel_requested"
        and body.get("operation_id") == request.get("operation_id")
        and body.get("operation_attempt") == request.get("operation_attempt"))
    absent = (status == 404 and body.get("run_id") == run_id and body.get("workflow") == run.workflow
        and body.get("status") == "run_missing" and body.get("operation_id") == request.get("operation_id")
        and type(body.get("operation_attempt")) is int and body["operation_attempt"] == request.get("operation_attempt"))
    return bool((status == 200 and (direct or scoped) or absent)
        and body.get("cleanup_pending") is not True and body.get("cleanupPending") is not True)


def finish(op, *, now):
    """Retain uncertain cancellation separately while preview/token work continues."""
    if not applicable(op):
        return
    identifiers = op.payload.get("cancel_run_ids", [])
    value = goals.mapping(op.receipt.get(KEY))
    if identifiers and (not can_call(op, now=now) or value["attempts"] >= LIMIT):
        for identifier in identifiers:
            _remember(op, "unconfirmed_cancel_run_ids", identifier)
        op.payload = {**op.payload, "cancel_run_ids": []}
    if not any(op.receipt.get(key) for key in OBLIGATIONS):
        return
    op.receipt["remote_cleanup_pending"] = True
    other_work = (op.payload.get("cancel_run_ids") or op.payload.get("stop_preview_run_ids")
        or op.payload.get("disable_auto_merge_prs") or goals.mapping(op.receipt.get("tokens")).get("pending"))
    if not other_work:
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt["status"] = "cancel_requires_reconciliation"
        op.receipt.pop("user_action", None)
