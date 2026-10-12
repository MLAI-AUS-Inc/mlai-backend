"""Persisted, bounded orchestration of the existing website readiness journey."""
import logging
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .website_connections import authority_guard, contract_for
from .website_contract import WebsiteAuthorityError, evidence_digest, connection_contract
from .website_journey import journey_for_context
from .website_models import WebsiteConnectionOperation

logger = logging.getLogger(__name__)

RETRY_MINUTES = (1, 2, 5, 10)
DEPLOYMENT_RETRY_MINUTES = (2, 5, 10, 20, 40)
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


def start_prepare(config, *, expected=None, company_id=None, user=None, retry=False):
    """Reuse one prepare receipt for the current consent generation."""
    connection = config.website_connection
    if connection is None:
        raise WebsiteAuthorityError("website_connection_required", "Select and authorize your website repository first.")
    binding = {**(contract_for(connection) if expected is None else expected)}
    if getattr(connection, "organization", None):
        binding.update(domain=connection.organization.domain, github_repo=connection.github_repo)
    with authority_guard(binding, action="read") as current:
        op, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"{current.pk}:prepare:{current.generation}", defaults={
                "connection": current, "generation": current.generation, "action": "prepare",
                "payload": {"company_id": str(company_id or ""), "requested_by_user_id": str(getattr(user, "pk", "") or ""), "attempt": 1, "user_clicks": 1},
                "receipt": {"status": "working", "current_step": "verify-access", "message": "Preparing your website for articles."}})
        if op.action != "prepare" or op.generation != current.generation:
            raise WebsiteAuthorityError("operation_key_conflict", "The saved preparation belongs to another website.")
        if not created and retry and op.state in {"needs_attention", "needs_user", "cancelled"}:
            reset_child = op.state in {"cancelled", "needs_attention"}
            op.state, op.next_attempt_at = "pending", None
            op.receipt = {**op.receipt, "step_failures": {}, "status": "working", "user_action": None, "last_completed": None,
                "child_run_id": None if reset_child else op.receipt.get("child_run_id"), "reason_code": "", "blocked_step": None}
            op.payload = {**op.payload, "attempt": op.payload.get("attempt", 1) + 1, "user_clicks": op.payload.get("user_clicks", 1) + 1}
            if reset_child:
                op.payload.pop("run_id", None)
            op.save(update_fields=["state", "next_attempt_at", "receipt", "payload", "updated_at"])
    return op


def _claim_prepare(identifier, now):
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
            pk=getattr(identifier, "pk", identifier), action="prepare", state__in=["pending", "running"]).first()
        if op is None or op.next_attempt_at and op.next_attempt_at > now:
            return None
        if op.generation != op.connection.generation or op.connection.state != "connected":
            op.state = "cancelled"
            op.receipt = {**op.receipt, "status": "authority_changed", "message": "Website access changed."}
            op.save(update_fields=["state", "receipt", "updated_at"])
            return None
        op.state, op.attempts = "running", op.attempts + 1
        op.next_attempt_at = now + timedelta(minutes=5)
        op.save(update_fields=["state", "attempts", "next_attempt_at", "updated_at"])
        op._claimed_at = op.updated_at
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
    # Compare the lease and generation after remote work. A disconnect always wins.
    WebsiteConnectionOperation.objects.filter(pk=op.pk, action="prepare", state="running", attempts=op.attempts,
        updated_at=op._claimed_at, connection__generation=op.generation, connection__state="connected").update(
            state=op.state, receipt=op.receipt, payload=op.payload, next_attempt_at=op.next_attempt_at, updated_at=now)
    return op


def _finish_prepare(op, *, now):
    logger.info("website_prepare operation=%s company=%s connection=%s generation=%s step=%s state=%s reason=%s attempt=%s next_attempt=%s",
        op.pk, op.payload.get("company_id"), op.connection_id, op.generation, op.receipt.get("current_step"),
        op.state, op.receipt.get("reason_code", ""), op.attempts, op.next_attempt_at)
    return _persist_prepare(op, now=now)


def _permission_action(connection, *, grants=None):
    from .website_github_permissions import installation_grants
    grants = grants or installation_grants(connection)
    return {"id": "review_github_permissions", "label": "Review GitHub permissions", "url": grants["settingsUrl"]}


