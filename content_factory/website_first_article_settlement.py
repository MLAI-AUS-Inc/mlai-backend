"""Bounded child evidence for the parent that owns one first-article payment.

The caller holds organization, connection and terminal parent locks. Nothing in
this module contacts the worker or infers no effects from a terminal status.
"""
from django.db.models import Q

from . import website_goal as goals
from .article_publication_evidence import HANDOFF_FIELDS, cancellation_receipt, publication_state, useful_delivery
from .run_state import ARTICLE_WORKFLOWS

KEY = "first_article_settlement_v1"
MAX_CHILDREN = 32
NO_WORK = {"dispatch_never_received", "received_not_started", "dispatch_rejected"}


def _binding(op, billing):
    return goals.digest({"goal_id": str(op.pk), "billing": billing["binding"],
        "client_request_id": billing["client_request_id"], "payer": billing["payer_user_id"],
        "ledger": billing["spend_ledger_id"], "points": billing["cost_points"]})


def _owned(run, op, billing, by_id, visiting=None):
    """Publish children may inherit payment through their exact source lineage."""
    request = goals.mapping(run.run_request)
    scope = op.receipt["goal"]["scope"]
    repository = request.get("github_repo", run.github_repo)
    if (run.workflow not in ARTICLE_WORKFLOWS or str(run.organization_id) != scope["organization_id"]
        or run.domain.casefold() != op.connection.organization.domain.casefold()
        or not run.github_repo or not isinstance(repository, str) or repository.casefold() != run.github_repo.casefold()
        or request.get("first_article_goal_operation_id") != str(op.pk)
        or request.get("first_article_goal_id") != str(op.pk)
        or request.get("website_connection_id") != scope["connection_id"]
        or type(request.get("connection_generation")) is not int
        or request["connection_generation"] != scope["connection_generation"]
        or str(request.get("repository_id")) != scope["repository_id"]
        or type(request.get("deletion_epoch")) is not int or request["deletion_epoch"] != scope["deletion_epoch"]
        or any(key in request and request[key] != scope[key] for key in ("branch", "app_root"))):
        return False
    source = by_id.get(request.get("source_run_id"))
    if source is not None:
        if source.github_repo.casefold() != run.github_repo.casefold():
            return False
        ancestor = goals.mapping(source.run_request)
        if any(key in request and key in ancestor and request[key] != ancestor[key]
                for key in ("github_repo", "expected_source_sha", "branch", "app_root")):
            return False
    payment = {"client_request_id": billing["client_request_id"], "roo_points_ledger_id": billing["spend_ledger_id"],
        "roo_points_billing_user_id": billing["payer_user_id"]}
    present = [key for key in payment if key in request
        and (request[key] not in (None, "") or request[key] is None and payment[key] is None)]
    if any(str(request[key]) != str(payment[key]) for key in present):
        return False
    source = request.get("source_run_id")
    if len(present) == len(payment) and not source:
        return True
    visiting = (visiting or set()) | {run.run_id}
    return bool(source and source not in visiting and source in by_id
        and _owned(by_id[source], op, billing, by_id, visiting))


def _child(run, op, billing, by_id):
    item = {"run_id": run.run_id, "state": "unknown", "stop_receipt_id": None, "identity_digest": _identity(run)}
    if not _owned(run, op, billing, by_id):
        return item
    try:
        proof = cancellation_receipt(run, goals.mapping(run.result))
        result = run.result
        handed = _handoff_ids(run)
        if handed and all(identifier in by_id and identifier != run.run_id
                and _owned(by_id[identifier], op, billing, by_id)
                and _descends(by_id[identifier], run.run_id, by_id) for identifier in handed):
            # Exact children are independently classified in this same locked
            # graph. Their stop/value outcomes still govern the parent's credit.
            result = {key: value for key, value in goals.mapping(run.result).items() if key not in HANDOFF_FIELDS}
            for key in ("result", "latest_control_response"):
                if isinstance(result.get(key), dict):
                    result[key] = {name: value for name, value in result[key].items() if name not in HANDOFF_FIELDS}
        effects = publication_state(run, result=result)
    except (ValueError, TypeError):
        return item
    if proof is None:
        if run.status == "completed" and (effects in {"article", "unknown"} or useful_delivery(run)):
            return item
        item["state"] = "stop_required"
        return item
    item.update(stop_receipt_id=proof["receipt_id"], generation=proof["generation"], state_version=proof["state_version"])
    if (run.status != "cancelled" or effects in {"article", "unknown"}
        or proof["outcome"] != "no_article_effect" or proof["delivered_value"] is None):
        return item
    item["state"] = "retained" if useful_delivery(run) or proof["delivered_value"] else "no_value"
    return item


