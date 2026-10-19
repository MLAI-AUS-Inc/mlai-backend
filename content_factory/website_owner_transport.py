"""Observe authenticated owner intent before entering mutation transactions.

These are server-owned HTTP/MCP contexts. JSON fields never select an origin or
waive observation. Complete source coverage is distinct from an independently
verified request/provider observation window.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import inspect
import json
import re
from uuid import uuid4

from django.db import transaction
from django.http import HttpRequest
from rest_framework.request import Request
from rest_framework.views import APIView
from rest_framework.exceptions import APIException

from . import website_goal as goals

CURRENT = ContextVar("articles_owner_transport", default=None)
VERSION = 1
MCP_WRITES = {"research_article_topics", "generate_article", "revise_article_draft", "publish_article"}
MAX_BYTES = 1024 * 1024


class OwnerAuditRequired(APIException):
    """A qualified sandbox cannot mutate before its intent is durably recorded."""
    status_code = 503
    default_detail = "The original articles audit could not record this action. Retry after observation recovers."
    default_code = "articles_owner_audit_required"


def required(organization_id):
    """Apply only to explicitly selected sandbox organizations and goal rollout."""
    from django.conf import settings
    from .website_goal_audit import enabled
    values = getattr(settings, "ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS", ())
    if isinstance(values, str):
        values = tuple(part.strip() for part in values.split(",") if part.strip())
    return bool(getattr(settings, "ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED", False)
        and isinstance(values, (list, tuple, set, frozenset))
        and str(organization_id) in {str(value) for value in values} and enabled(organization_id))


def require_admitted_goal(op):
    """Keep historical/unobserved goals out of strict sandbox execution."""
    if required(op.connection.organization_id):
        from .website_goal_audit import transport_coverage
        if transport_coverage(op)["coverage"] != "complete":
            from .website_contract import WebsiteAuthorityError
            raise WebsiteAuthorityError("website_prepare_blocked", "Start this audited setup through its authenticated owner transport.", status=503)


def _intent_identity(view, request, kwargs):
    """Use actual mutation replay contracts; generic headers prove no replay."""
    names = {base.__name__ for base in view.__class__.__mro__}
    data = dict(request.data)
    if "VibeMarketingArticleReviewView" in names:
        if data.get("action") == "refresh":
            return None
        from .website_review_action_audit import intent
        normalized = intent(data)
        if normalized is None:
            return None
        return normalized["operation"], normalized["fingerprint"]
    if "VibeMarketingRunControlView" in names and kwargs.get("action") in {"refresh", "refresh-setup-pr-status"}:
        return None
    if "VibeMarketingRunCommentsView" in names:
        replay = data.get("operationId") or data.get("operation_id")
        return (replay if isinstance(replay, str) and re.fullmatch(r"[\w-]{8,100}", replay) else str(uuid4())), None
    if "VibeMarketingRunCommentDetailView" in names:
        return str(uuid4()), None
    if names & {"WebsiteConnectionActionView", "VibeMarketingScanView", "VibeMarketingArticleSystemSetupView", "VibeMarketingArticleView"}:
        return (request.headers.get("Idempotency-Key") or data.get("idempotency_key") or data.get("client_request_id") or str(uuid4())), None
    return str(uuid4()), None


def descriptor(user, context, config, *, action, data, run_id=None, replay=None, origin):
    """Hash bounded inputs; do not retain article content or credential values."""
    if (origin not in {"http", "mcp"} or not getattr(user, "is_active", False)
            or not getattr(user, "is_authenticated", False) or user.pk is None
            or not isinstance(data, dict) or not isinstance(action, str) or not 1 <= len(action) <= 160):
        return None
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    identity = replay or str(uuid4())
    if len(raw) > MAX_BYTES or not isinstance(identity, str) or not 1 <= len(identity) <= 1024:
        return None
    return {"origin": origin, "actor_id": str(user.pk), "company_id": str(context.company.pk),
        "organization_id": str(context.organization.pk), "connection_id": str(config.website_connection_id),
        "action": action, "run_id": str(run_id or ""), "request_id": identity,
        "intent_digest": hashlib.sha256(raw).hexdigest()}


def event_id(observed, goal_id):
    """Derive one exact-intent identity shared by admission and route observation."""
    return goals.digest({"kind": "owner_transport_intent", "goal_id": str(goal_id), **observed})


def inventory():
    """Check the loaded route classes and supported MCP write caller inventory."""
    from django.urls import URLPattern, URLResolver, get_resolver
    from . import website_views, vibe_marketing_views, article_review_views
    from startup_updates.mcp import articles
    declared = [website_views, vibe_marketing_views, article_review_views]
    gaps, covered, wrappers = [], [], {}
    allowed_wrappers = {"community_chat.my_startup_views.MyStartupAccess.initial",
        "founder_tools.my_startup.api.MyStartupViewMixin.dispatch"}
    def check_chain(cls):
        for base in cls.__mro__:
            if base is ObservedOwnerAPIView:
                return
            for method in ("initial", "dispatch"):
                function = base.__dict__.get(method)
                if function is None:
                    continue
                original = inspect.unwrap(function)
                if original is getattr(ObservedOwnerAPIView, method):
                    continue
                name = base.__module__ + "." + base.__name__ + "." + method
                if name not in allowed_wrappers:
                    gaps.append("owner_http_dispatch_uncovered")
                else:
                    wrappers[name] = hashlib.sha256(original.__code__.co_code).hexdigest()
    for module in declared:
        for name, cls in vars(module).items():
            if not inspect.isclass(cls) or cls.__module__ != module.__name__ or not issubclass(cls, APIView):
                continue
            methods = [method for method in ("post", "put", "patch", "delete") if callable(getattr(cls, method, None))]
            if methods:
                covered.append(module.__name__ + "." + name + ":" + ",".join(methods))
                if not issubclass(cls, ObservedOwnerAPIView):
                    gaps.append("owner_http_class_uncovered")
                else:
                    check_chain(cls)
    def walk(patterns):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern.url_patterns)
            elif isinstance(pattern, URLPattern):
                yield getattr(pattern.callback, "view_class", None)
    known = {item.__name__ for module in declared for item in vars(module).values()
        if inspect.isclass(item) and item.__module__ == module.__name__ and issubclass(item, APIView)}
    for cls in walk(get_resolver().url_patterns):
        if inspect.isclass(cls) and any(base.__name__ in known for base in cls.__mro__):
            if any(callable(getattr(cls, method, None)) for method in ("post", "put", "patch", "delete")) and not issubclass(cls, ObservedOwnerAPIView):
                gaps.append("owner_http_route_uncovered")
            elif issubclass(cls, ObservedOwnerAPIView):
                check_chain(cls)
    writes = {tool["name"] for tool in articles.TOOLS if tool["annotations"]["readOnlyHint"] is False}
    if writes != MCP_WRITES or getattr(articles._call_tool, "_articles_transport_observed", False) is not True:
        gaps.append("owner_mcp_caller_uncovered")
    # Per-request atomic wrapping would put pre-dispatch writes in a rollback
    # transaction. Method-level mutation transactions are observed before entry.
    from django.conf import settings
    if any(config.get("ATOMIC_REQUESTS", False) for config in settings.DATABASES.values()):
        gaps.append("owner_request_transaction_uncovered")
    manifest = {"version": VERSION, "http": sorted(set(covered)), "mcp": sorted(writes),
        "wrappers": wrappers, "policy": "authenticated_before_mutation_v1"}
    return {"version": VERSION, "manifest_digest": goals.digest(manifest),
        "coverage": "partial" if gaps else "complete", "gaps": sorted(set(gaps))}


def admission(op):
    """Bind the initial setup intent inside the original goal's admission write."""
    observed = CURRENT.get()
    if not isinstance(observed, dict):
        return None
    if (observed["company_id"] != str(op.payload.get("company_id"))
            or observed["organization_id"] != str(op.connection.organization_id)
            or observed["connection_id"] != str(op.connection_id)
            or observed["actor_id"] != str(op.payload.get("requested_by_user_id"))):
        return None
    manifest = inventory()
    return {"version": VERSION, "manifest_digest": manifest["manifest_digest"],
        "origin": observed["origin"], "from_start": True, "gaps": manifest["gaps"],
        "required": required(op.connection.organization_id),
        "initial_event": event_id(observed, op.pk)}


