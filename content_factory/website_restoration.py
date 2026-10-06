"""Reviewed inverse restoration transport, separate from ordinary write consent."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from organizations.models import Organization
from .website_contract import WebsiteAuthorityError, connection_contract
from .website_models import WebsiteConnection, WebsiteConnectionOperation


def restoration_change_groups(receipt):
    """Expose legacy review groups without changing the reviewed inverse diff."""
    changes = receipt.get("changes") if isinstance(receipt.get("changes"), list) else []
    return {"deletions": [item for item in changes if isinstance(item, dict) and item.get("operation") == "delete"],
        "restorations": [item for item in changes if isinstance(item, dict) and item.get("operation") == "restore"]}


def worker_restoration(operation, *, apply=False):
    """Call the artifact owner outside locks with a narrow operation manifest."""
    from integrations import http_client
    from .website_connections import require_unlocked_remote_call
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    require_unlocked_remote_call()
    website = operation.connection
    setup_run_id = operation.payload.get("setup_run_id") or next(iter(operation.payload.get("setup_run_ids", [])), None)
    if not setup_run_id:
        return None  # Historical ledger-only connections use conservative fallback.
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise WebsiteAuthorityError("restoration_worker_unavailable", "The restoration worker is temporarily unavailable.", status=503, retryable=True)
    from .website_operations import deletion_epoch
    from .models import WrittenArticle
    dependencies = {}
    for path in WrittenArticle.objects.filter(organization=website.organization).exclude(content_path="").values_list("content_path", flat=True):
        dependencies[path] = sorted({dep for mutation in website.repository_mutations.all() for item in mutation.files
            if item.get("kind") == "published_article" and item.get("path") == path for dep in item.get("retained_dependencies", [])})
    payload = {"setup_run_id": setup_run_id, "connection_generation": website.generation, "repository_id": website.repository_id,
        "domain": website.organization.domain, "github_repo": website.github_repo,
        "operation_id": str(operation.pk), "operation_attempt": operation.payload.get("attempt", 1), "deletion_epoch": deletion_epoch(website),
        "content_dependencies": dependencies}
    if apply:
        payload.update(approved=True, expected_base_sha=operation.receipt.get("source_sha"), plan_digest=operation.receipt.get("proposal_digest"), idempotency_key=operation.idempotency_key)
    response = http_client.post(f"{remote['base_url']}/api/connections/{website.pk}/restoration-{'apply' if apply else 'plan'}",
        headers=_content_factory_headers(), json=payload, timeout=(3, 30))
    response.raise_for_status()
    result = response.json()
    if result.get("schema_version") != 2:
        raise WebsiteAuthorityError("restoration_contract_unavailable", "The worker does not support reviewed inverse restoration yet.", retryable=True)
    return {**result, **restoration_change_groups(result), "source_sha": result.get("base_sha"), "proposal_digest": result.get("plan_digest"),
        "setup_run_id": setup_run_id, "requires_review": not apply, "default_branch_modified": False, "cleanup_complete": False}


def approve_worker_restoration(config, *, user, data):
    """Claim exact approval, contact the worker, then conditionally persist proof."""
    from .website_connections import verify_repository_access
    from uuid import UUID
    try:
        operation_id = UUID(str(data.get("operation_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_operation_id", "Select a valid cleanup operation.", status=422) from exc
    binding = connection_contract(data)
    metadata = verify_repository_access(user=user, repo=config.github_repo)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        website = WebsiteConnection.objects.select_for_update().get(pk=config.website_connection_id)
        if not binding or any({"website_connection_id": str(website.pk), "connection_generation": website.generation, "repository_id": website.repository_id}.get(k) != v for k, v in binding.items()):
            raise WebsiteAuthorityError("website_connection_changed", "Refresh the reviewed removal proposal.")
        if metadata["repository_id"] != website.repository_id:
            raise WebsiteAuthorityError("website_repository_changed", "Repository identity changed.")
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation_id, connection=website, generation=website.generation, action="cleanup").first()
        if not op or not op.receipt.get("setup_run_id"):
            return None
        if op.state in {"awaiting_merge", "awaiting_deployment", "completed"}:
            return op
        if data.get("proposal_digest") != op.receipt.get("proposal_digest") or data.get("source_sha") != op.receipt.get("source_sha"):
            raise WebsiteAuthorityError("cleanup_proposal_changed", "Review the current removal proposal.")
        if op.state == "applying" and op.next_attempt_at and op.next_attempt_at > timezone.now():
            raise WebsiteAuthorityError("cleanup_in_progress", "Removal is being reconciled.", retryable=True)
        if op.state not in {"review_required", "applying"}:
            raise WebsiteAuthorityError("cleanup_proposal_required", "Prepare and review a removal proposal first.")
        op.state = "applying"
        op.attempts += 1
        claim = op.attempts
        op.next_attempt_at = timezone.now() + timedelta(minutes=5)
        op.payload = {**op.payload, "setup_run_id": op.receipt["setup_run_id"],
            "approved_restoration": {"plan_digest": op.receipt["proposal_digest"], "expected_base_sha": op.receipt["source_sha"],
                "approved_by_user_id": str(user.pk), "workflow_paths": sorted({item["path"]
                    for item in (op.receipt.get("changes") or []) if isinstance(item, dict)
                    and item.get("operation") in {"delete", "restore"}
                    and item.get("path") == ".github/workflows/mlai-articles-verification.yml"})}}
        op.save(update_fields=["state", "attempts", "next_attempt_at", "payload", "updated_at"])
    try:
        receipt = worker_restoration(op, apply=True)
        receipt = {**op.receipt, **receipt}
    except Exception:
        WebsiteConnectionOperation.objects.filter(pk=op.pk, attempts=claim, state="applying").update(
            next_attempt_at=timezone.now(), receipt={**op.receipt, "last_error": "restoration_reconciliation_required"})
        raise WebsiteAuthorityError("restoration_reconciliation_required", "Retry the same approved removal to reconcile its remote outcome.", retryable=True)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        current = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
        saved = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
        if current.generation != op.generation or saved.attempts != claim or saved.state != "applying":
            saved.state = "attention_required"
            saved.receipt = {**receipt, "status": "authority_changed_after_restoration", "cleanup_complete": False}
        else:
            saved.state = "awaiting_merge" if receipt.get("pr_url") else "attention_required" if receipt.get("conflicts") else "completed" if receipt.get("status") == "no_op" else "review_required"
            saved.receipt = receipt
        saved.save(update_fields=["state", "receipt", "updated_at"])
        return saved
