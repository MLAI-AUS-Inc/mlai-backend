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


def _ci_evidence_operation(connection, data):
    """Keep the CI-only credential bound to the original current operation."""
    from .website_operations import validate_operation
    if any(data.get(key) in (None, "") for key in ("operation_id", "operation_attempt", "deletion_epoch")):
        raise WebsiteAuthorityError("website_operation_required", "CI evidence requires the original operation, attempt and deletion watermark.")
    operation = validate_operation(connection, data)
    recorded_run = operation.payload.get("run_id")
    if recorded_run and recorded_run != (data.get("run_id") or data.get("job_id")):
        raise WebsiteAuthorityError("website_operation_changed", "CI evidence belongs to a different run.")


def mint_ci_evidence_token(*, installation_id, repository, repository_id):
    """Issue only CI read credentials after the caller's canonical authority check."""
    from integrations.services.github_app import (create_installation_access_token, GitHubAppTokenError,
        GitHubCIEvidencePermissionRequired, CI_EVIDENCE_PERMISSION_DETAIL, GitHubPermissionLookupUnavailable)
    from integrations.http_client import RequestException
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        return create_installation_access_token(installation_id=installation_id, repository=repository,
            repository_id=repository_id, permission_mode="read", permission_profile="ci_evidence", use_cache=False)
    except GitHubCIEvidencePermissionRequired as exc:
        raise WebsiteAuthorityError("github_ci_evidence_permission_required", CI_EVIDENCE_PERMISSION_DETAIL) from exc
    except (GitHubPermissionLookupUnavailable, RequestException) as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub CI evidence is temporarily unavailable. Retry shortly.", status=503, retryable=True) from exc
    except GitHubAppTokenError as exc:
        raise WebsiteAuthorityError("github_repository_unavailable", "GitHub access could not be verified. Reconnect the selected repository.") from exc


def read_ci_provider_json(url, *, headers):
    """Read provider evidence without exposing credential or provider error bodies."""
    from integrations import http_client
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        response = http_client.get(url, headers=headers, timeout=(3, 15))
    except http_client.RequestException as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub CI evidence is temporarily unavailable. Retry shortly.", status=503, retryable=True) from exc
    provider_headers = getattr(response, "headers", {}) or {}
    if (response.status_code == 429 or response.status_code >= 500
            or (response.status_code == 403 and (provider_headers.get("Retry-After") or provider_headers.get("X-RateLimit-Remaining") == "0"))):
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub CI evidence is temporarily unavailable. Retry shortly.", status=503, retryable=True)
    if response.status_code != 200:
        raise WebsiteAuthorityError("github_repository_unavailable", "GitHub CI access could not be verified for the selected repository.")
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise WebsiteAuthorityError("github_ci_evidence_unavailable", "GitHub returned invalid CI evidence. Retry shortly.", status=503, retryable=True) from exc
    if not isinstance(payload, dict):
        raise WebsiteAuthorityError("github_ci_evidence_unavailable", "GitHub returned invalid CI evidence. Retry shortly.", status=503, retryable=True)
    return payload


def _valid_ci_check_row(row):
    if not isinstance(row, dict):
        return False
    text_or_null = lambda value: value is None or isinstance(value, str)
    if any(not text_or_null(row.get(key)) for key in ("name", "head_sha", "status", "conclusion")):
        return False
    app, output = row.get("app"), row.get("output")
    if app is not None and (not isinstance(app, dict) or not text_or_null(app.get("slug"))):
        return False
    if output is not None and (not isinstance(output, dict)
            or any(not text_or_null(output.get(key)) for key in ("title", "summary", "text"))):
        return False
    return True


def read_ci_provider_checks(url, *, headers):
    """Require a bounded provider Checks envelope before inspecting attestation."""
    payload = read_ci_provider_json(url, headers=headers)
    rows = payload.get("check_runs")
    if not isinstance(rows, list) or len(rows) > 100 or any(not _valid_ci_check_row(row) for row in rows):
        raise WebsiteAuthorityError("github_ci_evidence_unavailable", "GitHub returned invalid CI evidence. Retry shortly.", status=503, retryable=True)
    return payload


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
        elif exc.code == "github_ci_evidence_permission_required":
            payload.update(permission_profile="ci_evidence", required_permissions=["checks:read", "statuses:read"])
        return Response(payload, status=exc.status)
    except RequestException:
        return Response({"allowed": False, "error": "github_temporarily_unavailable", "code": "github_temporarily_unavailable", "detail": "GitHub could not issue a repository credential. Retry shortly.", "retryable": True}, status=503)
    except GitHubAppTokenError:
        return Response({"allowed": False, "error": "github_repository_unavailable", "detail": "GitHub access could not be verified. Reconnect the selected repository.", "retryable": False}, status=409)


def mint_website_token(data, *, permission_mode="read", action="read"):
    """Issue and track one ephemeral token under the same lock as revocation."""
    from integrations.services.github_app import (create_installation_access_token, GitHubAppTokenError,
        GitHubWorkflowPermissionRequired, GitHubCIEvidencePermissionRequired, CI_EVIDENCE_PERMISSION_DETAIL,
        GitHubPermissionLookupUnavailable, require_installation_workflow_permissions)
    from integrations.http_client import RequestException
    if action in {"portable", "worker_cleanup", "cancel_operation"}:
        raise WebsiteAuthorityError("portable_repository_access_denied", "Portable drafts cannot access repository credentials.")
    if permission_mode not in {"read", "write"} or (permission_mode == "write" and action not in {"setup", "publish", "merge", "cleanup", "restoration"}):
        raise WebsiteAuthorityError("invalid_write_action", "Write credentials require an explicit repository mutation action.", status=400)
    profile = data.get("permission_profile", "repository")
    if not isinstance(profile, str) or profile not in {"repository", "workflow_files", "ci_evidence"}:
        raise WebsiteAuthorityError("invalid_permission_profile", "Unknown repository permission profile.", status=400)
    if profile == "workflow_files" and (permission_mode != "write" or action not in {"setup", "cleanup", "restoration"}):
        raise WebsiteAuthorityError("invalid_permission_profile", "Workflow files require a setup or approved inverse operation with explicit write mode.", status=400)
    if profile == "ci_evidence" and (permission_mode != "read" or action != "read"):
        raise WebsiteAuthorityError("invalid_permission_profile", "CI evidence requires explicit read mode and action.", status=400)
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
        elif profile == "ci_evidence":
            _ci_evidence_operation(connection, data)
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
        if profile != "repository":
            kwargs["permission_profile"] = profile
        token = create_installation_access_token(**kwargs)
    except RequestException as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub could not issue a repository credential. Retry shortly.", status=503, retryable=True) from exc
    except GitHubPermissionLookupUnavailable as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub permission evidence is temporarily unavailable. Retry shortly.", status=503, retryable=True) from exc
    except GitHubWorkflowPermissionRequired as exc:
        raise _workflow_permission_denial() from exc
    except GitHubCIEvidencePermissionRequired as exc:
        raise WebsiteAuthorityError("github_ci_evidence_permission_required", CI_EVIDENCE_PERMISSION_DETAIL) from exc
    except GitHubAppTokenError as exc:
        raise WebsiteAuthorityError("github_repository_unavailable", "GitHub access could not be verified. Reconnect the selected repository.") from exc
    try:
        with authority_guard(binding, action=action) as connection:
            if profile == "workflow_files":
                _workflow_operation(connection, binding, action)
            elif profile == "ci_evidence":
                _ci_evidence_operation(connection, binding)
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
