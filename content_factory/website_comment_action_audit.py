"""Partial comment intent observation after the existing mutation transaction."""
from functools import wraps
import hashlib
import json
import logging
import re
from uuid import uuid4

from django.db import transaction

from . import website_goal as goals, website_review_action_audit as review_audit

logger = logging.getLogger(__name__)
_PENDING = "_website_comment_audit_capture"
_METHODS = {"POST", "PATCH", "DELETE"}
_ACTIONS = {"create", "edit", "delete", "resolve", "reopen"}
_SCOPE_FIELDS = ("website_connection_id", "connection_generation", "repository_id", "github_repo",
    "app_root", "branch", "domain", "expected_source_sha", "first_article_goal_operation_id",
    *review_audit.GOAL_ALIASES)


def scope_digest(run):
    """Freeze only the saved attribution fields, without copying article content."""
    saved = goals.mapping(run.run_request)
    budget = goals.mapping(saved.get("goal_budget"))
    return goals.digest({"run_id": run.run_id, "organization_id": run.organization_id,
        "github_repo": run.github_repo, "domain": run.domain,
        "request": {key: saved[key] for key in _SCOPE_FIELDS if key in saved},
        "budget_present": "goal_budget" in saved,
        "goal_budget": {key: budget[key] for key in ("version", "goal_id", "scope_digest") if key in budget}})


def capture(request, context, run, *, action, payload, comment_id=None, operation_id=None):
    """Stage a bounded digest at the validated route boundary, before mutation."""
    try:
        if (not review_audit.eligible(request, context, methods=_METHODS) or action not in _ACTIONS
                or not isinstance(payload, dict)):
            return
        if operation_id is not None and (action != "create" or not isinstance(operation_id, str)
                or not re.fullmatch(r"[\w-]{8,100}", operation_id)):
            return
        value = {"action": action, "comment_id": str(comment_id or ""), "payload": payload}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > review_audit.MAX_INTENT_BYTES:
            logger.info("articles_comment_action_audit_unavailable reason=intent_size")
            return
        observed = {"action": "comment:" + action,
            "operation": goals.digest({"comment_operation": operation_id or str(uuid4())}),
            "fingerprint": hashlib.sha256(encoded).hexdigest()}
        # References to already-owned request/context only; the staged payload
        # contains run identifiers and hashes, never a second copy of comment text.
        setattr(request._request, _PENDING, (request, context, run.pk, run.run_id, scope_digest(run), observed))
    except Exception:
        logger.warning("articles_comment_action_audit_capture_unavailable")


def after_transaction(view):
    """Preserve the view result/exception and observe after its atomic dispatch."""
    @wraps(view)
    def wrapped(raw_request, *args, **kwargs):
        raw_request.__dict__.pop(_PENDING, None)
        try:
            return view(raw_request, *args, **kwargs)
        finally:
            pending = raw_request.__dict__.pop(_PENDING, None)
            if pending is not None:
                try:
                    # An unexpected outer transaction could roll back this audit
                    # too; do not claim durable observation or invert its locks.
                    if transaction.get_connection().in_atomic_block:
                        logger.info("articles_comment_action_audit_unavailable reason=outer_transaction")
                    else:
                        from workflow_runs.models import ContentFactoryRun
                        request, context, pk, run_id, frozen_scope, observed = pending
                        run = ContentFactoryRun.objects.filter(pk=pk, run_id=run_id,
                            organization_id=context.organization.pk).first()
                        if run is None or scope_digest(run) != frozen_scope:
                            logger.info("articles_comment_action_audit_unavailable reason=run_changed")
                        else:
                            review_audit.record_intent(request, context, run, observed,
                                kind="comment_http_intent", methods=_METHODS)
                except Exception:
                    logger.warning("articles_comment_action_audit_observation_unavailable")
    return wrapped
