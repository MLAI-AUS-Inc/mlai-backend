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
    "delivery_mode_confirmed", "article_quality_gate_policy")


def enroll_setup_delivery(operation, payload, website, workflow):
    """Record enrollment only from an explicit server-side organisation list.

    Existing enrolled attempts stay enrolled when new enrollment is paused.
    Worker configuration and an incoming request marker cannot opt themselves
    in. With the defaults off, the legacy request shape is unchanged.
    """
    if workflow not in {"repo_scan", "article_system_setup"}:
        return False
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
    original = {key: deepcopy(payload[key]) for key in ORIGINAL_REQUEST_FIELDS if key in payload}
    original.update(scope)
    original["setup_delivery_v1"] = dict(MARKER)
    operation.payload = {**saved, "setup_delivery_v1": dict(MARKER), "setup_delivery_scope": scope,
                         "setup_delivery_request": original}
    payload["setup_delivery_v1"] = dict(MARKER)
    return True
