"""Pure portable-draft intent and snapshot validation, without repository authority."""

import re

from .website_contract import CONNECTION_FIELDS, WebsiteAuthorityError, connection_contract

REPOSITORY_CONFIG_FIELDS = frozenset({
    "article_template", "design_guide", "resource_prompt", "scan_summary", "tech_stack",
    "installed_packages", "repo_execution_contract", "build_healing_hints", "article_system",
    "article_system_setup_cache", "scan_artifact_cache", "framework_component_specs",
    "last_scanned_sha", "last_scanned_at", "scan_request_fingerprint", "publish_targets",
    "default_publish_target_id", "generated_components", "component_mapping", "design_snapshot",
    "visual_context", "renderer_style_profile", "reference_screenshots", "directory_style_feedback",
    "articles_scaffolded", "articles_scaffold_pr_url", "articles_scaffold_preview_url",
    "article_path_pattern", "registry_path", "repository_inventory",
})

PORTABLE_CALLBACK_EVENTS = frozenset({
    "content_ready", "generation_failed", "generation_blocked", "article_progress",
    "article_admission_attention", "error",
})
PORTABLE_WORKFLOWS = frozenset({"article_generation", "direct_generate", "confirmed_topic", "article_revision", "component_revision"})
PORTABLE_RUN_CONTROLS = frozenset({"resume", "cancel", "deny", "revise", "regenerate-image", "regenerate-images", "preview"})


def portable_run_control_allowed(run, action, payload):
    """Permit editorial controls from original intent without website authority."""
    return action in PORTABLE_RUN_CONTROLS and portable_run_update_allowed(run, payload)


def portable_run_update_allowed(run, payload, *, event_type=""):
    """Allow draft-only updates from original durable intent, never sender claims.

    This grants no repository configuration, preview, publication or token access.
    Only editorial controls, callbacks and run snapshots opt in to this exception.
    """
    if not original_portable_run(run):
        return False
    if getattr(run, "workflow", "") not in PORTABLE_WORKFLOWS or (event_type and event_type not in PORTABLE_CALLBACK_EVENTS):
        return False
    if payload.get("domain") and str(payload["domain"]).lower().strip() != str(run.domain).lower().strip():
        return False
    workflow_groups = ({"article_generation", "direct_generate", "confirmed_topic"}, {"article_revision", "component_revision"})
    allowed_workflows = next((group for group in workflow_groups if run.workflow in group), {run.workflow})
    if payload.get("workflow") and payload["workflow"] not in allowed_workflows:
        return False
    if any(payload.get(key) and str(payload[key]) != str(run.run_id) for key in ("run_id", "job_id")):
        return False
    editorial_status_paths = set()
    request = payload.get("run_request")
    if isinstance(request, dict) and request.get("editorial_admission") is not None:
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        try:
            # Catalog approvals are immutable editorial observations, not a
            # publication lease. Validate their schema, selection hash, brief,
            # tenant and any saved admission before distinguishing these paths.
            merge_editorial_run_snapshot(
                {"workflow": run.workflow, "domain": run.domain, "run_request": run.run_request},
                {**payload, "workflow": payload.get("workflow") or run.workflow,
                 "domain": payload.get("domain") or run.domain},
            )
        except EditorialRunConflict:
            return False
        editorial_status_paths = {
            ("run_request", "editorial_admission", kind, "status")
            for kind in ("audience", "offer")
        }
    forbidden = REPOSITORY_CONFIG_FIELDS | set(CONNECTION_FIELDS) | {
        "github_token", "github_installation_id", "expected_source_sha", "source_sha", "repo_head_sha", "commit_sha",
        "branch", "branch_name", "head_sha", "pr_url", "pr_number", "pull_request_url", "publish_url",
        "live_preview", "live_preview_url", "preview_url", "preview_commit_sha", "verified_sha", "capabilities",
        "publishingReady", "previewSupported", "build_verified", "route_is_live", "preview_content_verified",
        "website_connection", "article_system_setup", "publish_child_run_id", "setup_run_id",
        "connection_id", "connectionId", "connectionGeneration", "connectionTargetId", "repositoryId",
        "websiteConnectionId", "websiteConnection", "livePreview", "previewUrl", "livePreviewUrl", "prUrl",
    }
    def safe(value, path=()):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
                if (key in forbidden or normalized_key in forbidden) and item not in (None, "", False, [], {}):
                    return False
                if normalized_key in {"delivery_mode", "requested_delivery_mode", "resolved_delivery_mode", "publish_resolution"} and item not in (None, "", "content_only"):
                    return False
                if normalized_key == "github_repo" and item and item != getattr(run, "github_repo", ""):
                    return False
                if normalized_key == "domain" and item and str(item).lower().strip() != str(run.domain).lower().strip():
                    return False
                editorial_approval = item == "approved" and path + (key,) in editorial_status_paths
                if normalized_key in {"status", "publish_status", "publish_stage", "merge_status", "approval_state"} and item in ("published", "merged", "approved", "auto_approved", "pr_created", "draft_pr_created", "setup_pr_created") and not editorial_approval:
                    return False
                if not safe(item, path + (key,)):
                    return False
        elif isinstance(value, list):
            return all(safe(item, path + (index,)) for index, item in enumerate(value))
        return True
    return safe(payload)



def explicit_portable_request(payload):
    """Require an explicit confirmed draft choice; saved defaults are not consent."""
    mode = payload.get("delivery_mode", payload.get("deliveryMode"))
    confirmed = payload.get("delivery_mode_confirmed", payload.get("deliveryModeConfirmed", payload.get("delivery_mode_explicit", payload.get("deliveryModeExplicit"))))
    return (mode == "content_only" and (confirmed is True or str(confirmed).lower() in {"true", "1"})
            and payload.get("resolved_delivery_mode", payload.get("resolvedDeliveryMode")) in (None, "", "content_only"))


def original_portable_run(run):
    """Only original persisted unbound draft intent can bypass repository work."""
    original = getattr(run, "run_request", None)
    try:
        return (isinstance(original, dict) and explicit_portable_request(original)
                and not connection_contract(original)
                and original.get("resolved_delivery_mode") in (None, "", "content_only"))
    except WebsiteAuthorityError:
        # Malformed stored authority is never permission to bypass its fence.
        return False
