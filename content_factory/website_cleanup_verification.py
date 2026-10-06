"""Observed completion of the exact reviewed inverse, independent of write policy."""

import re
from uuid import UUID
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from organizations.models import Organization
from .website_contract import WebsiteAuthorityError, connection_contract
from .website_models import WebsiteConnection, WebsiteConnectionOperation


def verify_cleanup_deployment(config, *, data):
    """Require merged current source, provider CI and reviewed public-route outcomes."""
    from integrations import http_client
    from .website_tokens import mint_ci_evidence_token
    from .website_connections import require_unlocked_remote_call
    from .website_live_fetch import fetch_live_route
    try:
        identifier = UUID(str(data.get("operation_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_operation_id", "Select a valid cleanup operation.", status=422) from exc
    reviewed = connection_contract(data)
    if not config.website_connection_id:
        raise WebsiteAuthorityError("website_connection_required", "Select the connection whose cleanup you reviewed.")
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        website = WebsiteConnection.objects.select_for_update().get(pk=config.website_connection_id)
        if not reviewed or reviewed["website_connection_id"] != str(website.pk) or reviewed["connection_generation"] != website.generation:
            raise WebsiteAuthorityError("website_connection_changed", "Refresh the cleanup connection review.")
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, connection=website, generation=website.generation, action="cleanup").first()
        if not op or op.state not in {"awaiting_deployment", "completed"}:
            raise WebsiteAuthorityError("cleanup_deployment_not_ready", "Merge the reviewed cleanup pull request before verifying deployment.")
        if op.state == "completed":
            return op
        source = op.receipt.get("merge_sha")
        routes = op.receipt.get("verification_routes")
        if not isinstance(routes, list) or not routes or len(routes) > 30:
            raise WebsiteAuthorityError("cleanup_verification_manifest_required", "This historical integration has no reviewed route restoration proof. Its removal stays pending until an explicit verification manifest is available.")
        snapshot = (website.generation, website.configuration_version, op.updated_at)
        domain, repo, branch = website.organization.domain, website.github_repo, website.branch
    require_unlocked_remote_call()
    credential = mint_ci_evidence_token(installation_id=website.installation_id, repository=repo,
        repository_id=website.repository_id)
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    observations = []
    try:
        head = http_client.get(f"https://api.github.com/repos/{repo}/commits/{quote(branch, safe='')}", headers=headers, timeout=(3, 15))
        head.raise_for_status()
        if not source or head.json().get("sha") != source:
            raise WebsiteAuthorityError("cleanup_source_changed", "Verify the exact merged cleanup source; the selected branch changed.")
        checks = http_client.get(f"https://api.github.com/repos/{repo}/commits/{source}/check-runs?per_page=100", headers=headers, timeout=(3, 15))
        checks.raise_for_status()
        rows = checks.json().get("check_runs", [])
        if checks.json().get("total_count", len(rows)) != len(rows):
            raise WebsiteAuthorityError("cleanup_build_verification_required", "The repository check inventory exceeds the bounded verification response.")
        if (not any(row.get("head_sha") == source and row.get("conclusion") == "success" and (row.get("app") or {}).get("slug") == "github-actions" for row in rows)
                or any(row.get("status") != "completed" or row.get("conclusion") not in {"success", "neutral", "skipped"} for row in rows)):
            raise WebsiteAuthorityError("cleanup_build_verification_required", "The merged cleanup must pass its repository CI checks.")
        for route in routes:
            path = str(route.get("path") or "")
            status = route.get("expected_status", 200)
            if not path.startswith("/") or path.startswith("//") or ".." in path.split("/") or status not in {200, 404}:
                raise WebsiteAuthorityError("cleanup_verification_manifest_invalid", "The reviewed cleanup has an invalid route proof.")
            body, response_headers = fetch_live_route(f"https://{domain}{path}", domain, expected_status=status)
            if status == 200:
                digest = str(route.get("expected_artifact_digest") or "")
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise WebsiteAuthorityError("cleanup_restoration_marker_required", "Restored routes need their reviewed original artifact marker.")
                if response_headers.get("x-mlai-artifact-digest") != digest and not re.search(rb'<meta\s+name=["\x27]mlai-artifact-digest["\x27]\s+content=["\x27]' + digest.encode() + rb'["\x27]', body):
                    raise WebsiteAuthorityError("cleanup_restoration_unverified", "The restored public route differs from the reviewed inverse.")
            observations.append({"path": path, "status": status, "artifact_digest": route.get("expected_artifact_digest")})
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            pass
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        current = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
        saved = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
        if (current.generation, current.configuration_version, saved.updated_at) != snapshot or saved.state != "awaiting_deployment":
            raise WebsiteAuthorityError("cleanup_review_changed", "Cleanup changed during verification. Refresh its receipt.")
        saved.state = "completed"
        saved.receipt = {**saved.receipt, "status": "completed", "cleanup_complete": True,
            "deployment_verification_required": False, "deployment_receipt": {"source_sha": source,
                "provider_ci_verified": True, "routes": observations, "checked_at": timezone.now().isoformat()}}
        saved.save(update_fields=["state", "receipt", "updated_at"])
        return saved
