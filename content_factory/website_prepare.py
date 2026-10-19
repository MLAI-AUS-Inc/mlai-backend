"""Persisted, bounded orchestration of the existing website readiness journey."""
import logging
import re
from uuid import uuid4
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .website_connections import authority_guard, contract_for
from .website_contract import WebsiteAuthorityError, evidence_digest, connection_contract
from .website_journey import journey_for_context
from .website_models import WebsiteConnectionOperation
from .website_failure_classes import failure_class

logger = logging.getLogger(__name__)

RETRY_MINUTES = (1, 2, 5, 10, 20, 40, 60)
DEPLOYMENT_RETRY_MINUTES = (2, 5, 10, 20, 40, 60, 120)
PERMISSION_CODES = frozenset({"github_workflow_permission_required", "github_ci_evidence_permission_required"})
PERMISSION_STEPS = {
    "github_workflow_permission_required": frozenset({"permissions", "setup"}),
    "github_ci_evidence_permission_required": frozenset({"permissions", "verify", "ci-attestation"}),
}

AUTHORIZATION_CODES = frozenset({"github_reconnect_required", "github_authorization_required",
    "github_repository_unavailable", "github_workflow_permission_required", "github_ci_evidence_permission_required",
    "website_connection_required", "github_access_required", "github_verification_required"})
DEPLOYMENT_WAIT_CODES = frozenset({"deployment_source_unverified", "deployment_marker_unverified",
    "live_route_unavailable", "ci_attestation_required", "ci_evidence_unavailable", "verified_target_required",
    "deployment_verification_required", "native_ci_attestation_missing", "ci_proof_unavailable"})
SETUP_EXAMPLE_CLEANUP_CODE = "SETUP_EXAMPLE_CLEANUP_REQUIRED"
# Content Factory reported an authorization check it could not complete (a
# timeout while this backend restarted, for example) as this code until it
# split out CONNECTION_SERVICE_TRANSIENT. Such a run is resumed this many times.
UNVERIFIED_AUTHORIZATION_CODE = "CONNECTION_AUTHORIZATION_FAILED"
UNVERIFIED_AUTHORIZATION_RESUMES = 3


def _setup_example_cleanup_action(raw, connection):
    """Accept only CF's exact, portable file/record cleanup instruction."""
    from .website_contract import safe_repository_path
    if not isinstance(raw, dict) or raw.get("id") != "remove_setup_example":
        return None
    path, record = raw.get("file"), raw.get("record")
    if (not isinstance(path, str) or len(path) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not isinstance(record, str) or len(record) > 255
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?", record)):
        return None
    try:
        if safe_repository_path(path) != path:
            return None
    except WebsiteAuthorityError:
        return None
    action = {"id": "remove_setup_example", "label": "Review example cleanup", "file": path, "record": record}
    repository, branch = str(connection.github_repo or ""), str(connection.branch or "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) and branch and not any(ord(char) < 32 or ord(char) == 127 for char in branch):
        action["url"] = f"https://github.com/{repository}/blob/{quote(branch, safe='')}/{quote(path, safe='/')}"
    return action


def _retain_setup_example_cleanup(op, child):
    """Keep a terminal, current-operation cleanup child waiting for its owner."""
    from .website_operations import validate_operation
    connection = op.connection
    if child.workflow != "article_system_setup" or child.status not in {"blocked", "failed"}:
        return False
    result = child.result if isinstance(child.result, dict) else {}
    nested = result.get("article_system_setup")
    nested = nested if isinstance(nested, dict) else {}
    if not any(code == SETUP_EXAMPLE_CLEANUP_CODE for code in (result.get("error_code"), nested.get("error_code"))):
        return False
    if (child.run_id != op.receipt.get("child_run_id") or child.organization_id != connection.organization_id
            or str(child.domain or "").casefold().strip() != connection.organization.domain.casefold().strip()
            or str(child.github_repo or "").casefold() != connection.github_repo.casefold() or op.generation != connection.generation):
        return False
    action = next((valid for entry in (result.get("user_action"), result.get("requested_action"),
        nested.get("user_action"), nested.get("requested_action")) if (valid := _setup_example_cleanup_action(entry, connection))), None)
    if action is None:
        return False
    saved = child.run_request if isinstance(child.run_request, dict) else {}
    try:
        original, expected = connection_contract(saved), connection_contract(contract_for(connection))
        if any(original.get(key) != expected.get(key) for key in ("website_connection_id", "connection_generation", "repository_id")):
            return False
        if any(key not in saved for key in ("operation_id", "operation_attempt", "deletion_epoch")):
            return False
        operation = validate_operation(connection, {**saved, "run_id": child.run_id})
        if operation is None or operation.action != "workflow":
            return False
    except WebsiteAuthorityError:
        return False
    op.state, op.next_attempt_at = "needs_user", None
    op.receipt = {**op.receipt, "status": "needs_you", "current_step": "setup", "blocked_step": "setup",
        "reason_code": SETUP_EXAMPLE_CLEANUP_CODE, "retryable": False, "user_action": action,
        "message": f"Remove setup example record '{action['record']}' from {action['file']}. Then check again after cleaning it up."}
    return True


def _cleanup_retry_journey(op, context, config, journey):
    """Refresh source facts before dispatching the owner's new cleanup attempt."""
    if (op.payload.get("manual_setup_example_cleanup_retry") or op.payload.get("source_reverify")) and not op.receipt.get("scan_run_id"):
        from .vibe_marketing_views import _article_capabilities_for_context
        capabilities = _article_capabilities_for_context(context, config, force=True)
        return journey_for_context(context, config, capabilities=capabilities)
    return journey


SETUP_PR_RESULT_KEYS = ("pr_url", "pull_request_url", "draft_pr_url", "pr_number", "pull_request_number", "draft_pr_number")


def _superseded_setup_binding(op, child):
    """The replaced setup's own original identity, or None when its branch must stay.

    Only a blocked or failed setup of this connection generation that never
    recorded a pull request qualifies. Content Factory repeats these checks
    against its own ledger and GitHub before it removes anything.
    """
    from .website_operations import OPERATION_FIELDS, deletion_epoch
    connection = op.connection
    if child.workflow != "article_system_setup" or child.status not in {"blocked", "failed"}:
        return None
    if str(child.github_repo or "").casefold() != connection.github_repo.casefold():
        return None
    result = child.result if isinstance(child.result, dict) else {}
    nested = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    if any(payload.get(key) for payload in (result, nested) for key in SETUP_PR_RESULT_KEYS):
        return None
    saved = child.run_request if isinstance(child.run_request, dict) else {}
    try:
        original, expected = connection_contract(saved), connection_contract(contract_for(connection))
    except WebsiteAuthorityError:
        return None
    if (any(original.get(key) != expected.get(key) for key in ("website_connection_id", "connection_generation", "repository_id"))
            or any(saved.get(key) in (None, "") for key in OPERATION_FIELDS)
            or str(saved["deletion_epoch"]) != str(deletion_epoch(connection))):
        return None
    return {**original, **{key: saved[key] for key in OPERATION_FIELDS}, "run_id": child.run_id,
        "domain": connection.organization.domain, "github_repo": connection.github_repo}


def _queue_superseded_setup_cleanup(op, child):
    """Record the replaced setup's branch cleanup; the reconciler contacts the worker."""
    binding = _superseded_setup_binding(op, child)
    if binding is None:
        return None
    connection = op.connection
    followup, _ = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{connection.pk}:superseded-setup:{child.run_id}", defaults={
            "connection": connection, "generation": connection.generation, "action": "superseded-setup-cleanup",
            "payload": {"run_id": child.run_id, "binding": binding, "prepare_operation_id": str(op.pk),
                "superseded_attempt": op.payload.get("attempt", 1)},
            "receipt": {"status": "queued", "run_id": child.run_id, "repository_modified": None}})
    return followup


def _cancel_prepare_child(op, config):
    """Fence a stalled child through the ordinary durable cancellation control.

    A blocked or failed setup child has already stopped. The new attempt
    replaces it, so its own review branch is queued for cleanup instead.
    """
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import cancel_operation, deletion_epoch
    identifiers = list(dict.fromkeys(identifier for identifier in (op.receipt.get("hosted_verification_run_id"), op.receipt.get("child_run_id") or op.payload.get("run_id")) if identifier))
    for child_id in identifiers:
        child = ContentFactoryRun.objects.filter(run_id=child_id, organization_id=op.connection.organization_id).first()
        if child is not None and child.status in {"failed", "blocked"}:
            _queue_superseded_setup_cleanup(op, child)
        if child is None or child.status in {"completed", "failed", "blocked", "cancelled", "denied"}:
            continue
        saved = child.run_request if isinstance(child.run_request, dict) else {}
        cancel_operation(config, data={**contract_for(op.connection), "operation_id": saved.get("operation_id"),
            "operation_attempt": saved.get("operation_attempt", 1), "deletion_epoch": deletion_epoch(op.connection)},
            idempotency_key=f"prepare-retry:{op.pk}:{op.payload.get('attempt', 1)}:{child_id}")


