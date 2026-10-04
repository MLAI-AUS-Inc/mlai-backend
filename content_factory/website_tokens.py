"""Short-lived GitHub credentials scoped to current website consent."""

import uuid

from django.core.cache import cache
from rest_framework.response import Response

from .website_contract import WebsiteAuthorityError
from .website_connections import authority_guard, contract_for


def token_index_key(connection_id, generation):
    """Cache address of revocable references, never a credential value."""
    return f"website-token-refs:{connection_id}:{generation}"


def issue_website_token(request):
    """Do not fall back to legacy OAuth when website authority is denied."""
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from integrations.http_client import RequestException
    data = dict(request.query_params.items())
    mode = str(data.get("permission_mode") or "read")
    if mode not in {"read", "write"}:
        return Response({"error": "invalid_permission_mode"}, status=400)
    action = str(data.get("action") or "read")
    if action == "portable":
        return Response({"error": "portable_repository_access_denied"}, status=409)
    if mode == "write" and action not in {"setup", "publish", "merge", "cleanup"}:
        return Response({"error": "invalid_write_action"}, status=400)
    try:
        return Response(mint_website_token(data, permission_mode=mode, action=action))
    except WebsiteAuthorityError as exc:
        return Response(exc.as_dict(), status=exc.status)
    except RequestException:
        return Response({"allowed": False, "error": "github_temporarily_unavailable", "code": "github_temporarily_unavailable", "detail": "GitHub could not issue a repository credential. Retry shortly.", "retryable": True}, status=503)
    except GitHubAppTokenError:
        return Response({"allowed": False, "error": "github_repository_unavailable", "detail": "GitHub access could not be verified. Reconnect the selected repository.", "retryable": False}, status=409)


def mint_website_token(data, *, permission_mode="read", action="read"):
    """Issue and track one ephemeral token under the same lock as revocation."""
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from integrations.http_client import RequestException
    if action == "portable":
        raise WebsiteAuthorityError("portable_repository_access_denied", "Portable drafts cannot access repository credentials.")
    if permission_mode not in {"read", "write"} or (permission_mode == "write" and action not in {"setup", "publish", "merge", "cleanup"}):
        raise WebsiteAuthorityError("invalid_write_action", "Write credentials require an explicit repository mutation action.", status=400)
    with authority_guard(data, action=action) as connection:
        if not connection.repository_id or not connection.installation_id:
            raise WebsiteAuthorityError("github_verification_required", "Reconnect GitHub to verify repository identity.")
        try:
            token = create_installation_access_token(installation_id=connection.installation_id,
                repository=connection.github_repo, repository_id=connection.repository_id, permission_mode=permission_mode, use_cache=False)
        except RequestException as exc:
            raise WebsiteAuthorityError("github_temporarily_unavailable", "GitHub could not issue a repository credential. Retry shortly.", status=503, retryable=True) from exc
        except GitHubAppTokenError as exc:
            raise WebsiteAuthorityError("github_repository_unavailable", "GitHub access could not be verified. Reconnect the selected repository.") from exc
        reference = str(uuid.uuid4())
        cache.set(f"website-issued-token:{reference}", token.token, timeout=3600)
        index = token_index_key(connection.pk, connection.generation)
        references = cache.get(index) or []
        cache.set(index, [*references, reference], timeout=3600)
        return {**token.as_content_factory_payload(domain=connection.organization.domain),
            **contract_for(connection), "credential_reference": reference, "permission_mode": permission_mode}


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
