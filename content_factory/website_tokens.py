"""Short-lived GitHub credentials scoped to current website consent."""

import uuid

from django.core.cache import cache
from rest_framework.response import Response

from .website_contract import WebsiteAuthorityError, connection_contract
from .website_connections import authority_guard, contract_for


def _workflow_operation(connection, data, action):
    """Limit workflow credentials to a real setup or reviewed inverse operation."""
    from .website_operations import validate_operation
    if any(data.get(key) in (None, "") for key in ("operation_id", "operation_attempt", "deletion_epoch")):
        raise WebsiteAuthorityError("website_operation_required", "Workflow files require the original operation, attempt and deletion watermark.")
    try:
        uuid.UUID(str(data["operation_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_operation_id", "A valid operation ID is required.") from exc
    operation = validate_operation(connection, {**data, "action": action}, restoration=action == "restoration")
    if action == "setup":
        valid = operation.action == "workflow" and operation.payload.get("workflow") == "article_system_setup"
        recorded_run = operation.payload.get("run_id")
        if recorded_run and recorded_run != (data.get("run_id") or data.get("job_id")):
            valid = False
    else:
        valid = operation.action == "cleanup"
        if action == "cleanup":
            approved = operation.payload.get("approved_cleanup") or {}
            valid = (valid and operation.state in {"applying", "awaiting_merge", "awaiting_deployment", "completed"}
                     and isinstance(approved, dict) and bool(approved.get("approved_by_user_id"))
                     and bool(approved.get("proposal_digest")) and bool(approved.get("source_sha"))
                     and data.get("proposal_digest") == approved.get("proposal_digest")
                     and (data.get("source_sha") or data.get("expected_source_sha")) == approved.get("source_sha")
                     and all(data.get(key) in (None, "", approved.get("source_sha")) for key in ("source_sha", "expected_source_sha"))
                     and isinstance(approved.get("deletions"), list)
                     and ".github/workflows/mlai-articles-verification.yml" in approved["deletions"])
        else:
            approved = operation.payload.get("approved_restoration") or {}
            valid = (valid and operation.state in {"applying", "awaiting_merge", "awaiting_deployment", "completed"}
                     and isinstance(approved, dict) and bool(approved.get("approved_by_user_id"))
                     and bool(approved.get("plan_digest")) and bool(approved.get("expected_base_sha"))
                     and isinstance(approved.get("workflow_paths"), list)
                     and ".github/workflows/mlai-articles-verification.yml" in approved["workflow_paths"])
    if not valid:
        raise WebsiteAuthorityError("website_operation_changed", "Workflow files require the original setup or approved cleanup operation.")


def _workflow_permission_denial():
    from integrations.services.github_app import WORKFLOW_PERMISSION_DETAIL
    return WebsiteAuthorityError("github_workflow_permission_required", WORKFLOW_PERMISSION_DETAIL)


def _token_contract(connection, data):
    contract = contract_for(connection)
    target = connection_contract(data).get("connection_target_id")
    if target:
        # authority_guard has checked this target against the current generation.
        contract["connection_target_id"] = target
    return contract


def token_index_key(connection_id, generation):
    """Cache address of revocable references, never a credential value."""
    return f"website-token-refs:{connection_id}:{generation}"


def issue_website_token(request):
    """Do not fall back to legacy OAuth when website authority is denied."""
    from integrations.services.github_app import GitHubAppTokenError
    from integrations.http_client import RequestException
    data = dict(request.query_params.items())
    mode = str(data.get("permission_mode") or "read")
    if mode not in {"read", "write"}:
        return Response({"error": "invalid_permission_mode"}, status=400)
    action = str(data.get("action") or "read")
    if action in {"portable", "worker_cleanup", "cancel_operation"}:
        return Response({"error": "portable_repository_access_denied"}, status=409)
    if mode == "write" and action not in {"setup", "publish", "merge", "cleanup", "restoration"}:
        return Response({"error": "invalid_write_action"}, status=400)
    try:
        return Response(mint_website_token(data, permission_mode=mode, action=action))
    except WebsiteAuthorityError as exc:
        payload = exc.as_dict()
        if exc.code == "github_workflow_permission_required":
            payload.update(permission_profile="workflow_files", required_permission="workflows:write")
        return Response(payload, status=exc.status)
    except RequestException:
        return Response({"allowed": False, "error": "github_temporarily_unavailable", "code": "github_temporarily_unavailable", "detail": "GitHub could not issue a repository credential. Retry shortly.", "retryable": True}, status=503)
    except GitHubAppTokenError:
        return Response({"allowed": False, "error": "github_repository_unavailable", "detail": "GitHub access could not be verified. Reconnect the selected repository.", "retryable": False}, status=409)


def mint_website_token(data, *, permission_mode="read", action="read"):
    """Issue and track one ephemeral token under the same lock as revocation."""
    from integrations.services.github_app import (create_installation_access_token, GitHubAppTokenError,
        GitHubWorkflowPermissionRequired, GitHubPermissionLookupUnavailable, require_installation_workflow_permissions)
    from integrations.http_client import RequestException
    if action in {"portable", "worker_cleanup", "cancel_operation"}:
        raise WebsiteAuthorityError("portable_repository_access_denied", "Portable drafts cannot access repository credentials.")
    if permission_mode not in {"read", "write"} or (permission_mode == "write" and action not in {"setup", "publish", "merge", "cleanup", "restoration"}):
        raise WebsiteAuthorityError("invalid_write_action", "Write credentials require an explicit repository mutation action.", status=400)
    profile = data.get("permission_profile", "repository")
    if not isinstance(profile, str) or profile not in {"repository", "workflow_files"}:
        raise WebsiteAuthorityError("invalid_permission_profile", "Unknown repository permission profile.", status=400)
    if profile == "workflow_files" and (permission_mode != "write" or action not in {"setup", "cleanup", "restoration"}):
        raise WebsiteAuthorityError("invalid_permission_profile", "Workflow files require a setup or approved inverse operation with explicit write mode.", status=400)
    raw_preflight = data.get("preflight", "0")
    if raw_preflight not in ("0", "1", "false", "true", False, True):
        raise WebsiteAuthorityError("invalid_permission_preflight", "Unknown permission preflight mode.", status=400)
    preflight = raw_preflight in ("1", "true", True)
    if preflight and profile != "workflow_files":
        raise WebsiteAuthorityError("invalid_permission_preflight", "Permission preflight requires the workflow-files profile.", status=400)
    with authority_guard(data, action=action) as connection:
        if not connection.repository_id or not connection.installation_id:
            raise WebsiteAuthorityError("github_verification_required", "Reconnect GitHub to verify repository identity.")
        if profile == "workflow_files":
            _workflow_operation(connection, data, action)
        binding = {**dict(data), **contract_for(connection)}
        installation_id, repository, repository_id = connection.installation_id, connection.github_repo, connection.repository_id
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        if preflight:
            permissions = require_installation_workflow_permissions(installation_id)
            with authority_guard(binding, action=action) as connection:
                _workflow_operation(connection, binding, action)
                return {"allowed": True, "permission_profile": profile, "permission_mode": permission_mode,
                        "preflight": True, "required_permission": "workflows:write",
                        "permissions_source": "installation_grant", "token_source": "github_app_installation",
                        "github_permissions": permissions, "permissions": permissions,
                        **_token_contract(connection, binding)}
        kwargs = {"installation_id": installation_id, "repository": repository,
                  "repository_id": repository_id, "permission_mode": permission_mode, "use_cache": False}
        if profile == "workflow_files":
            kwargs["permission_profile"] = profile
        token = create_installation_access_token(**kwargs)
    except RequestException as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub could not issue a repository credential. Retry shortly.", status=503, retryable=True) from exc
    except GitHubPermissionLookupUnavailable as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub permission evidence is temporarily unavailable. Retry shortly.", status=503, retryable=True) from exc
    except GitHubWorkflowPermissionRequired as exc:
        raise _workflow_permission_denial() from exc
    except GitHubAppTokenError as exc:
        raise WebsiteAuthorityError("github_repository_unavailable", "GitHub access could not be verified. Reconnect the selected repository.") from exc
    try:
        with authority_guard(binding, action=action) as connection:
            if profile == "workflow_files":
                _workflow_operation(connection, binding, action)
            reference = str(uuid.uuid4())
            cache.set(f"website-issued-token:{reference}", token.token, timeout=3600)
            for index in (token_index_key(connection.pk, connection.generation),
                    token_index_key(connection.pk, f"operation:{data.get('operation_id')}")):
                if "operation:None" in index:
                    continue
                references = cache.get(index) or []
                cache.set(index, [*references, reference], timeout=3600)
            return {**token.as_content_factory_payload(domain=connection.organization.domain),
                **_token_contract(connection, binding), "credential_reference": reference, "permission_mode": permission_mode,
                "permission_profile": profile}
    except WebsiteAuthorityError:
        # Minting can race disconnect. Do not deliver a credential after consent
        # changed; revoke the just-created token outside the failed transaction.
        from integrations import http_client
        try:
            http_client.delete("https://api.github.com/installation/token", headers={"Authorization": f"Bearer {token.token}"}, timeout=(3, 10))
        except Exception:
            pass
        raise


def revoke_generation_tokens(connection_id, generation):
    """Best-effort remote revocation with retryable reference-only receipts."""
    from integrations import http_client
    index = token_index_key(connection_id, generation)
    pending = []
    revoked = 0
    for reference in cache.get(index) or []:
        key = f"website-issued-token:{reference}"
        token = cache.get(key)
        if not token:
            continue
        try:
            response = http_client.delete("https://api.github.com/installation/token", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=(3, 10))
            if response.status_code not in {204, 401, 404}:
                pending.append(reference)
                continue
        except Exception:
            pending.append(reference)
            continue
        cache.delete(key)
        revoked += 1
    if pending:
        cache.set(index, pending, timeout=3600)
    else:
        cache.delete(index)
    return {"revoked": revoked, "pending": len(pending)}
