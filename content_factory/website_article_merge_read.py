"""Original paid authority for current manual-merge observation, never recovery."""
from contextlib import contextmanager
from copy import deepcopy

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article merge observation changed.")


def binding_for(op, child):
    """Retain the child's original role instead of borrowing a current contract."""
    request = goals.mapping(child.run_request)
    fields = ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt",
        "deletion_epoch", "expected_source_sha", "configuration_revision", "branch", "app_root", "connection_target_id")
    if any(request.get(key) in (None, "") for key in fields[:7]) or "deletion_epoch" not in request:
        _deny()
    # Zero is an explicit valid deletion watermark, unlike missing identity.
    if (request.get("first_article_goal_operation_id") != str(op.pk)
            or request.get("first_article_goal_id") != str(op.pk)):
        _deny()
    return {**{key: deepcopy(request[key]) for key in fields if key in request},
        "first_article_goal_operation_id": str(op.pk), "first_article_goal_id": str(op.pk),
        "domain": child.domain, "github_repo": child.github_repo, "run_id": child.run_id}


@contextmanager
def current_observation(op, child, intent, *, expected=None):
    """Lock and recheck original owner/payment/approval/child before or after HTTP."""
    from .website_connections import authority_guard
    from .website_prepare import _prepare_context
    from .website_first_article_billing import for_generation
    from .website_first_article import active, article_run, approval_matches, _save_fields, _immutable_binding
    from .website_article_source_fence import KEY as SOURCE_FENCE_KEY
    from workflow_runs.models import ContentFactoryRun
    binding = binding_for(op, child)
    with authority_guard(binding, action="read", stored_source_only=True) as connection:
        context, config, user = _prepare_context(op)
        parent, billing, _, _ = for_generation(op.pk, context=context, user=user)
        if (not active(parent) or SOURCE_FENCE_KEY in parent.receipt or str(parent.pk) != str(op.pk)
                or str(connection.pk) != str(parent.connection_id)):
            _deny()
        source = article_run(parent, config)
        saved = ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
        original = goals.mapping(getattr(saved, "run_request", None))
        if (source is None or not approval_matches(parent, source) or saved.status != "completed"
                or saved.run_id != child.run_id or saved.organization_id != connection.organization_id
                or goals.digest(original) != goals.digest(child.run_request)
                or original.get("source_run_id") != source.run_id
                or (goals.mapping(source.result).get("publish_child_run_id") != saved.run_id
                    and parent.receipt.get("first_article_publish_run_id") != saved.run_id)
                or goals.mapping(saved.result).get("merge_status") != "manual_merge_required"
                or goals.mapping(saved.result).get("publish_merge_intent") != intent
                or original.get("expected_source_sha") != parent.receipt["goal"].get("current_source_sha")
                or intent.get("source_sha") != original.get("expected_source_sha")
                or intent.get("website_connection_id") != str(connection.pk)
                or intent.get("connection_generation") != connection.generation
                or intent.get("repository_id") != connection.repository_id or intent.get("run_id") != saved.run_id
                or intent.get("base_branch") != connection.branch
                or not isinstance(intent.get("head_sha"), str) or not goals.SHA.fullmatch(intent["head_sha"])
                or not isinstance(intent.get("head_branch"), str) or not 0 < len(intent["head_branch"]) <= 255):
            _deny()
        snapshot = {"goal": _immutable_binding(parent.receipt["goal"]), "revision": parent.receipt["goal"]["revision"],
            "billing": {key: billing[key] for key in ("client_request_id", "payer_user_id", "spend_ledger_id", "cost_points")},
            "source_run_id": source.run_id, "approval": deepcopy(parent.receipt.get("first_article_approval")),
            "child_request_digest": goals.digest(original), "intent": deepcopy(intent)}
        if expected is not None and snapshot != expected:
            _deny()
        _save_fields(op, {})  # Original prepare lease/revision CAS, including after HTTP.
        yield binding, saved, snapshot
