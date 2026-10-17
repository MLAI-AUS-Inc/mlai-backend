"""Backend-owned source supersession history; it never grants execution authority.

Record before ordinary cancellation erases the reason an original run stopped.
Recovery must additionally prove current owner/goal/payment and remote fencing.
"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError

KEY = "article_source_fence_operation_id"
ACTION = "article-source-fence"
ORIGIN_STATES = {"queued", "running", "awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode", "completed", "failed", "blocked"}


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article source fence changed.")


def _fence_row(connection, identifier, *, lock=False):
    query = connection.operations
    if lock:
        query = query.select_for_update()
    return query.filter(pk=identifier, generation=connection.generation, action=ACTION).first()


def _run(connection, identifier, *, lock=True):
    from workflow_runs.models import ContentFactoryRun
    query = ContentFactoryRun.objects
    if lock:
        query = query.select_for_update()
    return query.filter(run_id=identifier, organization_id=connection.organization_id).first()


def _operation(connection, identifier):
    return connection.operations.select_for_update().filter(pk=identifier, generation=connection.generation, action="workflow").first()


def _authority(goal):
    from .website_first_article import _immutable_binding
    return {**_immutable_binding(goal), "owner_user_id": goal["consent"]["owner_user_id"]}


def _scope(value):
    return (isinstance(value, dict) and set(value) == set(goals.SCOPE_FIELDS)
        and all(type(value[key]) is int and value[key] >= minimum for key, minimum in
            (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))
        and all(isinstance(value[key], str) and 0 < len(value[key]) <= 255
            for key in ("company_id", "organization_id", "connection_id", "repository_id", "branch"))
        and isinstance(value["app_root"], str) and len(value["app_root"]) <= 1024)


def _validate(row, parent, *, for_stop=False):
    """Accept only a private recorded fence for this original active goal."""
    from .website_setup_consent import valid as valid_consent
    payload = goals.mapping(getattr(row, "payload", None))
    goal = goals.mapping(parent.receipt).get("goal")
    binding = goals.mapping(payload.get("binding"))
    if (row is None or row.action != ACTION or row.state not in ({"completed", "cancelled"} if for_stop else {"completed"})
            or str(row.connection_id) != str(parent.connection_id) or row.generation != parent.generation
            or parent.state not in ({"pending", "running", "needs_user", "needs_attention", "cancelled", "denied", "deleted", "completed", "stopped"}
                if for_stop else {"pending", "running", "needs_user"})
            or not goals.valid_goal(goal, goals.mapping(goal).get("scope")) or not valid_consent(goal)
            or goal.get("target") != "first_article_live" or goal.get("goal_id") != str(parent.pk)
            or set(payload) != {"schema_version", "binding", "recorded_at", "digest"}
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload["digest"] != goals.digest({key: value for key, value in payload.items() if key != "digest"})
            or set(binding) != {"goal", "root_run_id", "source_scope", "source_sha", "fence_scope", "fence_source_sha", "billing", "runs"}
            or binding.get("goal") != _authority(goal) or not goals.stamp(payload.get("recorded_at"))
            or not isinstance(binding.get("runs"), list) or not 1 <= len(binding["runs"]) <= 2
            or not _scope(binding.get("source_scope")) or not _scope(binding.get("fence_scope"))
            or any(binding["source_scope"].get(key) != goal["scope"].get(key) or binding["fence_scope"].get(key) != goal["scope"].get(key)
                for key in goals.SCOPE_FIELDS if key != "configuration_revision")
            or binding["source_scope"]["configuration_revision"] >= binding["fence_scope"]["configuration_revision"]
            or binding["fence_scope"]["configuration_revision"] > goal["scope"]["configuration_revision"]
            or any(not isinstance(binding.get(key), str) or not goals.SHA.fullmatch(binding[key]) for key in ("source_sha", "fence_source_sha"))
            or binding["source_sha"] == binding["fence_source_sha"]
            or parent.receipt.get(KEY) != str(row.pk)):
        _deny()
    identifiers = set()
    for item in binding["runs"]:
        if (not isinstance(item, dict) or set(item) != {"run_id", "operation_id", "operation_attempt", "run_request_digest", "status", "operation_state", "execution_version"}
                or not isinstance(item.get("run_id"), str) or not 0 < len(item["run_id"]) <= 100 or item["run_id"] in identifiers
                or str(UUID(item["operation_id"])) != item["operation_id"]
                or type(item.get("operation_attempt")) is not int or item["operation_attempt"] < 1
                or not isinstance(item.get("run_request_digest"), str) or not goals.DIGEST.fullmatch(item["run_request_digest"])
                or item.get("status") not in ORIGIN_STATES or item.get("operation_state") not in {"pending", "running", "completed", "failed", "blocked"}
                or item.get("execution_version") is not None and (not isinstance(item["execution_version"], list)
                    or len(item["execution_version"]) != 2 or any(type(value) is not int or value < 0 for value in item["execution_version"]))):
            _deny()
        identifiers.add(item["run_id"])
    if binding["root_run_id"] not in identifiers:
        _deny()
    return deepcopy(payload)


def validated(connection, parent, identifier):
    """Reload a current private marker; malformed metadata cannot become authority."""
    try:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            _deny()
        return _validate(_fence_row(connection, identifier, lock=True), parent)
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _deny()


def record(parent, config, *, rebound, now):
    """Record trusted source-only stop causality inside the caller's site lock.

    This removes old effects. It neither debits nor proves permission to resume;
    actual original-spend/refund and current-owner checks remain mandatory later.
    """
    from .website_article_recovery import enabled
    from .website_setup_consent import valid as valid_consent
    from .website_goal_scope import transition_chain
    from .website_first_article_billing import _reservation
    from .website_models import WebsiteConnectionOperation
    candidate = SimpleNamespace(pk=parent.pk, state=parent.state, generation=parent.generation,
        connection=parent.connection, payload=parent.payload, receipt={**parent.receipt, "goal": rebound})
    if not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(candidate):
        return None
    old = goals.mapping(parent.receipt).get("goal")
    if goals.mapping(old).get("target") != "first_article_live" or not parent.receipt.get("first_article_run_id"):
        return None
    connection = config.website_connection
    if KEY not in parent.receipt and goals.mapping(goals.mapping(old).get("scope")).get("configuration_revision", 0) >= connection.configuration_version:
        # An owned merge can update source without a source-only scope edge.
        return None
    if (str(connection.pk) != str(parent.connection_id) or parent.generation != connection.generation
            or parent.state not in {"pending", "running", "needs_user"}
            or not goals.valid_goal(old, goals.mapping(old).get("scope")) or not valid_consent(old)
            or not goals.valid_goal(rebound, goals.scope_for(connection, parent.payload.get("company_id"))) or not valid_consent(rebound)
            or rebound.get("target") != "first_article_live" or rebound.get("goal_id") != str(parent.pk)
            or _authority(old) != _authority(rebound) or rebound.get("current_source_sha") != connection.verified_sha):
        _deny()
    existing = parent.receipt.get(KEY)
    if existing is not None:
        validated(connection, parent, existing)
        return _fence_row(connection, existing)
    root = _run(connection, parent.receipt["first_article_run_id"])
    request = goals.mapping(getattr(root, "run_request", None))
    if root is None or root.status in {"cancelled", "denied"}:
        # An earlier owner stop is not source supersession.
        return None
    billing = _reservation(parent, old)
    source, revision = request.get("expected_source_sha"), request.get("configuration_revision")
    if (root.workflow != "article_generation" or root.status not in ORIGIN_STATES
            or root.domain.casefold() != connection.organization.domain.casefold() or root.github_repo.casefold() != connection.github_repo.casefold()
            or any(str(request.get(key)) != str(value) for key, value in {
                "website_connection_id": connection.pk, "connection_generation": connection.generation,
                "repository_id": connection.repository_id, "first_article_goal_id": parent.pk,
                "first_article_goal_operation_id": parent.pk, "client_request_id": billing["client_request_id"],
                "roo_points_billing_user_id": billing["payer_user_id"],
                "branch": connection.branch, "app_root": connection.app_root, "deletion_epoch": old["scope"]["deletion_epoch"]}.items())
            or not isinstance(source, str) or not goals.SHA.fullmatch(source) or source == connection.verified_sha
            or str(request.get("roo_points_ledger_id") or "") != str(billing["spend_ledger_id"] or "")
            or type(revision) is not int or not 1 <= revision < connection.configuration_version):
        _deny()
    source_scope = {**old["scope"], "configuration_revision": revision}
    edges = connection.operations.filter(generation=connection.generation, action="goal-source-scope", state="completed",
        payload__to_revision__gt=revision, payload__to_revision__lte=connection.configuration_version).order_by("created_at", "pk")[:16]
    if transition_chain(source_scope, rebound["scope"], [goals.mapping(edge.payload) for edge in edges], source_sha=connection.verified_sha) is None:
        _deny()
    root_result = goals.mapping(root.result)
    child_ids = {value for key in ("publish_child_run_id", "promoted_publish_job_id") if (value := root_result.get(key))}
    if len(child_ids) > 1 or any(not isinstance(identifier, str) or identifier == root.run_id for identifier in child_ids):
        _deny()
    runs, operations, captured = [root], {}, []
    for identifier in child_ids:
        child = _run(connection, identifier)
        saved = goals.mapping(getattr(child, "run_request", None))
        if (child is None or child.status not in ORIGIN_STATES or saved.get("source_run_id") != root.run_id
                or child.github_repo.casefold() != connection.github_repo.casefold()
                or any(str(saved.get(key)) != str(request.get(key)) for key in
                    ("website_connection_id", "connection_generation", "repository_id", "first_article_goal_id", "first_article_goal_operation_id", "deletion_epoch", "expected_source_sha"))):
            _deny()
        runs.append(child)
    for run in runs:
        saved = goals.mapping(run.run_request)
        try:
            if (not isinstance(saved.get("operation_id"), str) or str(UUID(saved["operation_id"])) != saved["operation_id"]
                    or type(saved.get("operation_attempt")) is not int or saved["operation_attempt"] < 1):
                _deny()
        except (ValueError, KeyError, TypeError):
            _deny()
        operation = _operation(connection, saved.get("operation_id"))
        if (operation is None or operation.state not in {"pending", "running", "completed", "failed", "blocked"}
                or operation.payload.get("attempt") != saved.get("operation_attempt")
                or operation.payload.get("run_id") not in {root.run_id, run.run_id}
                or operation.payload.get("first_article_goal_operation_id") != str(parent.pk)
                or KEY in goals.mapping(operation.receipt)):
            _deny()
        result = goals.mapping(run.result)
        version = [result.get("generation"), result.get("state_version")]
        if any(type(value) is not int or value < 0 for value in version):
            version = None
        captured.append({"run_id": run.run_id, "operation_id": str(operation.pk), "operation_attempt": saved.get("operation_attempt"),
            "run_request_digest": goals.digest(saved), "status": run.status, "operation_state": operation.state, "execution_version": version})
        operations[str(operation.pk)] = operation
    binding = {"goal": _authority(old), "root_run_id": root.run_id, "source_scope": source_scope, "source_sha": source,
        "fence_scope": deepcopy(rebound["scope"]), "fence_source_sha": connection.verified_sha,
        "billing": {key: billing[key] for key in ("client_request_id", "payer_user_id", "spend_ledger_id", "cost_points")}, "runs": captured}
    payload = {"schema_version": 1, "binding": binding, "recorded_at": now.isoformat()}
    payload["digest"] = goals.digest(payload)
    key = f"{connection.pk}:article-source-fence:{root.run_id}:{request['operation_id']}:{request['operation_attempt']}"
    row, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key,
        defaults={"connection": connection, "generation": connection.generation, "action": ACTION, "state": "completed", "payload": payload,
            "receipt": {"status": "source_fence_recorded", "remote_confirmed": False, "effect_authority": False}})
    if not created:
        _deny()
    parent.receipt = {**parent.receipt, KEY: str(row.pk)}
    parent.save(update_fields=["receipt", "updated_at"])
    for operation in operations.values():
        operation.receipt = {**goals.mapping(operation.receipt), KEY: str(row.pk)}
        operation.save(update_fields=["receipt", "updated_at"])
    return row


def cancellation_parent(connection, operation, *, lock=True):
    """Resolve a stopped article's original goal for an explicit owner stop.

    The marker is private durable metadata. HTTP data cannot select this target.
    Stopping a fenced article must stop its original goal instead of allowing a
    future automatic refresh to undo the owner's action.
    """
    identifier = goals.mapping(operation.receipt).get(KEY)
    if identifier is None:
        return None
    try:
        row = _fence_row(connection, str(UUID(identifier)), lock=lock)
        binding = goals.mapping(goals.mapping(getattr(row, "payload", None)).get("binding"))
        parent_id = goals.mapping(binding.get("goal")).get("goal_id")
        query = connection.operations.select_for_update() if lock else connection.operations
        parent = query.filter(pk=parent_id, action="prepare", generation=connection.generation).first()
        if (row is None or row.state not in {"completed", "cancelled"} or parent is None
                or parent.receipt.get(KEY) != identifier or parent.pk != operation.pk and not any(
                    item.get("operation_id") == str(operation.pk) and item.get("operation_attempt") == operation.payload.get("attempt")
                    for item in binding.get("runs", []) if isinstance(item, dict))):
            _deny()
        _validate(row, parent, for_stop=True)
        return parent, row
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        _deny()


def authorize_automatic_cancellation(connection, operation, identifier):
    """A server-only argument permits the exact automatic stop, never a resume."""
    selected = cancellation_parent(connection, operation)
    if selected is None or str(selected[1].pk) != str(identifier) or selected[1].state != "completed":
        _deny()
    _validate(selected[1], selected[0])
    return selected


def cancellation_run_matches(connection, operation, run_id):
    """Permit only stopping/acknowledging a pre-recorded child of the fenced op."""
    selected = cancellation_parent(connection, operation, lock=False)
    if selected is None:
        return False
    node = next((item for item in selected[1].payload["binding"]["runs"] if item["run_id"] == run_id
        and item["operation_id"] == str(operation.pk) and item["operation_attempt"] == operation.payload.get("attempt")), None)
    if node is None:
        return False
    run = _run(connection, run_id, lock=False)
    return run is not None and node["run_request_digest"] == goals.digest(run.run_request)


def read_original(parent, config, run):
    """Read only original paid metadata through a freshly proved source fence.

    This narrowly replaces generic operation lookup for the historical paid
    root. It cannot grant worker artifact access, token, resume or approval.
    """
    from .website_article_recovery import enabled
    from .website_prepare import _prepare_context
    from .website_first_article_billing import for_generation
    from .website_connections import authority_guard, contract_for
    if not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(parent):
        _deny()
    context, current_config, user = _prepare_context(parent)
    if str(current_config.website_connection_id) != str(config.website_connection_id):
        _deny()
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        current, billing, _, _ = for_generation(parent.pk, context=context, user=user)
        if current.pk != parent.pk or current.receipt.get("goal") != parent.receipt.get("goal"):
            _deny()
        payload = validated(connection, parent, parent.receipt.get(KEY))
        binding = payload["binding"]
        if (binding["root_run_id"] != run.run_id or parent.receipt.get("first_article_run_id") != run.run_id
                or binding["billing"] != {key: billing[key] for key in ("client_request_id", "payer_user_id", "spend_ledger_id", "cost_points")}
                or run.status in {"denied"}):
            _deny()
        node = next((item for item in binding["runs"] if item["run_id"] == run.run_id), None)
        if node is None or node["run_request_digest"] != goals.digest(run.run_request):
            _deny()
        operation = _operation(connection, node["operation_id"])
        if (operation is None or operation.state in {"denied", "deleted"}
                or operation.payload.get("attempt") != node["operation_attempt"]
                or goals.mapping(operation.receipt).get(KEY) != parent.receipt[KEY]):
            _deny()
        run._article_source_fence_read = {"operation_id": parent.receipt[KEY], "origin_status": node["status"],
            "execution_version": deepcopy(node["execution_version"])}
    return run


def _fenced_operation_for_run(context, request):
    from .website_models import WebsiteConnectionOperation
    try:
        identifier, connection_id = UUID(request["operation_id"]), UUID(request["website_connection_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if type(request.get("connection_generation")) is not int:
        return None
    return WebsiteConnectionOperation.objects.filter(pk=identifier, connection_id=connection_id,
        connection__organization_id=context.organization.pk, generation=request["connection_generation"],
        action="workflow", receipt__has_key=KEY).first()


def owner_stop_for_run(*, context, run, action):
    """Make both ordinary draft controls revoke the original fenced goal.

    Look up the durable marker even after rollback disables source refresh.
    Caller data cannot choose the goal, operation, or automatic-stop argument.
    """
    from .vibe_marketing_views import _get_config
    from .website_connections import contract_for, authority_guard
    from .website_operations import cancel_operation, deletion_epoch
    if action not in {"cancel", "deny"}:
        return None
    request = goals.mapping(run.run_request)
    operation = _fenced_operation_for_run(context, request)
    if operation is None:
        return None
    config = _get_config(context.organization)
    connection = config.website_connection
    if (connection is None or run.organization_id != context.organization.pk
            or str(operation.connection_id) != str(connection.pk) or operation.generation != connection.generation
            or str(request.get("operation_id")) != str(operation.pk)
            or request.get("operation_attempt") != operation.payload.get("attempt")):
        _deny()
    with authority_guard(contract_for(connection), action="read") as connection:
        operation = _operation(connection, request["operation_id"])
        if operation is None or operation.payload.get("attempt") != request.get("operation_attempt"):
            _deny()
        selected = cancellation_parent(connection, operation)
        node = next((item for item in selected[1].payload["binding"]["runs"] if item["run_id"] == run.run_id), None) if selected else None
        if (node is None or node["operation_id"] != str(operation.pk)
                or node["operation_attempt"] != request["operation_attempt"]):
            _deny()
        return cancel_operation(config, data={**contract_for(connection), "operation_id": str(operation.pk),
            "operation_attempt": request["operation_attempt"], "deletion_epoch": deletion_epoch(connection)},
            idempotency_key=goals.digest({"source_fence": str(selected[1].pk), "owner_action": action, "run_id": run.run_id}))