def _checking_owner_wait(op):
    """A permission, review, merge or deploy recheck preserves its saved child."""
    if op.state != "needs_user" or op.receipt.get("failure_class") in {"repairable", "strategy", "unsupported"}:
        return False
    action = op.receipt.get("user_action") or {}
    action_id = action.get("id") if isinstance(action, dict) else None
    if action_id in {"merge_pr", "review_setup", "review_github_permissions", "authorize_github",
            "deploy_site", "choose_articles_page"}:
        return True
    # Older receipts can lack the action object. Do not let an explicit retry or
    # failed-run action inherit this compatibility treatment from its step name.
    return action_id is None and op.receipt.get("current_step") in {"merge", "permissions", "verify", "ci-attestation"}


def start_prepare(config, *, expected=None, company_id=None, user=None, retry=False, target="ready", force_successor=False, consent=None):
    """Reuse one prepare receipt for the current consent generation."""
    connection = config.website_connection
    if connection is None:
        raise WebsiteAuthorityError("website_connection_required", "Select and authorize your website repository first.")
    binding = {**(contract_for(connection) if expected is None else expected)}
    if getattr(connection, "organization", None):
        binding.update(domain=connection.organization.domain, github_repo=connection.github_repo)
    with authority_guard(binding, action="read") as current:
        from .website_goal import enrolled
        from django.conf import settings
        if target != "ready" and not (target == "first_article_live" and enrolled(current.organization_id)
            and getattr(settings, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", False)
            and getattr(settings, "ARTICLES_SETUP_CONSENT_ENABLED", False)):
            raise WebsiteAuthorityError("website_prepare_blocked", "Only the ready goal is available until first-article publication is enabled.", status=422)
        if enrolled(current.organization_id):
            from .website_goal_runner import start_goal
            return start_goal(current, company_id=company_id, user=user, retry=retry, target=target,
                now=timezone.now(), force_successor=force_successor, consent=consent)
        if hasattr(current, "operations"):
            from .website_goal_runner import owns_goal
            reserved = WebsiteConnectionOperation.objects.filter(connection_id=current.pk, generation=current.generation, action="prepare",
                receipt__has_key="goal").order_by("-created_at", "-pk").first()
            if reserved and owns_goal(reserved):
                raise WebsiteAuthorityError("website_prepare_blocked", "Goal execution is disabled; reserved work is retained instead of redispatched.")
        op, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"{current.pk}:prepare:{current.generation}", defaults={
                "connection": current, "generation": current.generation, "action": "prepare",
                "payload": {"company_id": str(company_id or ""), "requested_by_user_id": str(getattr(user, "pk", "") or ""), "attempt": 1, "user_clicks": 1},
                "receipt": {"status": "working", "current_step": "verify-access", "message": "Preparing your website for articles."}})
        if op.action != "prepare" or op.generation != current.generation:
            raise WebsiteAuthorityError("operation_key_conflict", "The saved preparation belongs to another website.")
        if not created and retry and op.state in {"needs_attention", "needs_user", "cancelled", "stopped"}:
            cleanup_retry = (op.state == "needs_user" and op.receipt.get("reason_code") == SETUP_EXAMPLE_CLEANUP_CODE
                and _setup_example_cleanup_action(op.receipt.get("user_action"), current) is not None)
            check_again = _checking_owner_wait(op)
            if not check_again:
                _cancel_prepare_child(op, config)
            op.state, op.next_attempt_at = "pending", None
            op.receipt = {**op.receipt, "step_failures": {}, "status": "working", "user_action": None, "last_completed": None,
                "child_run_id": op.receipt.get("child_run_id") if check_again else None,
                "reason_code": "", "blocked_step": None, "deployment_attempts": 0,
                "dispatch_outcome": "replayed" if check_again else "new_attempt"}
            for key in ("deployment_wait_started_at", "transient_started_at", "child_resume_pending", "failure_class"):
                op.receipt.pop(key, None)
            op.payload = {**op.payload, "attempt": op.payload.get("attempt", 1) + (0 if check_again else 1),
                "user_clicks": op.payload.get("user_clicks", 1) + 1}
            if not check_again:
                op.payload["retry_epoch"] = uuid4().hex
                op.payload.pop("run_id", None)
                op.payload["repair_setup"] = True
            if cleanup_retry:
                op.payload["manual_setup_example_cleanup_retry"] = True
                for key in ("scan_run_id", "setup_run_id", "completed_steps"):
                    op.receipt.pop(key, None)
            op.save(update_fields=["state", "next_attempt_at", "receipt", "payload", "updated_at"])
        elif not created:
            op.receipt = {**op.receipt, "dispatch_outcome": "replayed"}
            # A response replay must not invalidate a worker's updated_at lease.
            op.save(update_fields=["receipt"])
    return op


def _claim_prepare(identifier, now):
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
            Q(state__in=["pending", "running"]) | Q(state="needs_user", receipt__user_action__id__in=["deploy_site","merge_pr","review_draft"]),
            pk=getattr(identifier, "pk", identifier), action="prepare").first()
        if op is None or op.next_attempt_at and op.next_attempt_at > now:
            return None
        from .website_goal_runner import owns_goal
        from .website_goal import enrolled
        if owns_goal(op) and not enrolled(op.connection.organization_id):
            return None
        if owns_goal(op):
            from .website_owner_transport import require_admitted_goal
            require_admitted_goal(op)
        if op.generation != op.connection.generation or op.connection.state != "connected":
            op.state = "cancelled"
            op.receipt = {**op.receipt, "status": "authority_changed", "message": "Website access changed."}
            op.save(update_fields=["state", "receipt", "updated_at"])
            return None
        from .website_execution_observation import record
        if op.state == "running" and op.next_attempt_at is not None and op.next_attempt_at <= now:
            # The new lease revokes this old claim. Record that fence, rather
            # than inventing a return from the process whose lease expired.
            record(op, "superseded", now=now)
        op.state, op.attempts = "running", op.attempts + 1
        op.next_attempt_at = now + timedelta(minutes=5)
        record(op, "claim", now=now)
        op.save(update_fields=["state", "attempts", "next_attempt_at", "receipt", "updated_at"])
        op._claimed_at = op.updated_at
        if owns_goal(op):
            from .website_goal import mapping as goal_mapping
            op._claimed_goal_revision = goal_mapping(op.receipt.get("goal")).get("revision")
        return op


def _prepare_context(op):
    from django.contrib.auth import get_user_model
    from .vibe_marketing_views import get_founder_company_context, _get_config
    user = get_user_model().objects.get(pk=op.payload["requested_by_user_id"], is_active=True)
    context = get_founder_company_context(user, company_id=op.payload["company_id"])
    if context.organization.pk != op.connection.organization_id:
        raise WebsiteAuthorityError("website_connection_changed", "This website belongs to another company.")
    config = _get_config(context.organization)
    if config.website_connection_id != op.connection_id:
        raise WebsiteAuthorityError("website_connection_changed", "The selected website changed.")
    return context, config, user


def _persist_prepare(op, *, now):
    from .website_notification_outbox import enabled,track_transition,reserve_notice
    from .website_goal_runner import owns_goal
    from .website_first_article_billing import REFUND_TERMINAL
    from .website_first_article_refunds import reserve_refund
    from .website_cost_recovery import pending_for, lock_connection, reserve as reserve_usage_refresh
    from .website_operational_alerts import budget_alert, stall_alert
    from .website_operational_alert_delivery import reserve as reserve_operational_alert
    notify = enabled(op.connection) and owns_goal(op)
    refund = op.state in REFUND_TERMINAL and bool(op.receipt.get("first_article_billing"))
    accounting = bool(pending_for(op))
    incidents = [item for item in (budget_alert(op, now=now), stall_alert(op, now=now)) if item is not None]
    if notify or refund or accounting or incidents:
        with transaction.atomic():
            if (accounting or incidents) and not lock_connection(op):
                return None
            notice=track_transition(op,now=now) if notify else None
            saved=_persist_prepare_state(op,now=now)
            if saved is not None:
                if refund:reserve_refund(op,now=now)
                if notify:reserve_notice(op,notice,now=now)
                if accounting:reserve_usage_refresh(op,now=now)
                for incident in incidents:reserve_operational_alert(op,incident,now=now)
            return saved
    return _persist_prepare_state(op,now=now)


def _persist_prepare_state(op, *, now):
    from .website_goal_audit import enabled, preserve_during_prepare_write
    if enabled(op.connection.organization_id):
        return preserve_during_prepare_write(op, lambda: _persist_prepare_claim(op, now=now))
    return _persist_prepare_claim(op, now=now)