def prepare_failure(op, step, error, *, now=None):
    """Classify failures without asking the founder to repeat retryable work."""
    now = now or timezone.now()
    code = getattr(error, "code", "website_prepare_unavailable")
    message = str(error) or "Website preparation is temporarily unavailable."
    failures = dict(op.receipt.get("step_failures") or {})
    step = str(step or op.receipt.get("current_step") or "prepare")
    permission_needed = step in PERMISSION_STEPS.get(code, ())
    failures[step] = failures.get(step, 0) + 1
    op.receipt = {**op.receipt, "step_failures": failures, "reason_code": code, "message": message,
        "retryable": bool(getattr(error, "retryable", True))}
    if getattr(error, "user_action", None) and (code not in PERMISSION_CODES or permission_needed):
        op.state, op.next_attempt_at = "needs_user", None
        op.receipt.update(status="needs_you", user_action=error.user_action)
    elif permission_needed:
        op.state, op.next_attempt_at = "needs_user", None
        op.receipt.update(status="needs_you", user_action=_permission_action(op.connection))
    elif code in AUTHORIZATION_CODES and code not in PERMISSION_CODES:
        op.state = "needs_user"
        op.receipt.update(status="needs_you", user_action={"id": "authorize_github", "label": "Authorize GitHub"})
        op.next_attempt_at = None
    elif code in DEPLOYMENT_WAIT_CODES or step in {"verify", "ci-attestation"} and getattr(error, "retryable", False):
        count = op.receipt.get("deployment_attempts", 0) + 1
        op.receipt["deployment_attempts"] = count
        op.state = "pending" if count <= len(DEPLOYMENT_RETRY_MINUTES) else "needs_attention"
        op.receipt["status"] = "waiting_for_deployment" if op.state == "pending" else "needs_attention"
        op.next_attempt_at = now + timedelta(minutes=DEPLOYMENT_RETRY_MINUTES[min(count - 1, 4)]) if op.state == "pending" else None
    elif failures[step] >= 3 or not getattr(error, "retryable", True):
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="needs_attention", user_action={"id": "retry", "label": "Try again"})
    else:
        op.state = "pending"
        op.next_attempt_at = now + timedelta(minutes=RETRY_MINUTES[min(failures[step] - 1, 3)])
        op.receipt["status"] = "working"
    from .website_operations import prepare_receipt
    op.receipt = prepare_receipt(op)


