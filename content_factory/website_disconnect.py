"""One consent click fences work, refunds starts and cleans only ledger-owned refs."""
from urllib.parse import quote, urlparse
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .website_connections import contract_for, transition_connection
from .website_contract import WebsiteAuthorityError, connection_contract
from .website_models import WebsiteConnectionOperation


def start_disconnect(config, *, user, data):
    """Commit the generation fence immediately and reserve the composite receipt."""
    remove = data.get("remove_setup", False)
    if not isinstance(remove, bool):
        raise WebsiteAuthorityError("invalid_disconnect_choice", "Choose whether to keep the articles pages.", status=400)
    contract = connection_contract(data)
    connection = config.website_connection
    if connection is None or not contract or contract["website_connection_id"] != str(connection.pk):
        raise WebsiteAuthorityError("website_connection_required", "Select the current website to disconnect.")
    operation_id = data.get("operation_id")
    if operation_id:
        try:
            operation_id = UUID(str(operation_id))
        except (ValueError, TypeError):
            raise WebsiteAuthorityError("disconnect_operation_required", "Select the saved disconnect receipt.", status=400)
        existing = WebsiteConnectionOperation.objects.filter(pk=operation_id, connection=connection,
            action="disconnect", payload__remove_setup__isnull=False).first()
        if existing is None:
            raise WebsiteAuthorityError("disconnect_operation_required", "That disconnect receipt does not belong to the selected website.")
        if contract["connection_generation"] != connection.generation:
            raise WebsiteAuthorityError("website_connection_changed", "Refresh the current disconnect receipt.")
    else:
        key = f"{connection.pk}:disconnect:{contract['connection_generation']}"
        existing = WebsiteConnectionOperation.objects.filter(idempotency_key=key, connection=connection).first()
    if existing:
        if existing.action != "disconnect" or "remove_setup" in data and existing.payload.get("remove_setup", False) != remove:
            raise WebsiteAuthorityError("operation_key_conflict", "This disconnect request has a different cleanup choice.")
        if existing.generation != connection.generation or connection.state != "disconnected":
            raise WebsiteAuthorityError("website_connection_changed", "A newer connection superseded this disconnect.")
        if existing.state == "needs_attention":
            existing.state, existing.next_attempt_at = "pending", timezone.now()
            existing.receipt = {**existing.receipt, "cleanup_failures": 0, "status": "working", "user_action": None}
            changed = WebsiteConnectionOperation.objects.filter(pk=existing.pk, state="needs_attention",
                generation=existing.generation, connection__generation=existing.generation,
                connection__state="disconnected").update(state=existing.state, next_attempt_at=existing.next_attempt_at,
                    receipt=existing.receipt, updated_at=timezone.now())
            if changed != 1:
                raise WebsiteAuthorityError("website_connection_changed", "The saved disconnect changed before retry.")
        return existing
    with transaction.atomic():
        # The fence and complete cleanup/refund manifest commit together, so a
        # web-process crash cannot strand a generic receipt between them.
        op = transition_connection(config, action="disconnect", expected=data,
            idempotency_key=f"disconnect:{contract['connection_generation']}")
        saved = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
        if "remove_setup" in saved.payload:
            if saved.payload["remove_setup"] != remove:
                raise WebsiteAuthorityError("operation_key_conflict", "This disconnect request has a different cleanup choice.")
            return saved
        saved.payload = {**saved.payload, "remove_setup": remove, "requested_by_user_id": str(user.pk),
            "refund_run_ids": list(saved.payload.get("cancel_run_ids", [])),
            "mutation_ids": [str(value) for value in connection.repository_mutations.values_list("pk", flat=True)]}
        saved.receipt = {**saved.receipt, "keep_pages": not remove, "remove_setup": remove,
            "completed_steps": ["authority_revoked", "local_runs_cancelled"], "status": "working",
            "message": "Disconnecting your website.", "disconnect_steps_pending": True}
        saved.save(update_fields=["payload", "receipt", "updated_at"])
    return saved


def _fence_disconnect(op):
    """Stop if reconnect or a newer consent operation supersedes this receipt."""
    from .website_models import WebsiteConnection
    if not WebsiteConnection.objects.filter(pk=op.connection_id, generation=op.generation, state="disconnected",
            repository_id=op.connection.repository_id, installation_id=op.connection.installation_id).exists():
        raise WebsiteAuthorityError("website_connection_changed", "A newer website connection superseded this disconnect.")


