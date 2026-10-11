"""Founder-owned article operations shared with the MLAI MCP transport.

The MCP adapter never dispatches an HTTP view or obtains provider credentials.
Paid work uses the canonical charge, admission and durable dispatch operations.
Publication can only continue an exact draft already approved in MLAI Chat.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from urllib.parse import quote, urlencode
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response

from founder_tools.services import FounderCompanyContext
from . import vibe_marketing_views as views
from .article_export import article_export
from .article_publish_approval import (
    RECEIPT_KEY, article_publish_approval_receipt_matches, article_review_identity,
    article_review_identity_is_complete,
)
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_connections import authority_guard, owner_operation_scope, scoped_run_contract
from .website_contract import WebsiteAuthorityError, connection_contract


class ArticleOperationError(APIException):
    """Preserve a canonical article refusal as structured MCP error data."""

    status_code = 409
    default_code = "article_operation_unavailable"

    def __init__(self, payload, status_code=409):
        self.status_code = status_code
        super().__init__(payload)


def result_payload(result):
    """Unwrap domain responses without dropping price or setup refusal details."""
    if isinstance(result, Response):
        if result.status_code >= 400:
            raise ArticleOperationError(result.data, result.status_code)
        return result.data
    if not isinstance(result, dict):
        raise ArticleOperationError({"detail": "Article service returned an invalid result."}, 502)
    return result


def context_for(user, company):
    """Require the existing founder account; never create or switch a profile."""
    profile = company.profile
    if profile.user_id != user.pk or profile.role != profile.ROLE_FOUNDER:
        raise PermissionDenied("Only the owning founder can access startup articles.")
    return FounderCompanyContext(profile=profile, company=company, organization=company.organization)


def review_url(company, run_id=None):
    """Build the private owned Articles destination without capability credentials."""
    query = {"company_id": str(company.pk)}
    path = "/my-startup/articles"
    if run_id:
        path = "/my-startup/runs/" + quote(run_id, safe="")
        query.update(articleStep="review", reviewMode="expanded")
    return settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + path + "?" + urlencode(query)


def capabilities(user, company):
    """Return current setup, approved editorial choices and Roo prices."""
    context = context_for(user, company)
    config = views._get_config(context.organization)
    from .editorial_catalog import catalog_payload
    from .website_connections import contract_for
    website = connection_contract(contract_for(config.website_connection)) if config.website_connection else None
    if website and config.default_publish_target_id:
        website["connection_target_id"] = config.default_publish_target_id
    return {
        "companyId": str(company.pk),
        "articleCapabilities": views._article_capabilities_for_context(context, config),
        "websiteConnection": website,
        "editorialCatalog": catalog_payload(config.pillar_strategy),
        "reviewUrl": review_url(company),
    }


def account(user, company):
    """Expose account-owned connection readiness, never tokens or provider data."""
    from integrations.services.external_connectors import serialize_source_status
    from community_chat.startups.lifecycle import source_capabilities
    from community_chat.startups.website_connections import website_connection_sources
    context = context_for(user, company)
    state = serialize_source_status(user, organization=context.organization)
    sources = [source_capabilities(item) for item in state.get("sources", [])]
    sources.extend(website_connection_sources(user, company))
    allowed = ("provider", "key", "label", "name", "status", "configured", "available",
               "enabled", "usableForUpdates", "canConnect", "warning", "capability")
    return {
        "companyId": str(company.pk), "startup": {"name": company.name, "domain": context.organization.domain},
        "account": {"id": user.pk}, "rooBalancePoints": views._roo_points_balance_for_user(user),
        "connections": [{key: item[key] for key in allowed if key in item} for item in sources],
        **capabilities(user, company),
        "connectionsUrl": settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + "/my-startup/connections?" + urlencode({"company_id": str(company.pk)}),
    }


def owned_run(context, run_id, *, articles_only=False):
    """Resolve a persisted run and its domain/company binding before remote reads."""
    run = views.ContentFactoryRun.objects.prefetch_related("steps").filter(run_id=run_id).first()
    if run is None:
        run = (views.ContentFactoryRun.objects.prefetch_related("steps")
               .filter(run_request__client_request_id=run_id).order_by("-id").first())
    permitted = views.ARTICLE_WORKFLOWS if articles_only else views.ARTICLE_WORKFLOWS | views.DISCOVERY_WORKFLOWS
    if run is None or run.workflow not in permitted or not views._run_belongs_to_context(run, context):
        raise PermissionDenied("This article run is unavailable to the selected startup.")
    return run


def list_articles(user, company, *, limit=20):
    """List private drafts and article records with authoritative publishing state."""
    context = context_for(user, company)
    return {"companyId": str(company.pk),
            "drafts": views._recent_article_drafts(context.organization, limit=limit),
            "articles": views._recent_written_topics(context.organization, limit=limit),
            "reviewUrl": review_url(company)}


def run_status(user, company, run_id):
    """Read worker progress without mutating a user draft or approval state."""
    context = context_for(user, company)
    try:
        run = owned_run(context, run_id)
    except PermissionDenied:
        receipt = _owned_dispatch(user, company, run_id)
        if receipt is None:
            raise
        return _dispatch_payload(receipt, company, replayed=False)
    remote = views._call_content_factory_run_status(run.run_id, workflow=run.workflow)
    if remote and not views._is_status_poll_unavailable_payload(remote):
        if str(remote.get("run_id") or remote.get("job_id") or run.run_id) != run.run_id:
            raise ArticleOperationError({"detail": "The worker returned another run."}, 502)
        with views._remote_response_write_guard(remote, workflow=run.workflow):
            pass
    else:
        remote = {}
    state = views._serialize_run(run, context=context, mode="status")
    topic_result = views._run_result_from_remote(remote) if remote else (run.result or {})
    return {"companyId": str(company.pk), "runId": run.run_id, "run": state,
            "workerStatus": {key: remote[key] for key in ("status", "current_step", "progress", "approval_state") if key in remote},
            "topicCandidates": views._extract_topic_candidates_from_result(topic_result),
            "reviewUrl": review_url(company, run.run_id)}


def draft(user, company, run_id):
    """Read only the exact canonical markdown snapshot and current review fields."""
    from .article_review_views import remote_review
    context = context_for(user, company)
    run = owned_run(context, run_id, articles_only=True)
    latest = views._latest_review_ready_component_revision(run, context)
    snapshot = result_payload(remote_review(run))
    return {"companyId": str(company.pk), "runId": run.run_id,
            "revision": snapshot.get("revision"), "previewPending": snapshot.get("previewPending"),
            "fields": snapshot.get("fields", []),
            "articleExport": article_export(snapshot, run_id=run.run_id, latest_run_id=latest.run_id if latest else None),
            "latestRunId": latest.run_id if latest else None, "reviewUrl": review_url(company, run.run_id)}


@contextmanager
def _request_lock(key):
    """Serialise a paid MCP retry before consulting the durable operation receipt."""
    name = "mlai-mcp-article-request:" + hashlib.sha256(key.encode()).hexdigest()
    token = secrets.token_urlsafe(24)
    if not cache.add(name, token, timeout=180):
        raise ArticleOperationError({"detail": "This request is in progress. Retry with the same idempotencyKey.",
                                     "code": "article_request_in_progress", "retryable": True})
    try:
        yield
    finally:
        if cache.get(name) == token:
            cache.delete(name)


def _paid_identity(user, company, key, kind):
    key = str(UUID(key))
    return "mlai-mcp-" + kind + ":" + hashlib.sha256(f"{user.pk}:{company.pk}:{key}".encode()).hexdigest()


def _owned_dispatch(user, company, request_id):
    """Read only an article dispatch reserved for this owner and startup."""
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=request_id, organization=company.organization).first()
    request = ((row.payload or {}).get("request") or {}) if row else {}
    if row is None or request.get("roo_points_requested_by_user_id") != user.pk or not request.get("mcp_request_hash"):
        return None
    return row


def _dispatch_payload(row, company, *, replayed):
    """Expose a provisional durable start while its worker/outbox resolves delivery."""
    run_id = row.run_id or row.client_request_id
    return {"companyId": str(company.pk), "runId": run_id, "status": row.state,
            "dispatchPending": row.state in {"pending", "delivering", "awaiting_resolution", "refund_pending"},
            "replayed": replayed, "reviewUrl": review_url(company, run_id)}


def start(user, company, arguments, *, research=False):
    """Reuse one scoped paid run, preserving quote, billing and dispatch fences."""
    context = context_for(user, company)
    arguments = {**arguments, "companyId": str(company.pk), "idempotencyKey": str(UUID(arguments["idempotencyKey"]))}
    if not research:
        arguments["websiteConnection"] = connection_contract(arguments["websiteConnection"])
    request_id = _paid_identity(user, company, arguments["idempotencyKey"], "research" if research else "article")
    request_hash = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # The canonical research service additionally binds its input to payer/startup.
    stored_key = "vibe-research:" + hashlib.sha256(f"{user.pk}:{company.pk}:{request_id}".encode()).hexdigest() if research else request_id
    with _request_lock(request_id):
        existing = views.ContentFactoryRun.objects.filter(run_request__client_request_id=stored_key).order_by("-id").first()
        if existing is not None:
            if not views._run_belongs_to_context(existing, context) or (existing.run_request or {}).get("mcp_request_hash") != request_hash:
                raise ArticleOperationError({"detail": "This idempotencyKey was already used for a different article request.", "code": "article_request_conflict"})
            return {"companyId": str(company.pk), **views._run_start_payload(existing), "replayed": True,
                    "reviewUrl": review_url(company, existing.run_id)}
        dispatch = _owned_dispatch(user, company, stored_key)
        if dispatch is not None:
            if dispatch.payload["request"]["mcp_request_hash"] != request_hash:
                raise ArticleOperationError({"detail": "This idempotencyKey was already used for different article inputs.", "code": "article_request_conflict"})
            # A crash between charge and local-run persistence must reuse the
            # original durable research/article request and its input hash.
            return _dispatch_payload(dispatch, company, replayed=True)
        data = {key: value for key, value in arguments.items() if key not in {"companyId", "idempotencyKey", "websiteConnection"}}
        data["clientRequestId"] = request_id
        if research:
            data.update(customTopicTitle=data.pop("topic", ""), customTopicContext=data.pop("context", ""))
            result = views.start_article_research(user=user, context=context, data=data, request_hash=request_hash)
        else:
            expected = connection_contract(arguments["websiteConnection"])
            if not expected:
                raise ValidationError("Use the reviewed websiteConnection returned by get_article_capabilities.")
            config = views._get_config(context.organization)
            if expected.get("connection_target_id") != config.default_publish_target_id:
                raise WebsiteAuthorityError("website_target_changed", "The article destination changed. Reload article capabilities.")
            payload = {**expected, "domain": context.organization.domain, "github_repo": config.github_repo}
            with authority_guard(payload, action="read"):
                pass
            data.update(expected)
            with owner_operation_scope(payload):
                result = views.start_article_generation(user=user, context=context, data=data, request_hash=request_hash, website_contract=expected)
        response = result_payload(result)
        run_id = response.get("runId") or response.get("run_id")
        return {"companyId": str(company.pk), **response, "replayed": False, "reviewUrl": review_url(company, run_id)}


def revise(user, company, arguments):
    """Apply deterministic exact-revision edits with canonical replay protection."""
    from .article_review_views import apply_article_review_update
    context = context_for(user, company)
    run = owned_run(context, arguments["runId"], articles_only=True)
    if views._run_has_external_publish_evidence(run):
        raise ArticleOperationError({"detail": "This article has entered publication. Create a new draft revision."})
    payload = {"action": "applyUpdate", "operationId": str(UUID(arguments["idempotencyKey"])),
               "expectedRevision": arguments["expectedRevision"], "textEdits": arguments["textEdits"], "commentIds": []}
    result = result_payload(apply_article_review_update(user=user, context=context, run=run, data=payload))
    return {"companyId": str(company.pk), "runId": run.run_id, **result, "reviewUrl": review_url(company, run.run_id)}


def publication(user, company, run_id):
    """Describe the exact destination and persisted founder approval requirement."""
    context = context_for(user, company)
    run = owned_run(context, run_id, articles_only=True)
    config = views._get_config(context.organization)
    ready = article_publish_approval_receipt_matches(run)
    latest = views._latest_review_ready_component_revision(run, context)
    original = scoped_run_contract(run)
    approved_target = original.get("connection_target_id") or original.get("publish_target_id")
    if ready and not approved_target:
        child = views._local_publish_child_for_run(run, context=context)
        if child is not None and views._run_belongs_to_context(child, context):
            child_request = child.run_request or {}
            if child_request.get("source_run_id") == run.run_id:
                approved_target = child_request.get("publish_target_id") or child_request.get("connection_target_id")
    destination_matches = bool(approved_target and approved_target == config.default_publish_target_id
                               and run.github_repo.casefold() == config.github_repo.casefold())
    return {"companyId": str(company.pk), "runId": run.run_id,
            "reviewIdentity": article_review_identity(run),
            "reviewIdentityComplete": article_review_identity_is_complete(run),
            "founderApproved": ready and latest is None and destination_matches,
            "approvedPublishTargetId": approved_target,
            "destination": {"domain": context.organization.domain, "githubRepo": config.github_repo,
                            "publishTargetId": config.default_publish_target_id},
            "websiteConnection": connection_contract(original),
            "reviewUrl": review_url(company, run.run_id),
            "instructions": "The founder must review and approve this exact hosted draft in MLAI Chat. An agent confirmation cannot grant approval."}


def publish(user, company, arguments):
    """Continue publication only under an existing exact founder approval receipt."""
    context = context_for(user, company)
    run = owned_run(context, arguments["runId"], articles_only=True)
    state = publication(user, company, run.run_id)
    if not state["founderApproved"] or arguments["reviewIdentity"] != state["reviewIdentity"]:
        raise ArticleOperationError({"detail": "The founder must approve this exact article in MLAI Chat before publication.",
                                     "code": "article_founder_review_required", "reviewUrl": state["reviewUrl"]})
    receipt = (run.run_request or {}).get(RECEIPT_KEY) or {}
    actor = views.founder_actor_id_for_user(user) or str(user.pk)
    if receipt.get("actor_id") != actor:
        raise PermissionDenied("This article approval belongs to another founder account.")
    if arguments["destination"] != state["destination"]:
        raise ArticleOperationError({"detail": "The publication destination changed. Review it again.", "code": "article_destination_changed"})
    original = scoped_run_contract(run)
    with authority_guard(original, action="publish"):
        pass
    # This action reuses the durable approved publish handoff; it never issues approve.
    payload = {"clientRequestId": _paid_identity(user, company, arguments["idempotencyKey"], "publish"),
               "reviewedRevision": (run.result or {}).get("article_review_approval", {}).get("revision"),
               "auto_merge": False, "request_source": "mlai_mcp", **connection_contract(original)}
    with owner_operation_scope(original):
        result = views.control_article_run(user=user, context=context, run=run, action="publish-pr", data=payload)
    return {"companyId": str(company.pk), **result_payload(result), "reviewUrl": state["reviewUrl"]}