def _persist_prepare_claim(op, *, now):
    # Compare the lease and generation after remote work. A disconnect always wins.
    query = WebsiteConnectionOperation.objects.filter(pk=op.pk, action="prepare", state="running", attempts=op.attempts,
        updated_at=op._claimed_at, connection__generation=op.generation, connection__state="connected")
    from .website_goal_runner import owns_goal
    if owns_goal(op):
        query = query.filter(receipt__goal__revision=op._claimed_goal_revision,
            connection__configuration_version=op.connection.configuration_version)
    changed = query.update(
            state=op.state, receipt=op.receipt, payload=op.payload, next_attempt_at=op.next_attempt_at, updated_at=now)
    if owns_goal(op) and changed != 1:
        return None
    return op


def _finish_prepare(op, *, now):
    from .website_goal_runner import owns_goal, finish_goal
    if owns_goal(op):
        finish_goal(op, now=now)
        from .website_execution_observation import record
        record(op, "outcome", now=now)
    logger.info("website_prepare operation=%s company=%s connection=%s generation=%s step=%s state=%s reason=%s attempt=%s next_attempt=%s dispatch_outcome=%s",
        op.pk, op.payload.get("company_id"), op.connection_id, op.generation, op.receipt.get("current_step"),
        op.state, op.receipt.get("reason_code", ""), op.attempts, op.next_attempt_at, op.receipt.get("dispatch_outcome", ""))
    return _persist_prepare(op, now=now)


def _permission_action(connection, *, grants=None):
    from .website_github_permissions import installation_grants
    grants = grants or installation_grants(connection)
    return {"id": "review_github_permissions", "label": "Review GitHub permissions", "url": grants["settingsUrl"]}


def prepare_failure(op, step, error, *, now=None):
    """Classify failures without asking the founder to repeat retryable work."""
    from .website_failure_scope import taxonomy_enabled
    from .website_failure_contract import resolve_failure
    now = now or timezone.now()
    code = getattr(error, "code", "website_prepare_unavailable")
    message = str(error) or "Website preparation is temporarily unavailable."
    failures = dict(op.receipt.get("step_failures") or {})
    step = str(step or op.receipt.get("current_step") or "prepare")
    policy_enabled = taxonomy_enabled(op.connection.organization_id)
    observation_enabled = taxonomy_enabled(op.connection.organization_id, observe=True)
    decision = resolve_failure(getattr(error, "causal_code", code) if observation_enabled or policy_enabled else code, getattr(error, "result", None),
        retryable=getattr(error, "retryable", False), status=getattr(error, "status", None),
        stage=step, action=getattr(error, "user_action", None), findings=getattr(error, "findings", None),
        apply=policy_enabled, observe=observation_enabled, local=getattr(error, "result", None) is None)
    if observation_enabled and not policy_enabled and getattr(error, "result", None) is None and getattr(error, "legacy_projection", None):
        from .website_failure_classes import legacy_failure_class
        original = error.legacy_projection
        decision = {**decision, "code": original["code"], "failure_class": legacy_failure_class(original["code"],
            retryable=original["retryable"], status=getattr(error, "status", None))}
    code, classification = decision["code"], decision["failure_class"]
    strict_policy = bool(policy_enabled or decision["contract_invalid"])
    permission_needed = step in PERMISSION_STEPS.get(code, ())
    if not (strict_policy and classification == "waiting"):
        failures[step] = failures.get(step, 0) + 1
    if strict_policy and decision["failure"] is not None:
        message = decision["failure"]["user_message"]
    op.receipt = {**op.receipt, "step_failures": failures, "reason_code": code, "message": message,
        "failure_class": classification, "retryable": classification == "transient"}
    if decision["failure"] is not None and (strict_policy or decision["versioned"]):
        op.receipt["failure"] = decision["failure"]
        op.receipt["failure_fingerprint"] = decision["failure"]["failure_fingerprint"]
        op.receipt["failure_taxonomy_policy_applied"] = strict_policy
        # A pushed/pulled observation retains the producer's occurrence. Local
        # preparation outcomes bind to the existing lease/attempt, not delivery
        # IDs or a fabricated site goal. B2/B4 still own goal-wide counters.
        from uuid import UUID, NAMESPACE_URL, uuid5
        source = getattr(error, "result", None) or {}
        nested = source.get("article_system_setup") if isinstance(source, dict) else {}
        nested = nested if isinstance(nested, dict) else {}
        occurrence = source.get("failure_occurrence_id") or nested.get("failure_occurrence_id")
        if isinstance(occurrence, str):
            try:
                UUID(occurrence)
            except ValueError:
                occurrence = None
        else:
            occurrence = None
        if not occurrence:
            occurrence = str(uuid5(NAMESPACE_URL, f"mlai:prepare-failure:v1:{op.pk}:{op.generation}:{op.payload.get('attempt', 1)}:{op.attempts}:{step}"))
        op.receipt["failure_occurrence_id"] = occurrence
        from . import website_strategy as goal_strategy
        if "goal" in op.receipt and goal_strategy.enabled() and isinstance(source, dict) and "strategy_exhausted" in source:
            op.receipt["strategy_observation_invalid"] = not goal_strategy.valid_exhaustion(op.receipt["goal"], source["strategy_exhausted"])
    if observation_enabled:
        op.receipt["failure_taxonomy_observation"] = {"failure": decision["failure"], "classification": decision["failure"]["failure_class"] if decision["failure"] else None,
            "policy_classification": classification, "contract_invalid": decision["contract_invalid"]}
    if (getattr(error, "user_action", None) and (classification == "needs_user" or not strict_policy and getattr(error, "result", None) is None)
            and (code not in PERMISSION_CODES or permission_needed)):
        op.state, op.next_attempt_at = "needs_user", None
        op.receipt.update(status="needs_you", user_action=error.user_action, failure_class="needs_user", retryable=False)
    elif permission_needed and (not strict_policy or classification == "needs_user"):
        op.state, op.next_attempt_at = "needs_user", None
        op.receipt.update(status="needs_you", user_action=_permission_action(op.connection), failure_class="needs_user", retryable=False)
    elif code in AUTHORIZATION_CODES and code not in PERMISSION_CODES and (not strict_policy or classification == "needs_user"):
        op.state = "needs_user"
        op.receipt.update(status="needs_you", user_action={"id": "authorize_github", "label": "Authorize GitHub"},
            failure_class="needs_user", retryable=False)
        op.next_attempt_at = None
    elif strict_policy and classification == "waiting" and code not in DEPLOYMENT_WAIT_CODES:
        from datetime import datetime
        count = op.receipt.get("waiting_observations", 0) + 1
        op.receipt["waiting_observations"] = count
        started = op.receipt.setdefault("waiting_started_at", now.isoformat())
        deadline = datetime.fromisoformat(started) + timedelta(hours=24)
        if now >= deadline:
            from .website_operational_alerts import record_policy_stall
            record_policy_stall(op, step=step, reason="waiting_policy_deadline", started_at=started, deadline=deadline, now=now)
        op.state = "pending" if now < deadline else "needs_attention"
        op.receipt["status"] = "waiting_for_step" if op.state == "pending" else "needs_attention"
        op.next_attempt_at = min(deadline, now + timedelta(minutes=DEPLOYMENT_RETRY_MINUTES[min(count - 1, len(DEPLOYMENT_RETRY_MINUTES) - 1)])) if op.state == "pending" else None
        op.receipt.pop("user_action", None)
    elif (not strict_policy and code in DEPLOYMENT_WAIT_CODES) or classification == "waiting":
        count = op.receipt.get("deployment_attempts", 0) + 1
        op.receipt["deployment_attempts"] = count
        started = op.receipt.setdefault("deployment_wait_started_at", now.isoformat())
        from datetime import datetime
        deadline = datetime.fromisoformat(started) + timedelta(hours=24)
        from .website_deployment import enabled as deploy_detection_enabled, valid as valid_deployment_hint, manual_action
        from .website_goal import mapping
        source = mapping(op.receipt.get("goal")).get("current_source_sha") or getattr(op.connection,"verified_sha",None)
        deployment_hint = mapping(getattr(op.connection,"capabilities",None)).get("deploymentFingerprint")
        manual_deploy = deploy_detection_enabled() and valid_deployment_hint(deployment_hint,op.connection,source) and deployment_hint["automation"] == "manual"
        if manual_deploy:
            deadline = now
        elif now >= deadline:
            from .website_operational_alerts import record_policy_stall
            record_policy_stall(op, step=step, reason="deployment_policy_deadline", started_at=started, deadline=deadline, now=now)
        op.receipt["failure_class"] = "waiting"
        op.state = "pending" if now < deadline else "needs_user"
        op.receipt["status"] = "waiting_for_deployment" if op.state == "pending" else "needs_you"
        op.next_attempt_at = min(deadline, now + timedelta(minutes=DEPLOYMENT_RETRY_MINUTES[min(count - 1, len(DEPLOYMENT_RETRY_MINUTES) - 1)])) if op.state == "pending" else None
        if op.state == "needs_user":
            provider = (getattr(op.connection, "capabilities", {}) or {}).get("hosting_provider") or "your hosting provider"
            op.receipt.update(message=f"Deploy your site with {provider}, then check again.",
                user_action={"id": "deploy_site", "label": "Deploy your site", "hosting_provider": provider})
            if deploy_detection_enabled():
                action = manual_action(op.connection,source)
                op.receipt.update(user_action=action,message=action["instructions"] + " We’ll check the live site and continue when it is deployed.")
            # Keep a quiet observer after the owner prompt, so a manual deploy
            # resumes without requiring another button click.
            op.next_attempt_at = now + timedelta(hours=2)
    elif classification == "transient":
        from datetime import datetime
        started = op.receipt.setdefault("transient_started_at", now.isoformat())
        deadline = datetime.fromisoformat(started) + timedelta(hours=24)
        if now >= deadline:
            from .website_operational_alerts import record_policy_stall
            record_policy_stall(op, step=step, reason="transient_policy_deadline", started_at=started, deadline=deadline, now=now)
        op.state = "pending" if now < deadline else "needs_attention"
        op.next_attempt_at = min(deadline, now + timedelta(minutes=RETRY_MINUTES[min(failures[step] - 1, len(RETRY_MINUTES) - 1)])) if op.state == "pending" else None
        op.receipt["status"] = "working" if op.state == "pending" else "needs_attention"
    elif classification == "needs_user":
        if strict_policy and (code in PERMISSION_CODES or not decision["failure"] or not decision["failure"]["action"]):
            op.state, op.next_attempt_at = "needs_attention", None
            op.receipt.update(status="needs_attention")
            op.receipt.pop("user_action", None)
        else:
            op.state, op.next_attempt_at = "needs_user", None
            op.receipt.update(status="needs_you", user_action=decision["failure"]["action"] if strict_policy else {
                "id": "open_run", "label": "Open setup run", "run_id": op.receipt.get("child_run_id")})
    elif classification == "unsupported":
        op.next_attempt_at = None
        if strict_policy:
            op.state = "needs_attention"
            op.receipt.update(status="needs_attention")
            op.receipt.pop("user_action", None)
            op.receipt.pop("channel_options", None)
        else:
            op.state = "stopped"
            op.receipt.update(status="stopped", channel_options=["hosted", "portable"],
                user_action={"id": "choose_article_channel", "label": "Choose an articles channel"})
    else:
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="needs_attention", user_action={"id": "retry", "label": "Try again"})
    from .website_operations import prepare_receipt
    op.receipt = prepare_receipt(op)