class ObservedOwnerAPIView(APIView):
    """Authenticate first, then commit original-goal intent before a mutation."""

    def dispatch(self, request, *args, **kwargs):
        token = CURRENT.set(None)
        try:
            return super().dispatch(request, *args, **kwargs)
        finally:
            CURRENT.reset(token)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from django.conf import settings
        if (request.method not in {"POST", "PUT", "PATCH", "DELETE"}
                or not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False)
                or not getattr(request.user, "is_authenticated", False)
                or not isinstance(request, Request) or not isinstance(request._request, HttpRequest)
                or getattr(request, "_website_prepare_dispatch", False)):
            return
        audit_required = False
        try:
            from .website_views import _context
            from . import website_goal_audit as audit
            context, config, error = _context(request)
            if error is not None or not audit.enabled(context.organization.pk):
                return
            audit_required = required(context.organization.pk)
            method = getattr(self, request.method.lower())
            action = getattr(method, "_articles_audit_action", None) or self.__class__.__name__ + ":" + request.method.lower()
            if kwargs.get("action"):
                action = str(kwargs["action"])
            identity = _intent_identity(self, request, kwargs)
            if identity is None:
                return
            replay, fingerprint = identity
            observed = descriptor(request.user, context, config, action=action, data=dict(request.data),
                run_id=kwargs.get("run_id"), replay=replay, origin="http")
            if observed is None or transaction.get_connection().in_atomic_block:
                if audit_required:
                    raise OwnerAuditRequired()
                return
            if fingerprint is not None:
                observed["intent_digest"] = fingerprint
            CURRENT.set(observed)
            if audit.observe_transport(context, config, observed):
                request._articles_transport_observed = True
            elif audit_required:
                names = {base.__name__ for base in self.__class__.__mro__}
                if not ("WebsiteConnectionActionView" in names and kwargs.get("action") == "prepare"):
                    raise OwnerAuditRequired()
        except Exception:
            # Optional observation cannot expose private error text or change
            # existing product responses. Independent window proof stays needed.
            if audit_required:
                raise OwnerAuditRequired() from None
            return


@contextmanager
def mcp_intent(user, company, *, name, arguments):
    """Enter only after the MCP server validates the grant and owned company."""
    token = CURRENT.set(None)
    audit_required = False
    try:
        from django.conf import settings
        if not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
            yield
            return
        try:
            from . import mcp_operations, website_goal_audit as audit
            from .models import OrganizationContentConfig
            context = mcp_operations.context_for(user, company)
            audit_required = required(context.organization.pk)
            config = OrganizationContentConfig.objects.filter(organization=context.organization).first()
            if config is not None and name in MCP_WRITES and audit.enabled(context.organization.pk):
                observed = descriptor(user, context, config, action="mcp:" + name, data=arguments,
                    run_id=arguments.get("runId"), replay=arguments.get("idempotencyKey"), origin="mcp")
                if observed is not None and not transaction.get_connection().in_atomic_block:
                    CURRENT.set(observed)
                    if not audit.observe_transport(context, config, observed) and audit_required:
                        raise OwnerAuditRequired()
                elif audit_required:
                    raise OwnerAuditRequired()
            elif audit_required:
                raise OwnerAuditRequired()
        except Exception:
            if audit_required:
                raise OwnerAuditRequired() from None
        yield
    finally:
        CURRENT.reset(token)