def refund_cancelled_runs(op):
    """Refund the actual recorded payer, retaining the requesting founder in audit."""
    from roo.models import Ledger
    from workflow_runs.models import ContentFactoryRun
    from integrations.services.article_generation import refund_content_factory_request_for_user
    refunded = set(op.receipt.get("refunded_run_ids") or [])
    pending = []
    for run in ContentFactoryRun.objects.filter(organization_id=op.connection.organization_id,
            run_id__in=op.payload.get("refund_run_ids", op.payload.get("cancel_run_ids", []))):
        if run.run_id in refunded:
            continue
        request = run.run_request or {}
        key = request.get("client_request_id")
        if not key:
            # A setup/scan is free. A historical charge without request identity
            # stays explicitly pending for reconciliation instead of guessed.
            if request.get("roo_points_cost"):
                pending.append(run.run_id)
            else:
                refunded.add(run.run_id)
            continue
        ledger = Ledger.objects.filter(idempotency_key=f"content_factory:charge:{key}", kind="SPEND",
            source="CONTENT_FACTORY").select_related("user").first()
        if ledger and ledger.user:
            try:
                refund_content_factory_request_for_user(user=ledger.user,
                    actor_id=ledger.created_by_slack_id or "", article_request=request,
                    resolved_domain=op.connection.organization.domain, reason="Website disconnected before article completion.")
            except Exception:
                pending.append(run.run_id)
                continue
        refunded.add(run.run_id)
    op.receipt.update(refunded_run_ids=sorted(refunded), refund_pending_run_ids=pending)
    return not pending


