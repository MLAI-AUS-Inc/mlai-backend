"""Purpose-bound worker stop evidence; refunds still use backend-owned spends.

These receipts attest local worker quiescence, never repository authority or an
amount to credit. Unknown external effects remain visible after cancellation.
"""
from hashlib import sha256
from urllib.parse import urlparse

from .run_state import ARTICLE_WORKFLOWS
from .website_failure_contract import canonical_bytes

SCOPE_FIELDS = ("website_connection_id", "connection_generation", "connection_target_id", "repository_id",
    "github_repo", "expected_source_sha", "operation_id", "operation_attempt", "deletion_epoch",
    "client_request_id", "branch", "app_root")
URL_FIELDS = ("pr_url", "pull_request_url", "draft_pr_url", "draftPrUrl", "pullRequestUrl", "publish_url", "live_url")
NUMBER_FIELDS = ("pr_number", "pull_request_number", "draft_pr_number")
BRANCH_FIELDS = ("publish_branch", "draft_branch", "pr_branch", "branch_name")
HANDOFF_FIELDS = ("publish_handoff_pending", "publish_handoff_status", "publish_child_run_id", "promoted_publish_job_id")
# Terminal states whose paid obligation settles through a bound worker stop.
# A denied run is an authority refusal and settles like a failure.
TERMINAL_REFUND_STATUSES = frozenset({"failed", "cancelled", "denied"})
# Backend-owned settlement state. Worker snapshots and status polls can neither
# introduce nor erase it; only the backend's own settlement paths write it.
SETTLEMENT_RESULT_KEYS = frozenset({"article_terminal_refund", "reconciliation_refund_pending",
    "article_settlement_attempts", "article_settlement_status", "article_settlement_owner",
    "article_stop_attempts", "dispatch_rejection_v1"})
# Content Factory's code for a run whose first snapshot the backend refused.
DISPATCH_REJECTED = "dispatch_rejected"
# Refund states that own the original payment. A quarantined obligation does not:
# its stop may never seal, and a later credit rechecks restart reuse itself.
REFUND_OWNED_STATUSES = frozenset({"pending", "stop_required", "refunded"})


def mapping(value):
    return value if isinstance(value, dict) else {}


def original_scope(run):
    """Preserve missing optional keys, including a missing versus empty app root."""
    request = mapping(run.run_request)
    return {key: request[key] for key in SCOPE_FIELDS if key in request}


def comparable_scope(scope):
    """Compare unset scope as unset; an explicit empty app root or branch stays distinct.

    Django saves a content-only draft without connection keys and with
    github_repo "". Content Factory's request model records each as null, so a
    byte-for-byte compare refused every content-only stop receipt. Content
    Factory applies the same rule to the backend's expected cancellation.
    """
    if not isinstance(scope, dict):
        return scope
    return {key: value for key, value in scope.items() if value is not None and (key != "github_repo" or value != "")}


def original_goal(run):
    request = mapping(run.run_request)
    values = [value for value in (request.get("first_article_goal_id"), request.get("strategy_goal_id"),
        mapping(request.get("goal_budget")).get("goal_id")) if value not in (None, "")]
    if any(not isinstance(value, str) or value != values[0] for value in values):
        raise ValueError("Conflicting original goal identities")
    return values[0] if values else None


def _bound(run, receipt):
    return (type(receipt.get("version")) is int and receipt["version"] == 1 and receipt.get("run_id") == run.run_id
        and canonical_bytes(comparable_scope(receipt.get("scope"))) == canonical_bytes(comparable_scope(original_scope(run)))
        and "goal_id" in receipt and receipt["goal_id"] == original_goal(run))


def _surfaces(result):
    return (result, mapping(result.get("result")), mapping(result.get("latest_control_response")))