def _canonical_article_route(route):
    """Compare equivalent listing paths while retaining root as a valid route."""
    route = str(route or "")
    return re.sub(r"/+", "/", route).rstrip("/") or "/"


def _setup_surface(config, journey, *, goal=None):
    """Respect a saved owner choice, then use the current scan's listing route."""
    from django.conf import settings
    from .website_goal import mapping
    answers = mapping(mapping(mapping(goal).get("consent")).get("answers"))
    if getattr(settings, "ARTICLES_AUTO_PAGE_ENABLED", False) and goal is not None and answers.get("page_policy") == "explicit":
        return "existing" if answers.get("page_mode") == "existing" else "none", _canonical_article_route(answers["page_route"])
    pending = (config.article_system or {}).get("pending_article_system_setup") or {}
    if pending.get("mode") in {"existing", "none"} and pending.get("route_path"):
        return pending["mode"], _canonical_article_route(pending["route_path"])
    from django.conf import settings
    if getattr(settings, "ARTICLES_AUTO_PAGE_ENABLED", False) and goal is not None:
        from .website_article_page import choose_page
        from .website_goal import mapping
        consent = mapping(mapping(goal).get("consent"))
        answers = mapping(consent.get("answers"))
        if answers.get("page_policy") == "explicit":
            return "existing" if answers.get("page_mode") == "existing" else "none", _canonical_article_route(answers["page_route"])
        selection = choose_page(mapping(journey).get("discovery"), pick_when_ambiguous=answers.get("page_policy") == "pick")
        if selection["requiresChoice"]:
            error = WebsiteAuthorityError("articles_page_choice_required", "Choose the articles page to prepare.", retryable=False)
            error.user_action = {"id": "choose_articles_page", "candidates": selection["candidates"]}
            raise error
        return selection["mode"], selection["route"]
    discovery = (journey or {}).get("discovery") or {}
    resolution = discovery.get("resolution") or {}
    def route(item):
        if isinstance(item, str):
            return item
        if not isinstance(item, dict):
            return ""
        return item.get("route") or (item.get("metadata") or {}).get("route_path") or ""
    listing = route(resolution.get("listing_surface"))
    ranked = (resolution.get("ranked_candidates") or {}).get("listing_surface_candidates") or resolution.get("listing_surface_candidates") or []
    candidates = list(dict.fromkeys(value for item in ranked if (value := route(item))))
    if not candidates:
        candidates = list(dict.fromkeys(value for item in discovery.get("candidates", [])
            if item.get("candidate_group") == "listing_surface_candidates" and (value := route(item))))
    state = resolution.get("existence_state")
    if state == "missing":
        return "none", "/articles"
    if state == "confirmed" and (listing or candidates):
        return "existing", listing or candidates[0]
    if state == "ambiguous":
        candidates = list(dict.fromkeys(([listing] if listing else []) + candidates))
        if len(candidates) == 1:
            return "existing", candidates[0]
        if candidates:
            error = WebsiteAuthorityError("articles_page_choice_required", "Choose the articles page to prepare.", retryable=False)
            error.user_action = {"id": "choose_articles_page", "candidates": candidates}
            raise error
    raise WebsiteAuthorityError("article_surface_unresolved", "Scan the repository to identify an articles page before setup.", retryable=False)


def _owner_request(op, context, config, step, *, user, journey=None):
    """Call existing guarded owner handlers with a complete POST request shape."""
    connection = config.website_connection
    data = {**contract_for(connection), "company_id": str(context.company.pk),
        "configuration_revision": connection.configuration_version,
        "idempotency_key": f"prepare:{op.pk}:{step}:{op.payload.get('attempt', 1)}:{op.payload.get('retry_epoch', 'initial')}",
        "retry_epoch": op.payload.get("retry_epoch", "initial"),
        "force_refresh": True}
    from . import website_goal_budget as goal_budget, website_goal as goals
    if "goal" in op.receipt and goal_budget.enabled() and step in {"setup", "scan"}:
        goal = op.receipt["goal"]
        used = goals.mapping(goal.get("budget_used"))
        from django.conf import settings
        data["goal_budget"] = {"version": 1, "goal_id": goal["goal_id"],
            "scope_digest": goals.digest(goal["scope"]),
            "remaining_active_seconds": goal_budget.active_limit(goal) - (used.get("active_seconds") or 0),
            "remaining_model_usd": settings.ARTICLES_GOAL_MODEL_USD - (used.get("model_cost") or 0)}
    if "goal" in op.receipt and step == "setup":
        from .website_strategy import request_pin
        data.update(request_pin(op.receipt["goal"]))
        from .website_setup_consent import valid as valid_consent
        from .website_hosted_build import enabled as hosted_enabled
        if valid_consent(op.receipt["goal"]) and hosted_enabled(connection):
            data["verification_mode"] = op.receipt["goal"]["consent"]["answers"]["ci_mode"]
    if step == "setup":
        mode, surface = _setup_surface(config, journey or journey_for_context(context, config), goal=op.receipt.get("goal"))
        pending = (config.article_system or {}).get("pending_article_system_setup") or {}
        if op.payload.get("manual_setup_example_cleanup_retry") or op.payload.get("source_reverify"):
            pending = {**pending, "source_scan_run_id": op.receipt.get("scan_run_id") or ""}
        data.update(article_surface_mode=mode, article_surface_url=surface,
            source_scan_run_id=pending.get("source_scan_run_id") or op.receipt.get("scan_run_id") or "")
    return SimpleNamespace(user=user, data=data, method="POST", META={},
        query_params={"company_id": str(context.company.pk)}, headers={"Idempotency-Key": data["idempotency_key"]},
        _website_prepare_dispatch=True)


def _awaiting_page_choice(op):
    return (op.receipt.get("user_action") or {}).get("id") == "choose_articles_page"