def _setup_surface(config, journey):
    """Respect a saved owner choice, then use the current scan's listing route."""
    pending = (config.article_system or {}).get("pending_article_system_setup") or {}
    if pending.get("mode") in {"existing", "none"} and pending.get("route_path"):
        return pending["mode"], pending["route_path"]
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
        "idempotency_key": f"prepare:{op.pk}:{step}:{op.payload.get('attempt', 1)}",
        "force_refresh": True}
    if step == "setup":
        mode, surface = _setup_surface(config, journey or journey_for_context(context, config))
        pending = (config.article_system or {}).get("pending_article_system_setup") or {}
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
    if not identifier or pending.get("mode") != "existing" or route not in candidates:
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
    if (not isinstance(hint, dict) or hint.get("route_path") != route
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
        proof = discover_source_attestation(website, target, website.verified_sha)
        ci = record_ci_attestation(proof)
        receipt = ci.receipt
    if step == "verify":
        deployment = verify_live_deployment(config, data=receipt)
        return {"status": "completed", "operation_id": str(deployment.pk)}
    return {"status": "completed", "operation_id": str(ci.pk)}


def advance_prepare(identifier, *, now=None, handlers=None):
    """Advance a single leased operation by its next unmet readiness requirement."""
    now = now or timezone.now()
    op = _claim_prepare(identifier, now)
    if op is None:
        return None
    step = op.receipt.get("current_step") or "verify-access"
    try:
        context, config, user = _prepare_context(op)
        journey = journey_for_context(context, config)
        if journey.get("capabilities", {}).get("canPublishArticle"):
            op.state, op.next_attempt_at = "completed", None
            op.receipt.update(status="ready", message="Your website is ready for articles.", current_step="ready", user_action=None)
            return _finish_prepare(op, now=now)
        child_id = op.receipt.get("child_run_id")
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
        if child_id:
            from workflow_runs.models import ContentFactoryRun
            child = child or ContentFactoryRun.objects.filter(run_id=child_id, organization_id=op.connection.organization_id).first()
            setup = ((child.result or {}).get("article_system_setup") or {}) if child else {}
            setup_review_ready = bool(child and child.workflow == "article_system_setup" and
                child.status in {"needs_review", "awaiting_approval", "approval_required", "pr_opened", "publish_bundle_ready"} and
                setup.get("status") in {"preview_ready", "pr_created", "completed", "ready", "adopted_existing_surface", "adopted"})
            if child and not setup_review_ready and child.status not in {"completed", "failed", "blocked", "cancelled", "denied"}:
                op.state, op.next_attempt_at = "pending", now + timedelta(minutes=1)
                return _finish_prepare(op, now=now)
            if child and child.status in {"failed", "blocked", "cancelled", "denied"}:
                code = (child.result or {}).get("error_code") or "website_prepare_step_failed"
                result = child.result or {}
                retryable = result.get("retryable") is True or code in {"worker_unavailable", "github_unavailable", "HOSTED_PREVIEW_UNAVAILABLE"}
                message = getattr(child, "error", "") or result.get("error") or result.get("message") or "The website step could not finish."
                if retryable or code in AUTHORIZATION_CODES:
                    op.receipt.pop("child_run_id", None)
                    op.payload = {**op.payload, "attempt": op.payload.get("attempt", 1) + 1}
                    raise WebsiteAuthorityError(code, message, retryable=retryable)
                prepare_failure(op, step, WebsiteAuthorityError(code, message, retryable=False), now=now)
                op.receipt["user_action"] = {"id": "open_run", "label": "Open setup run", "run_id": child_id}
                return _finish_prepare(op, now=now)
            if child and child.workflow == "article_system_setup":
                merge = maybe_merge_prepared_setup(child, context, config, user=user)
                if merge.get("status") == "needs_user":
                    op.state, op.next_attempt_at = "needs_user", None
                    op.receipt.update(status="needs_you", current_step="merge", message="Merge the articles setup pull request.",
                        user_action=merge["user_action"])
                    return _finish_prepare(op, now=now)
                if merge.get("status") == "pending":
                    op.state, op.next_attempt_at = "pending", now + timedelta(minutes=2)
                    return _finish_prepare(op, now=now)
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
            digest = evidence_digest({"prerequisites": journey.get("prerequisites"), "capabilities": journey.get("capabilities")})
            if op.receipt.get("last_completed") == {"step": step, "facts_digest": digest}:
                raise WebsiteAuthorityError("prepare_no_progress", blocked.get("reason") or next_action.get("label") or "Website preparation needs attention.", retryable=False)
            op.receipt.update(current_step=step, blocked_step=None, reason_code="", retryable=False,
                status="working", message=f"Preparing articles: {step.replace('-', ' ')}.", user_action=None)
            handler = (handlers or {}).get(step)
            outcome = handler(op, context, config, user=user) if handler else _dispatch_prepare_step(op, context, config, step, user=user, journey=journey)
            run_id = (outcome or {}).get("run_id") or (outcome or {}).get("runId")
            if run_id:
                op.receipt.update(child_run_id=run_id, **{f"{step}_run_id": run_id})
                op.payload = {**op.payload, "run_id": run_id}
                break
            steps = list(op.receipt.get("completed_steps") or [])
            if step not in steps:
                steps.append(step)
            op.receipt["completed_steps"] = steps
            forced = (outcome or {}).get("capabilities") if step == "verify-access" else None
            if forced is not None:
                journey = journey_for_context(context, config, capabilities=forced)
                digest = evidence_digest({"prerequisites": journey.get("prerequisites"), "capabilities": journey.get("capabilities")})
            if journey.get("capabilities", {}).get("canPublishArticle"):
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
        prepare_failure(op, step, WebsiteAuthorityError("website_prepare_unavailable",
            "Website preparation is temporarily unavailable. It will retry automatically.", retryable=True), now=now)
    return _finish_prepare(op, now=now)


def advance_prepares_for_connection(connection_id):
    """Wake current preparation after scan, setup or CI callbacks commit."""
    current = WebsiteConnectionOperation.objects.filter(connection_id=connection_id, action="prepare",
        state__in=["pending", "needs_user"], connection__generation=F("generation"))
    current.update(state="pending", next_attempt_at=timezone.now(), updated_at=timezone.now())
    return current.count()


def wake_prepare_after_commit(connection_id):
    """Wake the scheduler only once the callback's authority transaction commits."""
    transaction.on_commit(lambda: advance_prepares_for_connection(connection_id), robust=True)


def wake_prepare_for_source(connection_id, *, organization_id):
    """Source changes reopen the current operation without granting new consent."""
    from .website_models import WebsiteConnection
    from .models import OrganizationContentConfig
    from founder_tools.models import VibeRaisingCompany
    connection = WebsiteConnection.objects.select_related("organization", "authorized_by").filter(
        pk=connection_id, organization_id=organization_id, state="connected").first()
    if connection is None or connection.authorized_by is None:
        return None
    config = OrganizationContentConfig.objects.filter(website_connection=connection).first()
    company = VibeRaisingCompany.objects.filter(organization_id=organization_id,
        profile__user_id=connection.authorized_by_id).first()
    if config is None or company is None:
        return None
    op = start_prepare(config, company_id=company.pk, user=connection.authorized_by)
    WebsiteConnectionOperation.objects.filter(pk=op.pk, generation=connection.generation,
        state__in=["completed", "needs_user", "pending"]).update(state="pending", next_attempt_at=timezone.now(),
            receipt={**op.receipt, "status": "working", "current_step": "verify", "deployment_attempts": 0,
                "step_failures": {}, "user_action": None}, updated_at=timezone.now())
    connection.operations.filter(generation=connection.generation, action="source-reverify", state="pending").update(
        state="completed", receipt={"status": "managed_by_prepare", "prepare_operation_id": str(op.pk),
            "repository_modified": False}, updated_at=timezone.now())
    return op


def maybe_merge_prepared_setup(run, context, config, *, user):
    """Auto-merge only after exact preview proof and confirmed absence of reviewers."""
    from urllib.parse import quote
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import validate_setup_merge_source, require_unlocked_remote_call
    from .vibe_marketing_views import _pull_request_number_from_run, _merge_setup_pr_for_run
    setup = (run.result or {}).get("article_system_setup") or {}
    number = _pull_request_number_from_run(run)
    if setup.get("merge_status") == "merged" or (run.result or {}).get("merge_status") == "merged":
        return {"status": "completed"}
    if not number:
        if run.status == "completed" and (run.result or {}).get("adopted") is True:
            return {"status": "completed"}
        return approve_prepared_setup(run, context, config, user=user)
    website = config.website_connection
    action = {"id": "merge_pr", "label": f"Merge PR #{number}", "pr_number": number,
        "url": f"https://github.com/{website.github_repo}/pull/{number}"}
    if config.requires_review:
        return {"status": "needs_user", "user_action": action}
    credential = None
    headers = {}
    try:
        binding = {**contract_for(website), "domain": website.organization.domain, "github_repo": website.github_repo}
        with authority_guard(binding, action="setup"):
            pass
        require_unlocked_remote_call()
        credential = create_installation_access_token(installation_id=website.installation_id,
            repository=website.github_repo, repository_id=website.repository_id, permission_mode="read", use_cache=False)
        headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
        base = f"https://api.github.com/repos/{website.github_repo}"
        pull_response = http_client.get(f"{base}/pulls/{number}", headers=headers, timeout=(3, 15))
        pull_response.raise_for_status()
        pull = pull_response.json()
        if pull.get("merged"):
            return {"status": "completed"}
        head = (pull.get("head") or {}).get("sha")
        validate_setup_merge_source(run, head)
        rules = http_client.get(f"{base}/rules/branches/{quote(website.branch, safe='')}", headers=headers, timeout=(3, 15))
        rules.raise_for_status()
        inventory = rules.json()
        if not isinstance(inventory, list) or any(rule.get("type") == "pull_request" and
                (rule.get("parameters") or {}).get("required_approving_review_count", 0) > 0 for rule in inventory):
            return {"status": "needs_user", "user_action": action}
        protection = http_client.get(f"{base}/branches/{quote(website.branch, safe='')}/protection", headers=headers, timeout=(3, 15))
        if protection.status_code != 404:
            protection.raise_for_status()
            review = protection.json().get("required_pull_request_reviews") or {}
            if review.get("required_approving_review_count", 0) > 0:
                return {"status": "needs_user", "user_action": action}
        with authority_guard(binding, action="setup"):
            pass
        _, error = _merge_setup_pr_for_run(run=run, context=context)
        return {"status": "pending"} if error is None else {"status": "needs_user", "user_action": action}
    except WebsiteAuthorityError as exc:
        if exc.code in AUTHORIZATION_CODES:
            raise
        # Unproven preview/source is retried before requesting a founder action.
        raise WebsiteAuthorityError("setup_preview_verification_required", "Waiting for exact setup preview proof.", retryable=True) from exc
    except Exception:
        # Ambiguous provider policy must never be treated as 'zero reviewers'.
        return {"status": "needs_user", "user_action": action}
    finally:
        if credential:
            try:
                http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
            except Exception:
                pass


def approve_prepared_setup(run, context, config, *, user):
    """Use the existing setup control only after exact automatic preview proof."""
    from .website_connections import _verified_setup_run_base, scoped_run_contract, verify_repository_head
    from .vibe_marketing_views import VibeMarketingRunControlView
    website = config.website_connection
    preview = (run.result or {}).get("live_preview") or {}
    if config.requires_review:
        return {"status": "needs_user", "user_action": {"id": "review_setup", "label": "Review setup",
            "url": preview.get("url") or (run.result or {}).get("preview_url"), "run_id": run.run_id}}
    head = str((run.result or {}).get("branch_commit_sha") or "")
    base = _verified_setup_run_base(run, head, website, automatic_approval=True)
    if not base:
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
