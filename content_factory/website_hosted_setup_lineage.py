"""Recover private verifier inputs from this goal's exact backend-owned merge.

This reference is neither approval nor source verification. It is used only by
the sealed hosted verifier, which reserves and checks a fresh execution child.
"""
import hashlib
import re

from .website_contract import WebsiteAuthorityError
from .website_failure_contract import canonical_bytes
from . import website_goal as goals

KEY = "hosted_merged_setup_source_v1"
FIELDS = frozenset({"version", "run_id", "prepare_operation_id", "goal_id", "setup_attempt",
    "setup_retry_epoch", "request_digest", "merge_digest", "consent_digest"})
MERGE_FIELDS = ("publish_merge_intent", "merge_response", "merge_status", "merged_at")


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pending():
    return WebsiteAuthorityError("verified_target_required",
        "The original owned setup inputs need reconciliation before hosted verification.", retryable=True)


def _claims_match(request, parent_id):
    claims = [request[key] for key in ("strategy_goal_id", "first_article_goal_id", "first_article_goal_operation_id") if key in request]
    if "goal_budget" in request:
        budget = request["goal_budget"]
        if type(budget) is not dict or "goal_id" not in budget:
            return False
        claims.append(budget["goal_id"])
    return all(type(value) is str and value == parent_id for value in claims)


def retain_post_merge_inputs(op, child):
    """Record original input ownership before rotating the prepare attempt.

Only backend dispatch identity supplies the parent link. A missing legacy link
stays unavailable; a source scan or caller-selected run cannot reconstruct it.
"""
    op.receipt.pop(KEY, None)
    request = goals.mapping(getattr(child, "run_request", None))
    goal = goals.mapping(op.receipt.get("goal"))
    parent_id = str(op.pk)
    attempt, epoch = op.payload.get("attempt", 1), op.payload.get("retry_epoch", "initial")
    if (type(attempt) is not int or attempt < 1 or type(epoch) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", epoch)
            or goal.get("goal_id") != parent_id or op.receipt.get("setup_run_id") != child.run_id
            or request.get("client_request_id") != f"prepare:{parent_id}:setup:{attempt}:{epoch}"
            or not _claims_match(request, parent_id)):
        return
    result = goals.mapping(getattr(child, "result", None))
    try:
        op.receipt[KEY] = {"version": 1, "run_id": child.run_id, "prepare_operation_id": parent_id,
            "goal_id": parent_id, "setup_attempt": attempt, "setup_retry_epoch": epoch,
            "request_digest": _digest(request),
            "consent_digest": _digest(goal.get("consent")),
            "merge_digest": _digest({key: result.get(key) for key in MERGE_FIELDS})}
    except (ValueError, TypeError, OverflowError):
        return


def discard_for_new_setup(op):
    """Retire this input reference when a fresh setup is actually selected."""
    op.receipt.pop(KEY, None)


def source_reference(op):
    """Return only a current setup or the explicitly retained merged reference."""
    current = op.receipt.get("setup_run_id")
    if KEY not in op.receipt:
        return current
    saved = op.receipt[KEY]
    if (type(saved) is not dict or set(saved) != FIELDS or type(saved.get("version")) is not int
            or saved["version"] != 1 or saved.get("prepare_operation_id") != str(op.pk)
            or saved.get("goal_id") != str(op.pk)
            or type(saved.get("run_id")) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", saved["run_id"])
            or op.receipt.get("merged_setup_run_id") != saved["run_id"]
            or current is not None and current != saved["run_id"]):
        raise _pending()
    return saved["run_id"]


def validate_recovered_source(op, connection, run, *, source):
    """Repeat exact goal, dispatch, merge and current-source checks under lock."""
    if KEY not in op.receipt:
        return
    from .website_connections import observed_source_sha
    saved = op.receipt[KEY]
    source_reference(op)
    request, result = goals.mapping(run.run_request), goals.mapping(run.result)
    intent, response = goals.mapping(result.get("publish_merge_intent")), goals.mapping(result.get("merge_response"))
    attempt, epoch = saved.get("setup_attempt"), saved.get("setup_retry_epoch")
    current_attempt, current_epoch = op.payload.get("attempt", 1), op.payload.get("retry_epoch", "initial")
    expected_intent = {"website_connection_id": str(connection.pk), "connection_generation": connection.generation,
        "repository_id": connection.repository_id, "run_id": run.run_id, "base_branch": connection.branch,
        "source_sha": request.get("expected_source_sha")}
    try:
        unchanged = (_digest(request) == saved.get("request_digest")
            and _digest({key: result.get(key) for key in MERGE_FIELDS}) == saved.get("merge_digest")
            and _digest(goals.mapping(op.receipt.get("goal")).get("consent")) == saved.get("consent_digest")
            and all(canonical_bytes(intent.get(key)) == canonical_bytes(value) for key, value in expected_intent.items()))
    except (ValueError, TypeError, OverflowError):
        raise _pending() from None
    if (type(attempt) is not int or attempt < 1 or type(current_attempt) is not int or current_attempt <= attempt
            or type(epoch) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", epoch)
            or type(current_epoch) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", current_epoch) or current_epoch == epoch
            or request.get("client_request_id") != f"prepare:{op.pk}:setup:{attempt}:{epoch}"
            or not _claims_match(request, str(op.pk)) or run.status != "completed"
            or run.run_id != saved["run_id"] or result.get("merge_status") != "merged"
            or goals.stamp(result.get("merged_at")) is None or response.get("merged") is not True
            or type(source) is not str or not goals.SHA.fullmatch(source)
            or response.get("sha") != source or observed_source_sha(connection) != source
            or type(intent.get("head_sha")) is not str or not goals.SHA.fullmatch(intent["head_sha"])
            or op.payload.get("repair_setup") or goals.mapping(op.receipt.get("goal")).get("strategy_requires_candidate")
            or not unchanged):
        raise _pending()