def _selected_setup_child(op, config, *, run=None):
    """Recognize a saved route-choice child without changing its authority tuple."""
    if not _awaiting_page_choice(op) or op.receipt.get("child_run_id"):
        return None
    website = config.website_connection
    if op.generation != website.generation or str(op.connection_id) != str(website.pk):
        return None
    pending = (config.article_system or {}).get("pending_article_system_setup") or {}
    identifier = pending.get("setup_run_id") or pending.get("setupRunId")
    route = pending.get("route_path") or pending.get("routePath")
    candidates = (op.receipt.get("user_action") or {}).get("candidates") or []
    if not identifier or not route or pending.get("mode") != "existing" or _canonical_article_route(route) not in {_canonical_article_route(item) for item in candidates}:
        return None
    if run is None:
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.filter(run_id=identifier, organization_id=website.organization_id).first()
    if (run is None or run.run_id != identifier or run.workflow != "article_system_setup"
            or run.organization_id != website.organization_id
            or str(run.domain or "").casefold().strip() != website.organization.domain.casefold().strip()):
        return None
    saved = run.run_request if isinstance(run.run_request, dict) else {}
    hint = saved.get("article_surface_hint") or {}
    if (not isinstance(hint, dict) or not hint.get("route_path") or _canonical_article_route(hint.get("route_path")) != _canonical_article_route(route)
            or saved.get("domain") and str(saved["domain"]).casefold().strip() != website.organization.domain.casefold().strip()):
        return None
    try:
        original, expected = connection_contract(saved), connection_contract(contract_for(website))
    except WebsiteAuthorityError:
        return None
    if any(original.get(key) != expected.get(key) for key in ("website_connection_id", "connection_generation", "repository_id")):
        return None
    return run


def _correlate_setup_child(op, run):
    from .website_hosted_setup_lineage import discard_for_new_setup
    discard_for_new_setup(op)
    op.receipt = {**op.receipt, "child_run_id": run.run_id, "setup_run_id": run.run_id, "current_step": "setup",
        "status": "working", "message": "Preparing your selected articles page.", "user_action": None,
        "reason_code": "", "blocked_step": None, "step_failures": {}, "last_completed": None, "retryable": False}
    op.payload = {**op.payload, "run_id": run.run_id}
    op.state, op.next_attempt_at = "pending", timezone.now()


def attach_selected_setup_run(config, run, *, company_id):
    """Hand a founder's direct route-choice setup back to its waiting prepare.

    The child retains its original workflow operation and connection fences;
    correlation never adds or changes credentials, consent or publishing proof.
    """
    website = config.website_connection
    with authority_guard(contract_for(website), action="read") as current:
        op = current.operations.select_for_update().filter(generation=current.generation, action="prepare",
            state__in=["needs_user", "pending", "running"], payload__company_id=str(company_id)).first()
        if op is None or str(op.payload.get("company_id")) != str(company_id) or _selected_setup_child(op, config, run=run) is None:
            return None
        _correlate_setup_child(op, run)
        op.save(update_fields=["receipt", "payload", "state", "next_attempt_at", "updated_at"])
    return op


def _dispatch_prepare_step(op, context, config, step, *, user, journey=None):
    from .vibe_marketing_views import VibeMarketingScanView, VibeMarketingArticleSystemSetupView, _article_capabilities_for_context
    if step == "permissions":
        grants = (journey or {}).get("githubPermissions") or {}
        prerequisite = (journey or {}).get("prerequisites", {}).get("permissions") or {}
        error = WebsiteAuthorityError(prerequisite.get("reasonCode") or "github_workflow_permission_required",
            prerequisite.get("reason") or "Review GitHub permissions.", retryable=grants.get("retryable", False))
        if error.code in PERMISSION_CODES:
            error.user_action = _permission_action(config.website_connection, grants=grants)
        raise error
    if step == "verify-access":
        capabilities = _article_capabilities_for_context(context, config, force=True)
        if not capabilities.get("repositoryAccessVerified"):
            raise WebsiteAuthorityError(capabilities.get("reasonCode") or "github_access_required",
                capabilities.get("reason") or "Authorize GitHub to prepare your website.")
        return {"status": "completed", "capabilities": capabilities}
    if step in {"scan", "setup"}:
        request = _owner_request(op, context, config, step, user=user, journey=journey)
        view = VibeMarketingScanView if step == "scan" else VibeMarketingArticleSystemSetupView
        response = view().post(request)
        body = dict(response.data)
        if response.status_code >= 400:
            raise WebsiteAuthorityError(body.get("code") or body.get("error_code") or "website_prepare_unavailable",
                body.get("detail") or "Website preparation could not start.", status=response.status_code,
                retryable=body.get("retryable", response.status_code >= 500))
        return body
    from .website_verification import current_ci_attestation, discover_source_attestation, record_ci_attestation, verify_live_deployment
    website = config.website_connection
    target = website.targets.filter(generation=website.generation, target_key=config.default_publish_target_id).first()
    if target is None:
        raise WebsiteAuthorityError("verified_target_required", "Waiting for articles build and render proof.", retryable=True)
    repository = (journey or {}).get("repository") or {}
    receipt = current_ci_attestation(website, target, capabilities={"repositorySourceSha": repository.get("observedSourceSha"),
        "repositoryBranch": repository.get("branch")}) if step == "verify" else None
    if receipt is None:
        from .website_connections import observed_source_sha
        source_sha = observed_source_sha(website) or repository.get("observedSourceSha") or website.verified_sha
        from .website_hosted_verification import enabled as hosted_attestation_enabled, advance as advance_hosted
        if hosted_attestation_enabled(website):
            from .website_setup_consent import valid as valid_consent
            if valid_consent(op.receipt.get("goal")) and op.receipt["goal"]["consent"]["answers"]["ci_mode"] == "hosted":
                outcome = advance_hosted(op, context, config, target, source_sha)
                if outcome["status"] != "completed":return outcome
                if step == "ci-attestation":return outcome
                website.refresh_from_db()
                target.refresh_from_db()
                receipt = current_ci_attestation(website, target, capabilities={"repositorySourceSha": source_sha, "repositoryBranch": website.branch})
                if receipt is None:raise WebsiteAuthorityError("ci_attestation_required", "Waiting for saved hosted source proof.", retryable=True)
        if receipt is None:
            proof = discover_source_attestation(website, target, source_sha)
            ci = record_ci_attestation(proof)
            receipt = ci.receipt
    if step == "verify":
        from .website_sandbox_faults import stage
        deployment = stage(op, context, run_id=op.receipt.get("setup_run_id") or op.receipt.get("merged_setup_run_id"),
            boundary="deployment_observation", forward=lambda: verify_live_deployment(config, data=receipt))
        return {"status": "completed", "operation_id": str(deployment.pk)}
    return {"status": "completed", "operation_id": str(ci.pk)}


def _unverified_authorization_is_transient(op, child, code, classification, step):
    """Whether a child stopped only because its authorization check went unanswered.

    A refused connection is disconnected or regenerated, and claiming this
    operation would already have cancelled it. While the connection still
    grants the child run's saved generation, this backend is not refusing the
    run, so it is resumed (a bounded number of times) instead of waiting for
    Try again. The resumed run re-authorizes each of its actions.
    """
    if code != UNVERIFIED_AUTHORIZATION_CODE or classification != "repairable":
        return False
    from .website_connections import scoped_run_contract
    contract, connection = scoped_run_contract(child), op.connection
    if (connection.state != "connected" or op.generation != connection.generation
            or str(contract.get("website_connection_id") or "") != str(connection.pk)
            or str(contract.get("connection_generation") or "") != str(connection.generation)):
        return False
    if op.receipt.get("child_resume_pending"):
        return True  # Carry out the resume already scheduled for this failure.
    return (op.receipt.get("step_failures") or {}).get(step, 0) < UNVERIFIED_AUTHORIZATION_RESUMES


def _resume_prepare_child(op, child, *, user, context):
    """Resume the saved run through existing guarded control and attempt fencing."""
    from .vibe_marketing_views import VibeMarketingRunControlView
    from .website_connections import scoped_run_contract
    request = SimpleNamespace(method="POST", user=user, META={}, query_params={}, headers={},
        data={**scoped_run_contract(child), "company_id": str(context.company.pk)})
    response = VibeMarketingRunControlView().post(request, child.run_id, "resume")
    if response.status_code >= 400:
        raise WebsiteAuthorityError(response.data.get("code") or "worker_unavailable",
            response.data.get("detail") or "The saved setup could not resume.",
            status=response.status_code, retryable=response.status_code >= 500)


def _setup_owner_action(child, setup, connection):
    status = setup.get("status")
    if status == "code_review_ready":
        return {"id": "review_setup", "label": "Review setup", "run_id": child.run_id,
            "url": setup.get("pr_url") or (child.result or {}).get("pr_url")}
    if status == "manual_merge_required":
        from .vibe_marketing_views import _pull_request_number_from_run
        number = _pull_request_number_from_run(child)
        return {"id": "merge_pr", "label": "Merge setup PR", "run_id": child.run_id,
            "pr_number": number, "url": setup.get("pr_url") or (child.result or {}).get("pr_url") or
                (f"https://github.com/{connection.github_repo}/pull/{number}" if number else None)}
    return None