def _handoff_ids(run):
    result = goals.mapping(run.result)
    return {value for surface in (result, goals.mapping(result.get("result")), goals.mapping(result.get("latest_control_response")))
        for key in ("publish_child_run_id", "promoted_publish_job_id")
        if isinstance(value := surface.get(key), str) and 0 < len(value) <= 100}


def _identity(run):
    return goals.digest({"run_id": run.run_id, "organization_id": str(run.organization_id), "domain": run.domain,
        "github_repo": run.github_repo, "request": run.run_request})


def _descends(run, ancestor, by_id):
    seen = set()
    while run.run_id not in seen and len(seen) < MAX_CHILDREN:
        seen.add(run.run_id)
        source = goals.mapping(run.run_request).get("source_run_id")
        if source == ancestor:
            return True
        if source not in by_id:
            return False
        run = by_id[source]
    return False


def _saved(op, binding):
    value = goals.mapping(op.receipt.get(KEY))
    try:
        if (set(value) == {"version", "binding", "children", "dispatch", "status", "erased", "history_complete", "digest"}
            and type(value["version"]) is int and value["version"] == 1 and value["binding"] == binding
            and type(value["erased"]) is bool
            and type(value["history_complete"]) is bool
            and value["digest"] == goals.digest({key: item for key, item in value.items() if key != "digest"})
            and value["dispatch"] in {"not_dispatched", "no_work", "delivered", "unknown"}
            and isinstance(value["children"], list) and len(value["children"]) <= MAX_CHILDREN
            and all(_valid_saved_child(item) for item in value["children"])):
            return value
    except (ValueError, TypeError):
        pass
    return {}


def _valid_saved_child(item):
    if (not isinstance(item, dict) or set(item) - {"run_id", "state", "stop_receipt_id", "generation", "state_version", "identity_digest"}
        or not isinstance(item.get("run_id"), str) or not 0 < len(item["run_id"]) <= 100
        or item.get("state") not in {"unknown", "stop_required", "no_value", "retained"}):
        return False
    if item["state"] in {"no_value", "retained"}:
        digest = item.get("stop_receipt_id")
        return (isinstance(digest, str) and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
            and all(type(item.get(key)) is int and item[key] >= 0 for key in ("generation", "state_version")))
    return True


