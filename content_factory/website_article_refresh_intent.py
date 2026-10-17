"""Journal source-refresh identity before reconciliation, reservation or dispatch.

This module supplies no worker/write/approval authority. Its pending intent is
an input to the remaining D2 coordinator, not permission to replay an old run.
"""
from copy import deepcopy
from uuid import UUID, uuid4

from django.conf import settings

from . import website_goal as goals, website_goal_budget as budget
from .website_contract import WebsiteAuthorityError

MAX_INTENTS = 4
KEY = "article_source_refresh"
HISTORY = "article_source_refresh_history"


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The saved article refresh binding changed.")


def _valid_binding(value):
    if not isinstance(value, dict) or set(value) != {"goal", "current_scope", "source_sha", "paid_root", "billing"}:
        return False
    scope, root, held, goal = (value[key] for key in ("current_scope", "paid_root", "billing", "goal"))
    if (not all(isinstance(item, dict) for item in (scope, root, held, goal))
            or set(scope) != set(goals.SCOPE_FIELDS)
            or any(type(scope[key]) is not int or scope[key] < minimum for key, minimum in
                (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))
            or any(not isinstance(scope[key], str) or not scope[key] for key in
                ("company_id", "organization_id", "connection_id", "repository_id", "branch"))
            or not isinstance(scope["app_root"], str)
            or set(goal) != {"goal_id", "authority_digest", "answers_digest"}
            or not isinstance(goal["goal_id"], str) or not goal["goal_id"]
            or any(not isinstance(goal[key], str) or not goals.DIGEST.fullmatch(goal[key])
                for key in ("authority_digest", "answers_digest"))
            or goal["authority_digest"] != goals.digest({key: item for key, item in scope.items() if key != "configuration_revision"})
            or set(root) != {"run_id", "source_sha", "configuration_revision", "operation_id", "operation_attempt", "generation", "state_version"}
            or not isinstance(root["run_id"], str) or not 0 < len(root["run_id"]) <= 100
            or any(type(root[key]) is not int or root[key] < minimum for key, minimum in
                (("configuration_revision", 1), ("operation_attempt", 1), ("generation", 0), ("state_version", 0)))
            or root["configuration_revision"] >= scope["configuration_revision"]
            or not isinstance(root["operation_id"], str) or str(UUID(root["operation_id"])) != root["operation_id"]
            or any(not isinstance(sha, str) or not goals.SHA.fullmatch(sha) for sha in (root["source_sha"], value["source_sha"]))
            or root["source_sha"] == value["source_sha"] or len(root["source_sha"]) != len(value["source_sha"])
            or set(held) != {"client_request_id", "payer_user_id", "spend_ledger_id", "cost_points"}
            or held["client_request_id"] != "articles-first:" + goals.digest({"goal_id": goal["goal_id"],
                "scope": {key: item for key, item in scope.items() if key != "configuration_revision"}})
            or not isinstance(held["payer_user_id"], str) or not held["payer_user_id"].isdecimal()
            or type(held["cost_points"]) is not int or held["cost_points"] < 0
            or (held["spend_ledger_id"] is not None if held["cost_points"] == 0 else
                not isinstance(held["spend_ledger_id"], str) or not held["spend_ledger_id"].isdecimal())):
        return False
    return True


def valid_intent(value):
    """Accept only this journal version; the identifier never grants authority."""
    try:
        return bool(isinstance(value, dict) and set(value) == {
            "version", "state", "id", "proposed_run_id", "binding", "created_at"}
            and type(value["version"]) is int and value["version"] == 1
            and value["state"] == "pending_reconciliation"
            and isinstance(value["proposed_run_id"], str)
            and str(UUID(value["proposed_run_id"])) == value["proposed_run_id"]
            and _valid_binding(value["binding"]) and goals.stamp(value["created_at"])
            and value["id"] == goals.digest(value["binding"]))
    except (TypeError, ValueError, AttributeError, KeyError):
        return False