def publication_state(run, *, result=None):
    """Classify effects conservatively; a bound setup receipt is not an article."""
    result = mapping(run.result if result is None else result)
    setup_urls, setup_numbers, setup_branches = set(), set(), set()
    seen_setup = False
    for surface in _surfaces(result):
        # The source-to-publisher POST can already have created a different run
        # before its reply/local child arrives. Stopping this source alone says
        # nothing about that child; only parent settlement resolves the graph.
        if any(surface.get(key) for key in HANDOFF_FIELDS):
            return "unknown"
        raw = surface.get("publication_evidence_v1")
        if raw is None:
            continue
        receipts = raw if isinstance(raw, list) else [raw]
        for receipt in receipts:
            receipt = mapping(receipt)
            try:
                valid = _bound(run, receipt)
            except (ValueError, TypeError):
                valid = False
            if not valid or receipt.get("purpose") not in {"article", "setup"}:
                return "unknown"
            if receipt["purpose"] == "article":
                return "article" if receipt.get("state") == "observed" else "unknown"
            # Pending setup work can be stopped, but cannot explain an observed PR.
            if receipt.get("state") != "observed" or receipt.get("kind") not in {"pr", "branch"}:
                return "unknown"
            seen_setup = True
            setup_urls.update(str(receipt[key]) for key in URL_FIELDS if receipt.get(key))
            setup_numbers.update(str(receipt[key]) for key in NUMBER_FIELDS if receipt.get(key))
            setup_branches.update(str(receipt[key]) for key in BRANCH_FIELDS if receipt.get(key))
    for surface in _surfaces(result):
        if any(str(surface[key]) not in setup_urls for key in URL_FIELDS if surface.get(key)):
            return "unknown"
        if any(str(surface[key]) not in setup_numbers for key in NUMBER_FIELDS if surface.get(key)):
            return "unknown"
        if any(str(surface[key]) not in setup_branches for key in BRANCH_FIELDS if surface.get(key)):
            return "unknown"
    return "setup_only" if seen_setup else "none"


def useful_delivery(run, *, result=None):
    """A completed package or article review is value; unreviewed source is not."""
    result = mapping(run.result if result is None else result)
    acceptance = mapping(getattr(run, "acceptance_summary", None))
    request = mapping(run.run_request)
    portable = request.get("delivery_mode") == "content_only" and request.get("delivery_mode_confirmed") is True
    if portable and acceptance.get("content_packaged"):
        return True
    for surface in _surfaces(result):
        for key in ("content_package", "contentPackage", "delivery_package", "deliveryPackage"):
            package = mapping(surface.get(key))
            if portable and (package.get("content_packaged") or package.get("contentPackaged")):
                return True
        if surface.get("preview_content_verified") is True and (surface.get("preview_url") or surface.get("review_url")):
            return True
    return False


def cancellation_receipt(run, payload):
    """Validate a stop receipt against saved identity and monotonic worker state."""
    payload = mapping(payload)
    receipt = payload.get("article_cancellation_v1")
    if receipt is None:
        receipt = mapping(payload.get("result")).get("article_cancellation_v1")
    if receipt is None:
        return None
    if run.workflow not in ARTICLE_WORKFLOWS or not isinstance(receipt, dict):
        raise ValueError("Invalid article cancellation receipt")
    allowed = {"version", "purpose", "run_id", "goal_id", "scope", "generation", "state_version", "outcome",
        "cancelled", "delivered_value", "watermark", "receipt_id"}
    if set(receipt) != allowed:
        raise ValueError("Invalid cancellation receipt fields")
    if not _bound(run, receipt) or receipt.get("purpose") != "article_cancellation" or receipt.get("cancelled") is not True:
        raise ValueError("Article cancellation scope mismatch")
    if any(type(receipt.get(key)) is not int or receipt[key] < 0 for key in ("generation", "state_version")):
        raise ValueError("Invalid cancellation execution version")
    result = mapping(run.result)
    known_generation, known_version = result.get("generation"), result.get("state_version")
    if type(known_generation) is int and receipt["generation"] != known_generation:
        raise ValueError("Cancellation generation mismatch")
    if type(known_version) is int and receipt["state_version"] < known_version:
        raise ValueError("Stale cancellation receipt")
    scope = original_scope(run)
    if canonical_bytes(receipt.get("watermark")) != canonical_bytes({"operation_id": scope.get("operation_id"),
            "attempt": scope.get("operation_attempt"), "cancelled": True}):
        raise ValueError("Cancellation watermark mismatch")
    if receipt.get("outcome") not in {"no_article_effect", "article_effect", "unknown"}:
        raise ValueError("Invalid publication outcome")
    if receipt.get("delivered_value") is not None and type(receipt["delivered_value"]) is not bool:
        raise ValueError("Invalid delivery outcome")
    body = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if sha256(canonical_bytes(body)).hexdigest() != receipt.get("receipt_id"):
        raise ValueError("Cancellation receipt digest mismatch")
    previous = mapping(result.get("article_cancellation_v1"))
    if (previous and (previous.get("generation"), previous.get("state_version")) ==
            (receipt["generation"], receipt["state_version"]) and previous.get("receipt_id") != receipt["receipt_id"]):
        raise ValueError("Conflicting cancellation receipt at the same version")
    return dict(receipt)