def advance_prepare(identifier, *, now=None, handlers=None):
    """Advance a single leased operation by its next unmet readiness requirement."""
    now = now or timezone.now()
    op = _claim_prepare(identifier, now)
    if op is None:
        return None
    from .website_failure_scope import failure_scope
    with failure_scope(op.connection.organization_id):
        return _advance_claimed_prepare(op, now=now, handlers=handlers)


def _advance_claimed_prepare(op, *, now, handlers):
    """Advance the original lease inside its saved organization's B1 scope."""
    step = op.receipt.get("current_step") or "verify-access"
    try:
        context, config, user = _prepare_context(op)
        from .website_first_article_billing import reservation_ready
        if not reservation_ready(op):
            op.state,op.next_attempt_at="pending",now+timedelta(minutes=1)
            op.receipt.update(status="working",message="Completing the previous article credit.",user_action=None)
            return _finish_prepare(op,now=now)
        journey = journey_for_context(context, config)
        journey = _cleanup_retry_journey(op, context, config, journey)
        from .website_goal_runner import owns_goal, goal_journey, ready
        if owns_goal(op):
            journey = goal_journey(op, context, config, journey, now=now)
            if getattr(op, "_goal_wait", False):
                return _finish_prepare(op, now=now)
        if ready(op, journey):
            op.state, op.next_attempt_at = "completed", None
            op.receipt.update(status="ready", message="Your website is ready for articles.", current_step="ready", user_action=None)
            return _finish_prepare(op, now=now)
        if (journey.get("nextAction") or {}).get("id")=="first-article":
            from .website_first_article import advance as advance_first_article
            step="first-article"
            from .website_sandbox_faults import disconnect_before_article
            if disconnect_before_article(op, context, config):
                return _finish_prepare(op, now=now)
            advance_first_article(op,context,config,user=user,now=now)
            return _finish_prepare(op,now=now)
        child_id = op.receipt.get("child_run_id")
        if (owns_goal(op) and getattr(op, "_goal_milestone", None) in {"access", "understood"}
                and child_id == op.receipt.get("setup_run_id")):
            child_id = None
        child = None
        if not child_id:
            child = _selected_setup_child(op, config)
            if child:
                _correlate_setup_child(op, child)
                child_id = child.run_id
            elif _awaiting_page_choice(op):
                pending = (config.article_system or {}).get("pending_article_system_setup") or {}
                if pending.get("status") == "pending_generation" and pending.get("mode") == "existing":
                    # Progress may arrive while the owner's queue request is
                    # still in flight. Do not start another differently keyed
                    # setup before its saved child identity is confirmed.
                    raise WebsiteAuthorityError("website_setup_dispatch_pending",
                        "Waiting for the selected articles setup request to finish queuing.", retryable=True)
            elif not owns_goal(op):
                child = _return_to_open_setup_pr(op, config, journey)
                child_id = child.run_id if child else None
        if child_id:
            from workflow_runs.models import ContentFactoryRun
            child = child or ContentFactoryRun.objects.filter(run_id=child_id, organization_id=op.connection.organization_id).first()
            setup = ((child.result or {}).get("article_system_setup") or {}) if child else {}
            owner_action = _setup_owner_action(child, setup, op.connection) if child and child.workflow == "article_system_setup" else None
            if owner_action:
                if owns_goal(op) and not any(item["name"] == "previewed" and item["state"] == "passed"
                        for item in op.receipt["goal"]["milestones"]):
                    raise WebsiteAuthorityError("setup_preview_verification_required", "Waiting for the exact setup preview proof.", retryable=True)
                op.state, op.next_attempt_at = "needs_user", None
                op.receipt.update(status="needs_you", failure_class="needs_user", user_action=owner_action,
                    message="Review the articles setup." if owner_action["id"] == "review_setup" else "Merge the articles setup pull request.")
                return _finish_prepare(op, now=now)
            setup_review_ready = bool(child and child.workflow == "article_system_setup" and
                child.status in {"needs_review", "awaiting_approval", "approval_required", "pr_opened", "publish_bundle_ready"} and
                setup.get("status") in {"preview_ready", "pr_created", "completed", "ready", "adopted_existing_surface", "adopted"})
            if child and not setup_review_ready and child.status not in {"completed", "failed", "blocked", "cancelled", "denied"}:
                op.state, op.next_attempt_at = "pending", now + timedelta(minutes=1)
                return _finish_prepare(op, now=now)
            if child and child.status in {"failed", "blocked", "cancelled", "denied"}:
                if _retain_setup_example_cleanup(op, child):
                    return _finish_prepare(op, now=now)
                result = child.result or {}
                code = result.get("error_code") or setup.get("error_code") or "website_prepare_step_failed"
                classification = failure_class(code, result, retryable=result.get("retryable") is True)
                if _unverified_authorization_is_transient(op, child, code, classification, step):
                    classification = "transient"
                message = getattr(child, "error", "") or result.get("error") or result.get("message") or "The website step could not finish."
                if classification == "transient" and op.receipt.get("child_resume_pending"):
                    _resume_prepare_child(op, child, user=user, context=context)
                    op.receipt.pop("child_resume_pending", None)
                    op.state, op.next_attempt_at = "pending", now + timedelta(minutes=1)
                    op.receipt.update(status="working", message="Resuming the saved website setup.")
                    return _finish_prepare(op, now=now)
                error = WebsiteAuthorityError(code, message, retryable=classification == "transient")
                error.result = {**result, "failure_class": classification}
                error.user_action = result.get("user_action") or setup.get("user_action")
                prepare_failure(op, step, error, now=now)
                if classification == "transient" and op.state == "pending":
                    op.receipt["child_resume_pending"] = True
                elif op.state == "needs_attention":
                    op.receipt["user_action"] = {"id": "open_run", "label": "Open setup run", "run_id": child_id}
                return _finish_prepare(op, now=now)
            if child and child.workflow == "article_system_setup":
                if owns_goal(op) and not any(item["name"] == "previewed" and item["state"] == "passed"
                        for item in op.receipt["goal"]["milestones"]):
                    raise WebsiteAuthorityError("setup_preview_verification_required", "Waiting for the exact setup preview proof.", retryable=True)
                from .website_sandbox_faults import stage
                merge = stage(op, context, run_id=getattr(child, "run_id", None), boundary="merge_observation",
                    forward=lambda: maybe_merge_prepared_setup(child, context, config, user=user, goal=op.receipt.get("goal")))
                if merge.get("status") == "needs_user":
                    review = merge["user_action"].get("id") == "review_setup" and not merge["user_action"].get("pr_number")
                    op.state, op.next_attempt_at = "needs_user", None
                    op.receipt.update(status="needs_you", current_step="setup" if review else "merge",
                        message=merge.get("message") or ("Review the articles setup preview, then approve it to open the setup pull request."
                            if review else "Merge the articles setup pull request."),
                        user_action=merge["user_action"])
                    return _finish_prepare(op, now=now)
                if merge.get("status") == "pending":
                    op.state, op.next_attempt_at = "pending", now + timedelta(minutes=2)
                    return _finish_prepare(op, now=now)
                if merge.get("status") == "completed" and op.receipt.get("merged_setup_run_id") != child.run_id and (
                        "merged" in {(child.result or {}).get("merge_status"), setup.get("merge_status")}):
                    # Re-read the merged branch head (source_reverify forces it) before choosing a step.
                    _begin_post_merge_attempt(op, child)
                    op.state, op.next_attempt_at = "pending", now
                    return _finish_prepare(op, now=now)
                # "superseded": the pull request was closed on GitHub, so a new setup replaces it below.
            op.receipt.pop("child_run_id", None)
        # Access probes may change the next requirement immediately; bound the
        # same-pass continuation to one probe plus one actual work dispatch.
        for _ in range(2):
            next_action = journey.get("nextAction") or {}
            step = next_action.get("id")
            blocked = next((item for item in journey.get("steps", []) if item.get("status") != "complete"), {})
            if not step:
                step = blocked.get("id") or op.receipt.get("current_step") or "prepare"
                op.receipt.update(current_step=step, blocked_step=step)
                raise WebsiteAuthorityError(blocked.get("reasonCode") or blocked.get("reason_code") or journey.get("reasonCode") or "website_prepare_blocked",
                    blocked.get("reason") or blocked.get("message") or journey.get("reason") or "Website preparation needs attention.", retryable=False)
            if step == "setup" and not op.payload.get("repair_setup"):
                from .website_connections import settled_setup_run
                if settled_setup_run(op.connection, source_sha=(journey.get("repository") or {}).get("observedSourceSha") or ""):
                    # Another setup on this head would re-stamp its marker and open
                    # another PR. Wait for the branch's own CI proof instead.
                    step = "ci-attestation"
            digest = evidence_digest({"prerequisites": journey.get("prerequisites"), "capabilities": journey.get("capabilities")})
            if op.receipt.get("last_completed") == {"step": step, "facts_digest": digest}:
                raise WebsiteAuthorityError("prepare_no_progress", blocked.get("reason") or next_action.get("label") or "Website preparation needs attention.", retryable=False)
            op.receipt.update(current_step=step, blocked_step=None, reason_code="", retryable=False,
                status="working", message=f"Preparing articles: {step.replace('-', ' ')}.", user_action=None)
            handler = (handlers or {}).get(step)
            outcome = handler(op, context, config, user=user) if handler else _dispatch_prepare_step(op, context, config, step, user=user, journey=journey)
            run_id = (outcome or {}).get("run_id") or (outcome or {}).get("runId")
            if run_id:
                if step == "setup":
                    from .website_hosted_setup_lineage import discard_for_new_setup
                    discard_for_new_setup(op)
                op.receipt.update(child_run_id=run_id, **{f"{step}_run_id": run_id})
                op.payload = {**op.payload, "run_id": run_id}
                break
            if (outcome or {}).get("status") == "pending":
                op.state, op.next_attempt_at = "pending", now + timedelta(minutes=1)
                return _finish_prepare(op, now=now)
            steps = list(op.receipt.get("completed_steps") or [])
            if step not in steps:
                steps.append(step)
            op.receipt["completed_steps"] = steps
            forced = (outcome or {}).get("capabilities") if step == "verify-access" else None
            if forced is not None:
                journey = journey_for_context(context, config, capabilities=forced)
                if owns_goal(op):
                    journey = goal_journey(op, context, config, journey, now=now)
                digest = evidence_digest({"prerequisites": journey.get("prerequisites"), "capabilities": journey.get("capabilities")})
            if ready(op, journey):
                op.state, op.next_attempt_at = "completed", None
                op.receipt.update(status="ready", message="Your website is ready for articles.", current_step="ready", user_action=None)
                return _finish_prepare(op, now=now)
            op.receipt["last_completed"] = {"step": step, "facts_digest": digest}
            if forced is not None and (journey.get("nextAction") or {}).get("id") != step:
                continue
            break
        op.state, op.next_attempt_at = "pending", now + timedelta(minutes=1)
    except WebsiteAuthorityError as exc:
        prepare_failure(op, step, exc, now=now)
    except Exception as exc:
        # Preserve stack locations without logging provider exception bodies.
        logger.exception("website_prepare unexpected operation=%s company=%s connection=%s generation=%s step=%s error_type=%s",
            op.pk, op.payload.get("company_id"), op.connection_id, op.generation, step, type(exc).__name__,
            exc_info=(RuntimeError, RuntimeError("Unexpected prepare dispatch failure"), exc.__traceback__))
        code = "website_prepare_unavailable"
        from .website_github_failure import taxonomy_active
        if taxonomy_active():
            from django.db import OperationalError
            from integrations import http_client
            code = "website_prepare_dependency_transient" if isinstance(exc, (http_client.exceptions.Timeout,
                http_client.exceptions.ConnectionError, OperationalError, TimeoutError)) else "website_prepare_contract_invalid"
        prepare_failure(op, step, WebsiteAuthorityError(code,
            "Website preparation is temporarily unavailable. It will retry automatically.", retryable=True), now=now)
    return _finish_prepare(op, now=now)


