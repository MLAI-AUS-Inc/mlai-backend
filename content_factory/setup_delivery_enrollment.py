"""Server-owned, sticky setup protocol enrollment before remote dispatch."""

from copy import deepcopy
from django.conf import settings

MARKER = {"schema_version": 1}
SCOPE_FIELDS = ("domain", "github_repo", "website_connection_id", "connection_generation", "repository_id",
                "operation_id", "operation_attempt", "deletion_epoch", "client_request_id",
                "app_root", "branch", "expected_source_sha")
ORIGINAL_REQUEST_FIELDS = (*SCOPE_FIELDS, "workflow", "configuration_revision", "connection_target_id",
    "source_run_id", "parent_run_id", "source_scan_run_id", "source_setup_run_id",
    "article_surface_mode", "article_surface_url", "article_surface_hint", "delivery_mode",
    "delivery_mode_confirmed", "article_quality_gate_policy", "workflow_order_v1")


def article_stream(payload):
    """Recognize only the original paired first-article reservation."""
    marker=payload.get("article_delivery_v1")
    return (isinstance(marker,dict) and set(marker)=={"schema_version"}
        and type(marker.get("schema_version")) is int and marker["schema_version"]==1
        and isinstance(payload.get("first_article_goal_id"),str)
        and bool(payload["first_article_goal_id"])
        and payload["first_article_goal_id"]==payload.get("first_article_goal_operation_id")
        and not payload.get("setup_delivery_v1"))


def enroll_article_delivery(operation,payload,website,workflow, *, new_run=False):
    """Reserve a separate article stream from a validated server-owned goal."""
    if workflow not in {"article_generation","direct_generate","confirmed_topic","article_publish","publish_article"}:
        return False
    from .workflow_order import MARKER as ORDER_MARKER, reserve_marker
    payload.pop(ORDER_MARKER, None)
    saved=operation.payload or {}
    sticky=saved.get("article_delivery_v1")==MARKER
    if sticky:
        from .website_contract import WebsiteAuthorityError
        original=saved.get("article_delivery_request") or {}
        if not article_stream(original) or any(payload.get(key)!=original.get(key) for key in (
            "first_article_goal_id","first_article_goal_operation_id","expected_source_sha","website_connection_id","connection_generation")):
            raise WebsiteAuthorityError("website_operation_changed","The original article delivery reservation changed.")
    if not sticky:
        if not getattr(settings,"CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED",False):
            payload.pop("article_delivery_v1",None)
            return False
        from .website_models import WebsiteConnectionOperation
        from .website_article_recovery import enabled
        from uuid import UUID
        try:
            identity=payload.get("first_article_goal_operation_id")
            if not isinstance(identity,str) or str(UUID(identity))!=identity:raise ValueError
        except (ValueError,TypeError,AttributeError):
            payload.pop("article_delivery_v1",None)
            return False
        parent=WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
            pk=identity,connection=website,action="prepare").first()
        if (parent is None or not enabled(parent)
            or payload.get("first_article_goal_id")!=str(parent.pk)
            or payload.get("expected_source_sha")!=parent.receipt["goal"].get("current_source_sha")):
            payload.pop("article_delivery_v1",None)
            return False
    if payload.get("setup_delivery_v1") or payload.get("review_update_operation_id") or payload.get("feedback_batch_id"):
        payload.pop("article_delivery_v1",None)
        return False
    scope={key:deepcopy(payload[key]) for key in SCOPE_FIELDS if key in payload and payload[key] not in (None,"")}
    scope.setdefault("domain",website.organization.domain);scope.setdefault("github_repo",website.github_repo)
    wire=saved.get("article_delivery_workflow") or ("confirmed_topic" if payload.get("source_run_id") else "direct_generate")
    reserve_marker(saved, payload, "article_delivery", new_run=new_run)
    original={key:deepcopy(payload[key]) for key in (*ORIGINAL_REQUEST_FIELDS,"first_article_goal_id","first_article_goal_operation_id") if key in payload}
    original.update(scope);original["article_delivery_v1"]=dict(MARKER)
    operation.payload={**saved,"article_delivery_v1":dict(MARKER),"article_delivery_scope":scope,
        "article_delivery_request":original,"article_delivery_workflow":wire}
    payload["article_delivery_v1"]=dict(MARKER)
    return True


def delivery_workflow(run):
    """Map the backend's reserved article names to the worker's execution name."""
    if run.workflow in {"article_publish","publish_article"}:
        return "direct_generate"
    if run.workflow=="article_generation":
        return "confirmed_topic" if (run.run_request or {}).get("source_run_id") else "direct_generate"
    return run.workflow


def enroll_setup_delivery(operation, payload, website, workflow, *, new_run=False):
    """Record enrollment only from an explicit server-side organisation list.

    Existing enrolled attempts stay enrolled when new enrollment is paused.
    Worker configuration and an incoming request marker cannot opt themselves
    in. With the defaults off, the legacy request shape is unchanged.
    """
    from .workflow_order import MARKER as ORDER_MARKER, reserve_marker
    payload.pop(ORDER_MARKER, None)
    if workflow not in {"repo_scan", "article_system_setup"}:
        return enroll_article_delivery(operation,payload,website,workflow,new_run=new_run)
    if payload.get("review_update_operation_id") or payload.get("feedback_batch_id"):
        # Independent review children update source comments under a different
        # lock order. Keep their entire execution on the audited legacy path;
        # inheriting a parent's protocol marker would strand the first outcome.
        payload.pop("setup_delivery_v1", None)
        return False
    saved = operation.payload or {}
    sticky = saved.get("setup_delivery_v1") == MARKER
    configured = getattr(settings, "CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS", ())
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.split(",") if item.strip()]
    allowed = (getattr(settings, "CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED", False)
               and str(website.organization_id) in {str(item) for item in configured})
    if not (sticky or allowed):
        payload.pop("setup_delivery_v1", None)
        return False
    scope = {key: payload[key] for key in SCOPE_FIELDS if key in payload
             and (key not in {"app_root", "branch", "expected_source_sha"} or payload[key] not in (None, ""))}
    scope.setdefault("domain", website.organization.domain)
    scope.setdefault("github_repo", website.github_repo)
    reserve_marker(saved, payload, "setup_delivery", new_run=new_run)
    original = {key: deepcopy(payload[key]) for key in ORIGINAL_REQUEST_FIELDS if key in payload}
    original.update(scope)
    original["setup_delivery_v1"] = dict(MARKER)
    operation.payload = {**saved, "setup_delivery_v1": dict(MARKER), "setup_delivery_scope": scope,
                         "setup_delivery_request": original}
    payload["setup_delivery_v1"] = dict(MARKER)
    return True