def _current_binding(op, context, config, *, user, connection):
    """Reload original article/payment authority without creating an intent."""
    from .website_first_article import article_run, _immutable_binding
    from .website_first_article_billing import for_generation
    from .website_goal_scope import transition_chain
    # for_generation locks the current parent and verifies the original
    # spend/refund/payer. It never calls PointsService.spend.
    parent, held, _, _ = for_generation(op.pk, context=context, user=user)
    if parent.pk != op.pk or parent.receipt.get("goal") != op.receipt.get("goal"):
        _deny()
    run = article_run(op, config)
    if run is None or run.status not in {"completed", "failed", "blocked", "awaiting_approval"}:
        _deny()
    request, result, goal = goals.mapping(run.run_request), goals.mapping(run.result), op.receipt["goal"]
    source, current = request.get("expected_source_sha"), goal.get("current_source_sha")
    revision = request.get("configuration_revision")
    if (not isinstance(source, str) or not goals.SHA.fullmatch(source)
            or not isinstance(current, str) or not goals.SHA.fullmatch(current)
            or source == current or current != connection.verified_sha
            or type(revision) is not int or revision < 1 or revision >= connection.configuration_version
            or any(type(request.get(key)) is not type(value) or request.get(key) != value
                for key, value in {"branch": connection.branch, "app_root": connection.app_root}.items())
            or any(type(result.get(key)) is not int or result[key] < 0 for key in ("generation", "state_version"))):
        _deny()
    old_scope = {**goal["scope"], "configuration_revision": revision}
    edges = connection.operations.filter(generation=connection.generation, action="goal-source-scope", state="completed",
        payload__to_revision__gt=revision, payload__to_revision__lte=connection.configuration_version).order_by("created_at", "pk")[:16]
    if transition_chain(old_scope, goal["scope"], [goals.mapping(row.payload) for row in edges], source_sha=current) is None:
        _deny()
    binding = {"goal": _immutable_binding(goal), "current_scope": deepcopy(goal["scope"]),
        "source_sha": current, "paid_root": {"run_id": run.run_id, "source_sha": source,
            "configuration_revision": revision, "operation_id": request.get("operation_id"),
            "operation_attempt": request.get("operation_attempt"),
            "generation": result["generation"], "state_version": result["state_version"]},
        "billing": {key: held[key] for key in ("client_request_id", "payer_user_id", "spend_ledger_id", "cost_points")}}
    return binding


def _same_paid_root(previous, current):
    """A later observation version is a fresh read, not another article identity."""
    return (isinstance(previous, dict) and isinstance(current, dict)
        and {key: value for key, value in previous.items() if key != "state_version"} ==
            {key: value for key, value in current.items() if key != "state_version"}
        and type(previous.get("state_version")) is int and type(current.get("state_version")) is int
        and current["state_version"] >= previous["state_version"])


def binding_matches(previous, current):
    """Match current read authority, preserving the original minimum observation."""
    return (isinstance(previous, dict) and isinstance(current, dict)
        and {key: value for key, value in previous.items() if key != "paid_root"} ==
            {key: value for key, value in current.items() if key != "paid_root"}
        and _same_paid_root(previous.get("paid_root"), current.get("paid_root")))


def reserve(op, context, config, *, user, now):
    """Persist one bounded source-keyed proposal identity, without external effects.

    Reuse the original paid root through canonical billing validation. Source-only
    revision edges come from backend rows, not a request or worker snapshot. The
    owning prepare lease must still win its CAS when the intent is saved.
    """
    from .website_article_recovery import enabled
    if not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(op):
        return None
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    if op.state != "running" or config.website_connection.pk != op.connection.pk:
        _deny()
    with authority_guard(contract_for(op.connection), action="read") as connection:
        binding = _current_binding(op, context, config, user=user, connection=connection)
        goal = op.receipt["goal"]
        previous, history = op.receipt.get(KEY), op.receipt.get(HISTORY, [])
        if not isinstance(history, list) or len(history) >= MAX_INTENTS or any(not valid_intent(item) for item in history):
            _deny()
        if (len({item["id"] for item in history}) != len(history)
                or len({item["proposed_run_id"] for item in history}) != len(history)
                or any(item["binding"][key] != binding[key] for item in history for key in ("goal", "billing"))
                or any(not _same_paid_root(item["binding"].get("paid_root"), binding["paid_root"]) for item in history)):
            _deny()
        if previous is not None:
            if not valid_intent(previous):
                _deny()
            if any(item["id"] == previous["id"] or item["proposed_run_id"] == previous["proposed_run_id"] for item in history):
                _deny()
            if binding_matches(previous["binding"], binding):
                return deepcopy(previous)
            # A changed original article/goal/payment must not mint a refresh.
            old = previous["binding"]
            if any(old.get(key) != binding[key] for key in ("goal", "billing")) or not _same_paid_root(old.get("paid_root"), binding["paid_root"]):
                _deny()
            history = [*history, previous]
            if len(history) >= MAX_INTENTS:
                _deny()
        reason = budget.exhaustion(goal, now=now, action="first-article-generation")
        if reason:
            from .website_goal_runner import stop_goal
            stop_goal(op, goal, reason=reason, now=now)
            return None
        intent = {"version": 1, "state": "pending_reconciliation", "id": goals.digest(binding),
            "proposed_run_id": str(uuid4()), "binding": binding, "created_at": now.isoformat()}
        if not valid_intent(intent):
            _deny()
        _save_fields(op, {KEY: intent, HISTORY: history}, remove=(
            "first_article_approval", "first_article_live_receipt", "article_recovery"))
        return deepcopy(intent)