def advance_prepares_for_connection(connection_id):
    """Wake current preparation after scan, setup or CI callbacks commit."""
    from .website_wakeups import enabled as event_wakes_enabled, queue_wake
    if event_wakes_enabled():
        return int(queue_wake(connection_id, signal={"kind": "trusted_callback", "success": True}) is not None)
    current = WebsiteConnectionOperation.objects.filter(connection_id=connection_id, action="prepare",
        state__in=["pending", "needs_user"], connection__generation=F("generation"))
    current.update(state="pending", next_attempt_at=timezone.now(), updated_at=timezone.now())
    return current.count()


def wake_prepare_after_commit(connection_id):
    """Wake the scheduler only once the callback's authority transaction commits."""
    transaction.on_commit(lambda: advance_prepares_for_connection(connection_id), robust=True)


def wake_prepare_for_source(connection_id, *, organization_id, owned_merge_run_id=""):
    """Source changes reopen the current operation without granting new consent.

    Stopping preparation keeps its setup pull request publishable, so an owned
    merge of a current-generation setup (MLAI's Publish, or the owner merging
    that verified pull request on GitHub) also reopens a cancelled preparation
    to verify that setup. Any other push leaves stopped work stopped.
    """
    from .website_models import WebsiteConnection
    from .models import OrganizationContentConfig
    from founder_tools.models import VibeRaisingCompany
    from workflow_runs.models import ContentFactoryRun
    connection = WebsiteConnection.objects.select_related("organization", "authorized_by").filter(
        pk=connection_id, organization_id=organization_id, state="connected").first()
    if connection is None or connection.authorized_by is None:
        return None
    config = OrganizationContentConfig.objects.filter(website_connection=connection).first()
    company = VibeRaisingCompany.objects.filter(organization_id=organization_id,
        profile__user_id=connection.authorized_by_id).first()
    if config is None or company is None:
        return None
    from .website_goal_runner import wake_goal_for_source, owns_goal, terminal_first_article_goal
    from .website_goal import enrolled
    latest = connection.operations.filter(generation=connection.generation, action="prepare").order_by("-created_at", "-pk").first()
    # A flag rollback cannot turn the finished original paid goal into legacy
    # source work. A new first article requires an explicit owner submission.
    if latest and terminal_first_article_goal(latest):
        return None
    if enrolled(connection.organization_id) and latest and owns_goal(latest):
        return wake_goal_for_source(config, company_id=company.pk, user=connection.authorized_by, now=timezone.now())
    op = start_prepare(config, company_id=company.pk, user=connection.authorized_by)
    reopenable = ["completed", "needs_user", "needs_attention", "pending", "stopped"]
    owned_setup = bool(owned_merge_run_id) and ContentFactoryRun.objects.filter(run_id=owned_merge_run_id,
        organization_id=organization_id, workflow="article_system_setup", run_request__website_connection_id=str(connection.pk),
        run_request__connection_generation=connection.generation).exists()
    if owned_setup:
        reopenable.append("cancelled")
    # Cancellation bookkeeping stays with the cancel follow-up that owns the cleanup.
    receipt = {key: value for key, value in op.receipt.items()
        if key not in {"scan_run_id", "setup_run_id", "completed_steps", "deployment_wait_started_at", "transient_started_at", "child_resume_pending",
            "remote_effects", "remote_outcome_unknown", "remote_cleanup_pending", "cancellation_undoes_remote_writes"}}
    if owned_setup:
        receipt["merged_setup_run_id"] = owned_merge_run_id
    reopened = WebsiteConnectionOperation.objects.filter(pk=op.pk, generation=connection.generation,
        state__in=reopenable).update(state="pending", next_attempt_at=timezone.now(),
            receipt={**receipt, "status": "working", "current_step": "scan", "deployment_attempts": 0,
                "step_failures": {}, "user_action": None, "last_completed": None, "child_run_id": None,
                "message": "Rechecking the changed website source."},
            payload={**{key: value for key, value in op.payload.items() if key not in {"run_id", "repair_setup"}},
                "source_reverify": True, "attempt": op.payload.get("attempt", 1) + 1, "retry_epoch": uuid4().hex}, updated_at=timezone.now())
    if not reopened:
        return None
    connection.operations.filter(generation=connection.generation, action="source-reverify", state="pending").update(
        state="completed", receipt={"status": "managed_by_prepare", "prepare_operation_id": str(op.pk),
            "repository_modified": False}, updated_at=timezone.now())
    return op


def _begin_post_merge_attempt(op, child):
    """Give the steps after this worker's own setup merge a fresh request identity.

    The merge's push webhook cannot reopen an operation this worker still holds,
    so a rescan or check reusing the pre-merge keys would conflict.
    """
    if op.receipt.get("merged_setup_run_id") == child.run_id:
        return  # A replay after the local transition cannot rotate its identity again.
    from .website_hosted_setup_lineage import retain_post_merge_inputs
    retain_post_merge_inputs(op, child)
    op.payload = {**{key: value for key, value in op.payload.items() if key not in {"run_id", "repair_setup"}},
        "attempt": op.payload.get("attempt", 1) + 1, "retry_epoch": uuid4().hex, "source_reverify": True}
    for key in ("scan_run_id", "setup_run_id", "completed_steps", "last_completed"):
        op.receipt.pop(key, None)
    op.receipt["merged_setup_run_id"] = child.run_id