def capture(op, billing, *, erasing=False):
    """Persist minimal financial proof before erasure; missing children stay unknown."""
    from workflow_runs.models import ContentFactoryRun
    from .dispatch_models import ContentFactoryDispatchOutbox
    binding = _binding(op, billing)
    previous = _saved(op, binding)
    expected = {op.receipt[key] for key in ("first_article_run_id", "first_article_publish_run_id")
        if isinstance(op.receipt.get(key), str) and op.receipt[key]}
    expected.update(item["run_id"] for item in previous.get("children", []) if isinstance(item, dict) and item.get("run_id"))
    dispatch = ContentFactoryDispatchOutbox.objects.select_for_update().filter(
        organization_id=op.connection.organization_id, client_request_id=billing["client_request_id"]).first()
    dispatch_state = "not_dispatched"
    dispatch_repo = None
    if dispatch is not None:
        dispatch_state = "unknown"
        request = goals.mapping(goals.mapping(dispatch.payload).get("request"))
        if (request.get("first_article_goal_operation_id") == str(op.pk)
            and request.get("first_article_goal_id") == str(op.pk)
            and request.get("client_request_id") == billing["client_request_id"]):
            dispatch_repo = request.get("github_repo")
            if dispatch.state == "delivered" and dispatch.run_id:
                dispatch_state = "delivered"
                expected.add(dispatch.run_id)
            elif dispatch.state == "failed" and not dispatch.run_id and dispatch.last_error in NO_WORK:
                dispatch_state = "no_work"
    elif previous:
        dispatch_state = previous["dispatch"] if previous["erased"] else (
            "not_dispatched" if previous["dispatch"] == "not_dispatched" else "unknown")
    query = (Q(run_request__first_article_goal_operation_id=str(op.pk))
        | Q(run_request__client_request_id=billing["client_request_id"]) | Q(run_id__in=sorted(expected)))
    rows = list(ContentFactoryRun.objects.select_for_update().filter(
        query, organization_id=op.connection.organization_id).order_by("pk")[:MAX_CHILDREN + 1])
    # Include descendants whose sparse worker mirror omitted the goal identity;
    # they cannot disappear from the obligation merely by losing that metadata.
    by_id = {run.run_id: run for run in rows}
    for _ in range(MAX_CHILDREN):
        expected.update(identifier for run in by_id.values() for identifier in _handoff_ids(run))
        descendants = list(ContentFactoryRun.objects.select_for_update().filter(
            Q(run_request__source_run_id__in=sorted(by_id)) | Q(run_id__in=sorted(expected)),
            organization_id=op.connection.organization_id)
            .exclude(run_id__in=sorted(by_id)).order_by("pk")[:MAX_CHILDREN + 1]) if by_id else []
        if not descendants:
            break
        by_id.update((run.run_id, run) for run in descendants)
        if len(by_id) > MAX_CHILDREN:
            break
    children = [_child(run, op, billing, by_id) for run in list(by_id.values())[:MAX_CHILDREN]]
    if isinstance(dispatch_repo, str) and dispatch_repo:
        for item in children:
            if by_id[item["run_id"]].github_repo.casefold() != dispatch_repo.casefold():
                item["state"] = "unknown"
    saved_by_id = {item["run_id"]: item for item in previous.get("children", [])}
    for identifier in sorted(expected - set(by_id)):
        # Positive stored stop proof survives content erasure; absence itself is
        # never proof. Unknown and missing-stop obligations survive identically.
        children.append(saved_by_id[identifier] if previous.get("erased") and identifier in saved_by_id else
            {"run_id": identifier, "state": "unknown", "stop_receipt_id": None})
    overflow = len(by_id) > MAX_CHILDREN or len(children) > MAX_CHILDREN
    children = children[:MAX_CHILDREN]
    states = {item["state"] for item in children}
    complete = not overflow and not (op.receipt.get(KEY) is not None and not previous) and previous.get("history_complete", True)
    if not complete or dispatch_state == "unknown" or "unknown" in states:
        status = "needs_reconciliation"
    elif "stop_required" in states:
        status = "stop_required"
    else:
        draft = any(item["name"] == "first_draft" and item["state"] == "passed"
            and goals.valid_proof("first_draft", item.get("proof"), scope=op.receipt["goal"]["scope"],
                source_sha=goals.mapping(item.get("proof")).get("source_sha"),
                now=goals.stamp(goals.mapping(item.get("proof")).get("at"))) for item in op.receipt["goal"]["milestones"])
        status = "retained" if draft or "retained" in states else "refundable"
    evidence = {"version": 1, "binding": binding, "children": children, "dispatch": dispatch_state,
        "status": status, "erased": erasing or previous.get("erased", False), "history_complete": complete}
    evidence["digest"] = goals.digest(evidence)
    op.receipt = {**op.receipt, KEY: evidence}
    op.save(update_fields=["receipt", "updated_at"])
    return evidence


def stop_children(evidence, *, parent_id):
    """Request bounded stops after releasing billing locks; no credit occurs here."""
    from workflow_runs.models import ContentFactoryRun
    from .vibe_marketing_views import _call_content_factory_run_action
    from .website_cancellation_retirement import expected_article_cancellation
    from .service_views import _apply_cancellation_receipt
    from .website_models import WebsiteConnectionOperation
    from .website_first_article_billing import REFUND_TERMINAL, _reservation
    from .website_contract import WebsiteAuthorityError
    parent = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(pk=parent_id, action="prepare").first()
    if parent is None or parent.state not in REFUND_TERMINAL:
        return
    try:
        billing = _reservation(parent, goals.mapping(parent.receipt.get("goal")))
        if billing["status"] == "refunded" or _binding(parent, billing) != evidence["binding"]:
            return
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError):
        return
    for item in evidence["children"]:
        if item["state"] != "stop_required":
            continue
        run = ContentFactoryRun.objects.filter(run_id=item["run_id"]).first()
        if run is None or item.get("identity_digest") != _identity(run):
            continue
        try:
            remote = _call_content_factory_run_action(run_id=run.run_id, action="cancel", workflow=run.workflow,
                payload={"reason": "first_article_goal_ended",
                    "expected_cancellation_v1": expected_article_cancellation(run)}, transport_errors_are_pending=True)
            if cancellation_receipt(run, remote) is None:
                continue
            incoming = {**goals.mapping(remote.get("result")), **{key: remote[key]
                for key in ("article_cancellation_v1", "publication_evidence_v1") if key in remote}}
            _apply_cancellation_receipt(run.run_id, {"domain": run.domain, "status": "cancelled", "result": incoming})
        except Exception:
            # The retained parent outbox owns another bounded attempt, including
            # a lost stop response. Missing proof never becomes credit authority.
            continue