def _checked_publication(run, value):
    """Keep only bounded identifiers; a cancellation cannot smuggle provider data."""
    if isinstance(value, list):
        if not value or len(value) > 16:
            raise ValueError("Invalid publication evidence count")
        return [_checked_publication(run, item) for item in value]
    allowed = {"version", "purpose", "run_id", "goal_id", "scope", "generation", "kind", "state",
        "pr_url", "pr_number", "branch_name", "head_sha", "setup_run_id", "parent_run_id"}
    if not isinstance(value, dict) or set(value) - allowed or not _bound(run, value):
        raise ValueError("Invalid publication receipt fields or scope")
    if value.get("purpose") not in {"article", "setup"} or value.get("state") not in {"pending", "observed", "unknown"}:
        raise ValueError("Invalid publication purpose or state")
    if value.get("kind") not in {"pr", "branch", "unknown"}:
        raise ValueError("Invalid publication kind")
    for key in ("run_id", "goal_id", "branch_name", "head_sha", "setup_run_id", "parent_run_id"):
        if value.get(key) is not None and (not isinstance(value[key], str) or len(value[key]) > 255
                or any(ord(char) < 32 for char in value[key])):
            raise ValueError("Invalid publication identifier")
    if "generation" in value and (type(value["generation"]) is not int or value["generation"] < 0):
        raise ValueError("Invalid publication generation")
    if "pr_number" in value and (type(value["pr_number"]) is not int or value["pr_number"] <= 0):
        raise ValueError("Invalid pull request number")
    if value.get("pr_url"):
        url = value["pr_url"]
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("Invalid pull request URL")
        parsed = urlparse(url)
        prefix = "/" + str(mapping(run.run_request).get("github_repo") or run.github_repo) + "/pull/"
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
                or not parsed.path.startswith(prefix) or not parsed.path[len(prefix):].isdigit()):
            raise ValueError("Invalid pull request URL")
    return dict(value)

def cancellation_result(run, payload):
    """Merge a validated stop without erasing proof, delivery or an owed credit."""
    result = dict(mapping(run.result))
    receipt = cancellation_receipt(run, payload)
    if receipt is None:
        return result
    incoming = mapping(payload)
    nested = mapping(incoming.get("result"))
    publication = incoming.get("publication_evidence_v1", nested.get("publication_evidence_v1"))
    if publication is not None:
        publication = _checked_publication(run, publication)
        # Never replace previously recorded article/unknown effects with setup.
        proposed = {**result, "publication_evidence_v1": publication}
        saved_effect = any(surface.get("publication_evidence_v1") is not None for surface in _surfaces(result))
        if (not saved_effect or publication_state(run) in {"none", "setup_only"}
                or publication_state(run, result=proposed) in {"article", "unknown"}):
            result = proposed
    effects = publication_state(run, result=result)
    no_value = (receipt["outcome"] == "no_article_effect" and receipt.get("delivered_value") is False
        and effects in {"none", "setup_only"} and not useful_delivery(run, result=result))
    result.update(article_cancellation_v1=receipt, generation=receipt["generation"], state_version=receipt["state_version"],
        reconciliation_required=receipt["outcome"] != "no_article_effect" or effects in {"article", "unknown"})
    previous = mapping(result.get("article_terminal_refund"))
    if no_value and mapping(run.run_request).get("first_article_goal_operation_id"):
        # The goal owns this shared spend across every repair child. A child
        # cannot queue a credit that fires after its parent successfully finishes.
        result["article_settlement_owner"] = "first_article_goal"
        return result
    if no_value and previous.get("status") not in {"refunded", "not_required"}:
        from .billing_lineage import paid_article_request
        request = mapping(run.run_request)
        result["article_terminal_refund"] = {**previous, "status": "pending", "job_id": run.run_id,
            "paid_obligation": paid_article_request(mapping(run.run_request)) or previous.get("paid_obligation") is True,
            "billing_resolution_required": bool(request.get("client_request_id"))
                and request.get("roo_points_billing_status") not in {"free", "gated", "refunded"},
            "settlement_reason": "cancelled_without_delivery", "cancellation_receipt_id": receipt["receipt_id"]}
        result["reconciliation_refund_pending"] = True
    return result