def _return_to_open_setup_pr(op, config, journey):
    """Resume the owner's open setup pull request instead of starting over.

    Retrying or preparing again must not replace an approved setup that only
    waits to be published. A pull request built on an older branch head is
    stale and is not resumed.
    """
    from .website_connections import open_setup_pr_run, setup_kit_check_failed
    source_sha = (journey.get("repository") or {}).get("observedSourceSha") or ""
    run = open_setup_pr_run(op.connection, source_sha=source_sha)
    if run is None or (op.payload.get("repair_setup") and setup_kit_check_failed(run)):
        # The owner cannot fix MLAI's own verification job on this pull request;
        # their Try again builds a new setup and kit instead of re-reading it.
        return None
    from .website_hosted_setup_lineage import discard_for_new_setup
    discard_for_new_setup(op)
    op.receipt = {**op.receipt, "child_run_id": run.run_id, "setup_run_id": run.run_id, "current_step": "merge",
        "status": "working", "message": "Returning to your articles setup pull request.", "user_action": None,
        "reason_code": "", "blocked_step": None, "step_failures": {}, "last_completed": None, "retryable": False}
    op.payload = {**op.payload, "run_id": run.run_id}
    _restore_pending_setup_pr(config, run)
    return run


def _restore_pending_setup_pr(config, run):
    """Point the saved setup back at the open pull request the owner will publish."""
    from .vibe_marketing_views import _pull_request_number_from_run
    result = run.result if isinstance(run.result, dict) else {}
    setup = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    article_system = dict(config.article_system or {})
    pending = dict(article_system.get("pending_article_system_setup") or {})
    status = str(setup.get("status") or result.get("status") or "pr_created")
    merge = str(setup.get("merge_status") or result.get("merge_status") or "not_merged")
    pr_url = str(result.get("pr_url") or setup.get("pr_url") or "")
    number = _pull_request_number_from_run(run)
    stamp = timezone.now().isoformat()
    pending.update({"setupRunId": run.run_id, "setup_run_id": run.run_id, "prUrl": pr_url, "pr_url": pr_url,
        "prNumber": number, "pr_number": number, "status": status, "setupStatus": status, "setup_status": status,
        "mergeStatus": merge, "merge_status": merge, "currentStep": "create_pull_request",
        "current_step": "create_pull_request", "setupCurrentStep": "create_pull_request",
        "setup_current_step": "create_pull_request", "retryAvailable": False, "retry_available": False,
        "updatedAt": stamp, "updated_at": stamp})
    article_system["pending_article_system_setup"] = pending
    config.article_system = article_system
    config.save(update_fields=["article_system", "updated_at"])


def maybe_merge_prepared_setup(run, context, config, *, user, goal=None):
    """Use the shared merge path after exact preview and current consent checks."""
    from .vibe_marketing_views import _pull_request_number_from_run, _attempt_setup_publish_merge
    setup = (run.result or {}).get("article_system_setup") or {}
    number = _pull_request_number_from_run(run)
    if setup.get("merge_status") == "merged" or (run.result or {}).get("merge_status") == "merged":
        return {"status": "completed"}
    if not number:
        if run.status == "completed" and (run.result or {}).get("adopted") is True:
            return {"status": "completed"}
        return approve_prepared_setup(run, context, config, user=user, goal=goal)
    url = f"https://github.com/{config.website_connection.github_repo}/pull/{number}"
    action = {"id": "merge_pr", "label": f"Merge PR #{number}", "pr_number": number, "url": url}
    # The setup run's Publish (in MLAI Chat) verifies and merges this pull request.
    publish = {"status": "needs_user", "message": f"Your articles setup pull request #{number} is ready. Open the setup to publish it.",
        "user_action": {"id": "review_setup", "label": "Review and publish", "run_id": run.run_id, "pr_number": number, "url": url}}
    if config.requires_review:
        return publish
    outcome = _attempt_setup_publish_merge(run=run, context=context, automatic=True)
    kind = outcome.get("outcome")
    if kind in {"merged", "waiting", "auto_merge_pending"}:
        return {"status": "pending"}
    if kind == "closed":
        return {"status": "superseded"}
    if kind == "error" and outcome.get("code") == "setup_verification_required":
        # Only the owner's Publish re-verifies a pull request whose head changed
        # after its preview passed. Preparation waits for them; it never retries.
        return publish
    if kind == "checks_failed":
        raise WebsiteAuthorityError("setup_checks_failed", outcome.get("detail") or "Setup checks failed.", retryable=False)
    if kind == "error":
        code = outcome.get("code") or "github_temporarily_unavailable"
        raise WebsiteAuthorityError(code, outcome.get("detail") or "Waiting for GitHub setup merge.",
            retryable=outcome.get("retryable", code == "github_temporarily_unavailable"))
    return {"status": "needs_user", "user_action": action}


def _setup_preview_proven_for_review(run, head_sha):
    """True once a pending setup's exact preview proof has passed for its branch head.

    Automatic approval can still be ineligible (a platform-deployed preview skips the
    local build, or the verified source moved). Waiting longer cannot change that.
    """
    result = run.result if isinstance(run.result, dict) else {}
    setup = result.get("article_system_setup") or {}
    gates = result.get("directory_quality_gates") or {}
    proof = result.get("directory_quality_verification") or {}
    preview = result.get("live_preview") or result.get("livePreview") or {}
    if not all(isinstance(item, dict) for item in (setup, gates, proof, preview)):
        return False
    return bool(
        head_sha
        and run.workflow == "article_system_setup"
        and run.status in {"awaiting_approval", "approval_required"} and run.approval_state == "approval_required"
        and result.get("status") == "preview_ready" and setup.get("status") == "preview_ready"
        and setup.get("setup_run_id") == run.run_id
        and result.get("branch_commit_sha") == head_sha and setup.get("branch_commit_sha") == head_sha
        and preview.get("exactRender") is True and preview.get("commitSha") == head_sha
        and proof.get("status") == "passed" and proof.get("commit_sha") == head_sha
        and proof.get("preview_identity") == f"commit:{head_sha}" and proof.get("completed_at")
        and gates.get("status") == "passed"
    )


def approve_prepared_setup(run, context, config, *, user, goal=None):
    """Use the existing setup control only after exact automatic preview proof."""
    from .website_connections import _verified_setup_run_base, scoped_run_contract, verify_repository_head
    from .vibe_marketing_views import VibeMarketingRunControlView
    website = config.website_connection
    preview = (run.result or {}).get("live_preview") or {}
    review = {"status": "needs_user", "user_action": {"id": "review_setup", "label": "Review setup",
        "url": preview.get("url") or (run.result or {}).get("preview_url"), "run_id": run.run_id}}
    if config.requires_review:
        return review
    from .website_hosted_build import enabled as hosted_enabled, current_scan_source
    from .website_setup_consent import valid as valid_consent
    from .website_goal import scope_matches, scope_for, mapping
    hosted = hosted_enabled(website)
    if hosted and goal is not None:
        if not valid_consent(goal) or not scope_matches(mapping(goal).get("scope"), scope_for(website, context.company.pk)):
            raise WebsiteAuthorityError("website_operation_changed", "Setup approval requires the original owner choices for this website.")
        if goal["consent"]["answers"]["publish_mode"] == "review":
            return review
    if hosted and current_scan_source(website) != mapping(run.run_request).get("expected_source_sha"):
        raise WebsiteAuthorityError("website_source_changed", "The setup source changed. Refresh its scan and preview before approval.")
    head = str((run.result or {}).get("branch_commit_sha") or "")
    base = _verified_setup_run_base(run, head, website, automatic_approval=True)
    if not base:
        if hosted and goal is not None and _setup_preview_proven_for_review(run, head):
            from .website_hosted_build import partial_environment
            if partial_environment(run, preview, head, website):
                error = WebsiteAuthorityError("prepare_no_progress", "The hosted build identity is incomplete. Trying the next articles strategy.", retryable=False)
                error.findings = [{"constraint": "incomplete_environment_identity"}]
                raise error
        if _setup_preview_proven_for_review(run, head) and not (hosted and goal is not None):
            return review
        raise WebsiteAuthorityError("setup_preview_verification_required", "Waiting for exact setup preview proof.", retryable=True)
    verify_repository_head(website, base)
    data = {**scoped_run_contract(run), "company_id": str(context.company.pk), "configuration_revision": website.configuration_version}
    request = SimpleNamespace(method="POST", user=user, data=data, query_params={}, headers={})
    response = VibeMarketingRunControlView().post(request, run.run_id, "approve")
    if response.status_code >= 400:
        raise WebsiteAuthorityError(response.data.get("code") or "setup_approval_unavailable",
            response.data.get("detail") or "Setup approval could not finish. It will retry automatically.",
            retryable=response.status_code >= 500 or response.status_code == 409)
    return {"status": "pending"}