def close_owned_repository_refs(op, *, limit=20):
    """Close PRs and delete branches only while their live head matches the ledger."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import require_unlocked_remote_call
    connection = op.connection
    finished = set(op.receipt.get("closed_mutation_ids") or [])
    mutations = connection.repository_mutations.filter(pk__in=op.payload.get("mutation_ids", [])).order_by("created_at")
    rows = [row for row in mutations if str(row.pk) not in finished][:limit]
    skipped = list(op.receipt.get("skipped_refs") or [])
    closed_prs = list(op.receipt.get("closed_pr_urls") or [])
    deleted_branches = list(op.receipt.get("deleted_branches") or [])
    pending = []
    if not rows:
        return True
    require_unlocked_remote_call()
    _fence_disconnect(op)
    token = create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="write", use_cache=False)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{connection.github_repo}"
    try:
        for row in rows:
            _fence_disconnect(op)
            branch, head = row.branch, row.head_sha
            if not branch or branch == connection.branch or not head:
                skipped.append({"mutation_id": str(row.pk), "reason": "unproven_or_default_branch"})
                finished.add(str(row.pk))
                continue
            reference = http_client.get(f"{base}/git/ref/heads/{quote(branch, safe='')}", headers=headers, timeout=(3, 15))
            if reference.status_code != 404:
                reference.raise_for_status()
                if (reference.json().get("object") or {}).get("sha") != head:
                    skipped.append({"branch": branch, "reason": "customer_changed_branch"})
                    finished.add(str(row.pk))
                    continue
            url = urlparse(row.pr_url)
            prefix = f"/{connection.github_repo}/pull/"
            number = url.path.removeprefix(prefix)
            if row.pr_url and (url.hostname != "github.com" or not url.path.startswith(prefix) or not number.isdigit()):
                skipped.append({"mutation_id": str(row.pk), "reason": "unproven_pull_request"})
                finished.add(str(row.pk))
                continue
            if row.pr_url:
                pull_response = http_client.get(f"{base}/pulls/{number}", headers=headers, timeout=(3, 15))
                if pull_response.status_code != 404:
                    pull_response.raise_for_status()
                    pull = pull_response.json()
                    actual = pull.get("head") or {}
                    if actual.get("ref") != branch or actual.get("sha") != head or (actual.get("repo") or {}).get("id") != connection.repository_id:
                        skipped.append({"pr_url": row.pr_url, "reason": "customer_changed_pull_request"})
                        finished.add(str(row.pk))
                        continue
                    if pull.get("state") == "open" and not pull.get("merged"):
                        _fence_disconnect(op)
                        response = http_client.patch(f"{base}/pulls/{number}", headers=headers, json={"state": "closed"}, timeout=(3, 15))
                        response.raise_for_status()
                    if row.pr_url not in closed_prs and not pull.get("merged"):
                        closed_prs.append(row.pr_url)
            if reference.status_code != 404:
                _fence_disconnect(op)
                response = http_client.delete(f"{base}/git/refs/heads/{quote(branch, safe='')}", headers=headers, timeout=(3, 15))
                if response.status_code not in {204, 404}:
                    response.raise_for_status()
                if branch not in deleted_branches:
                    deleted_branches.append(branch)
            finished.add(str(row.pk))
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass
        op.receipt.update(closed_mutation_ids=sorted(finished), closed_pr_urls=closed_prs,
            deleted_branches=deleted_branches, skipped_refs=skipped)
    return len(finished) >= mutations.count()


def _open_cleanup(op):
    from django.contrib.auth import get_user_model
    from .models import OrganizationContentConfig
    from .website_reconciliation import _cleanup_proposal, approve_cleanup_proposal
    config = OrganizationContentConfig.objects.get(website_connection_id=op.connection_id)
    user = get_user_model().objects.get(pk=op.payload["requested_by_user_id"], is_active=True)
    cleanup, _ = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{op.connection_id}:disconnect-cleanup:{op.generation}", defaults={
            "connection": op.connection, "generation": op.generation, "action": "cleanup",
            "payload": {"mutation_ids": op.payload.get("mutation_ids", []), "setup_run_ids": [], "attempt": 1},
            "receipt": {"status": "proposal_requested", "repository_modified": False}})
    if cleanup.receipt.get("pr_url"):
        return cleanup
    receipt = _cleanup_proposal(cleanup)
    cleanup.receipt, cleanup.state = receipt, "review_required"
    cleanup.save(update_fields=["receipt", "state", "updated_at"])
    if not receipt.get("deletions"):
        cleanup.state = "completed"
        cleanup.receipt.update(status="no_unchanged_owned_files", repository_modified=False)
        cleanup.save(update_fields=["state", "receipt", "updated_at"])
        return cleanup
    return approve_cleanup_proposal(config, user=user, data={**contract_for(op.connection),
        "operation_id": str(cleanup.pk), "source_sha": receipt["source_sha"],
        "proposal_digest": receipt["proposal_digest"], "approve_cleanup": True})


def advance_disconnect_steps(op):
    """Resume only missing composite steps; generic reconciler handles cancellation/tokens."""
    _fence_disconnect(op)
    completed = list(op.receipt.get("completed_steps") or [])
    op.receipt["completed_steps"] = completed
    if "refunds" not in completed and refund_cancelled_runs(op):
        completed.append("refunds")
    if "repository_refs" not in completed and close_owned_repository_refs(op):
        completed.append("repository_refs")
    if op.payload.get("remove_setup") and "cleanup_pr" not in completed:
        cleanup = _open_cleanup(op)
        op.receipt.update(cleanup_operation_id=str(cleanup.pk), cleanup_pr_url=cleanup.receipt.get("pr_url"),
            cleanup_status=cleanup.receipt.get("status") or cleanup.state,
            cleanup_required=cleanup.receipt.get("status") != "no_unchanged_owned_files",
            cleanup_skipped_files=cleanup.receipt.get("conflicts", []), retained_files=cleanup.receipt.get("retained", []))
        completed.append("cleanup_pr")
    if not op.payload.get("remove_setup") and "pages_kept" not in completed:
        completed.append("pages_kept")
    required = {"refunds", "repository_refs", "cleanup_pr" if op.payload.get("remove_setup") else "pages_kept"}
    op.receipt.update(completed_steps=completed, disconnect_steps_pending=not required.issubset(set(completed)))
    return op


def disconnect_retry_policy(op, *, now, progressed):
    """Bound transport retries without spending a failure budget on batch progress."""
    if op.state != "pending":
        return op
    failures = 0 if progressed else int(op.receipt.get("cleanup_failures", 0)) + 1
    op.receipt["cleanup_failures"] = failures
    if failures >= 5:
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="needs_attention", message="Website access is revoked. Automatic cleanup needs attention.",
            user_action={"id": "retry_disconnect", "label": "Retry cleanup"})
    else:
        op.next_attempt_at = now + timedelta(minutes=(1, 2, 5, 10)[max(0, min(failures - 1, 3))])
    return op