def exhausted_article_candidate(run):
    """Stop an exhausted blocked run or an existing terminal refund obligation."""
    if run.workflow not in ARTICLE_WORKFLOWS or run.status not in {"blocked", *TERMINAL_REFUND_STATUSES}:
        return False
    if mapping(run.run_request).get("first_article_goal_operation_id"):
        return False  # Its goal's repair budget and terminal refund own the decision.
    result = mapping(run.result)
    if run.status in TERMINAL_REFUND_STATUSES:
        return (result.get("reconciliation_refund_pending") is True
            and mapping(result.get("article_terminal_refund")).get("status") == "stop_required")
    failure = mapping(result.get("failure"))
    if (result.get("requires_user_action") or failure.get("requires_user_action")
            or (failure.get("failure_class") or result.get("failure_class")) in {"waiting", "needs_user"}
            or getattr(run, "current_step", None) in {"await_review", "await_publish_approval", "await_merge", "await_deploy"}):
        return False
    code = failure.get("code") or result.get("error_code")
    return (code in {"RUN_BUDGET_EXHAUSTED", "ONBOARDING_MODEL_BUDGET_EXHAUSTED"}
        or mapping(result.get("budget")).get("exhausted") is True)


def denied_refund_code(run):
    """Name the no-value refusal that makes a paid denied article run owe its charge back.

    An authority refusal can end a run as denied while the worker's terminal
    callback is superseded, so no failure callback reserves the credit. Recorded
    effects or delivery keep the charge; the credit itself still needs a stop.
    """
    from .billing_lineage import paid_article_request
    from .incident_guards import NO_DELIVERY_REFUND_CODES, delivered_content
    request = mapping(run.run_request)
    if (run.workflow not in ARTICLE_WORKFLOWS or run.status != "denied" or not paid_article_request(request)
            or request.get("first_article_goal_operation_id")):
        return None
    result = mapping(run.result)
    code = str(mapping(result.get("failure")).get("code") or result.get("error_code")
        or mapping(result.get("authority_denied")).get("code") or "").strip().upper()
    if code not in NO_DELIVERY_REFUND_CODES or publication_state(run) in {"article", "unknown"}:
        return None
    return None if useful_delivery(run) or delivered_content(run) else code


def dispatch_rejected_refund_candidate(run):
    """A paid article run refused at its first snapshot, before any work was queued.

    Content Factory fails such a run locally without a terminal callback, and
    its stop endpoint cannot bind the backend's scope (the refused snapshot was
    never merged), so neither the callback nor the stop path can settle it.
    A restart that reused the charge, a goal-owned spend, or any recorded
    effect or delivery keeps the charge.
    """
    from .billing_lineage import paid_article_request
    from .incident_guards import delivered_content
    request, result = mapping(run.run_request), mapping(run.result)
    code = str(mapping(result.get("failure")).get("code") or result.get("error_code") or "").strip().lower()
    restart = mapping(result.get("restart_receipt"))
    if (run.workflow not in ARTICLE_WORKFLOWS or run.status != "failed" or code != DISPATCH_REJECTED
            or not paid_article_request(request) or not str(request.get("client_request_id") or "").strip()
            or str(request.get("client_request_id")).strip() == str(run.run_id or "").strip()
            or request.get("first_article_goal_operation_id") or request.get("pending_billing_refund")
            or result.get("dispatch_refund_processed") or request.get("roo_points_billing_status") == "refunded"
            or result.get("restart_child_run_id") or restart.get("state") in {"pending", "running", "completed"}):
        return False
    return publication_state(run) == "none" and not useful_delivery(run) and not delivered_content(run)


def dispatch_rejection_receipt(run):
    """Content Factory's own key record that the backend refused this exact run."""
    request, result = mapping(run.run_request), mapping(run.result)
    receipt = mapping(result.get("dispatch_rejection_v1"))
    if (set(receipt) != {"version", "run_id", "client_request_id", "key_status", "backend_code", "checked_at"}
            or type(receipt.get("version")) is not int or receipt["version"] != 1
            or receipt.get("run_id") != run.run_id or receipt.get("key_status") != "failed"
            or receipt.get("client_request_id") != str(request.get("client_request_id") or "").strip()
            or not isinstance(receipt.get("backend_code"), str) or not receipt["backend_code"]):
        return None
    return dict(receipt) if dispatch_rejected_refund_candidate(run) else None


def restart_reused_charge(run):
    """A replacement draft reserved or took this run's payment; crediting it too would pay twice."""
    result = mapping(run.result)
    return bool(result.get("restart_child_run_id")) or mapping(result.get("restart_receipt")).get("state") in {
        "pending", "running", "completed"}


def refund_owns_charge(run):
    """A recorded refund obligation owns this payment until it settles; a restart cannot reuse it."""
    return mapping(mapping(run.result).get("article_terminal_refund")).get("status") in REFUND_OWNED_STATUSES


def unrecoverable_refund_code(run):
    """Name the no-recovery failure that makes a paid failed article run owe its charge back.

    Content Factory marks every non-retryable failure as needing user action.
    When the code's registry contract is a stop with no recovery action, and
    the run has no resume, retry or scheduled recovery, nothing continues this
    run (PawShare run 9f479633, CORPUS_INCOMPLETE at plan_article, 29 Oct 2026).
    No callback reserves that credit. Recorded effects, delivery or a restart
    that reused the charge keep it; the credit itself still needs a stop.
    """
    from .billing_lineage import paid_article_request
    from .incident_guards import delivered_content
    from .website_failure_contract import load_registry
    request, result = mapping(run.run_request), mapping(run.result)
    failure = mapping(result.get("failure"))
    if (run.workflow not in ARTICLE_WORKFLOWS or run.status != "failed" or run.resume_available
            or not paid_article_request(request) or request.get("first_article_goal_operation_id")
            or request.get("pending_billing_refund") or request.get("roo_points_billing_status") == "refunded"
            or restart_reused_charge(run)):
        return None
    if (failure.get("requires_user_action", result.get("requires_user_action")) is not True
            or failure.get("retryable", result.get("retryable")) is not False or result.get("retry_available") is True
            or mapping(result.get("recovery")).get("state") == "pending"
            or any(mapping(intent).get("state") == "pending" for intent in mapping(result.get("recovery_intents")).values())):
        return None
    code = str(failure.get("code") or result.get("error_code") or "").strip()
    entry = load_registry()["by_code"].get(code) or {}
    if entry.get("retry_policy") != "stop_contract" or entry.get("action_id") is not None:
        return None
    if publication_state(run) in {"article", "unknown"} or useful_delivery(run) or delivered_content(run):
        return None
    return code


def refund_reconciliation_reason(run):
    """Recheck current proof immediately before credit; absence requires a stop."""
    if getattr(run, "workflow", None) not in ARTICLE_WORKFLOWS:
        return None
    if (run.status not in TERMINAL_REFUND_STATUSES or publication_state(run) in {"article", "unknown"} or useful_delivery(run)
            or restart_reused_charge(run)):
        return "needs_reconciliation"
    try:
        receipt = cancellation_receipt(run, run.result)
    except (ValueError, TypeError):
        return "needs_reconciliation"
    if receipt:
        return None if receipt["outcome"] == "no_article_effect" and receipt.get("delivered_value") is False else "needs_reconciliation"
    if dispatch_rejection_receipt(run):
        # The worker's key record proves this run never reached its queue.
        return None
    return "stop_required"
