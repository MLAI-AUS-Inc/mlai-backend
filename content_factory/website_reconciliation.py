"""Durable GitHub revocation and reference-only outbox processing."""

from datetime import timedelta
import base64
import hashlib
from urllib.parse import quote

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import OrganizationContentConfig
from .website_connections import contract_for, transition_connection
from .website_contract import cleanup_plan, safe_repository_path, evidence_digest
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteScanSnapshot


def _merge_connection_identity(website):
    """Fence authenticated merge observations to the currently selected source."""
    return (str(website.pk), website.generation, website.repository_id,
        website.github_repo.casefold(), website.branch, website.state,
        website.installation_id, website.configuration_version)


def _merge_intent_matches(website, run, pull, source_sha):
    """Accept only a merged GitHub PR matching our saved pre-merge intent."""
    intent = (run.result or {}).get("publish_merge_intent") or {}
    request = run.run_request or {}
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if not (intent.get("website_connection_id") == str(website.pk)
            and intent.get("connection_generation") == website.generation
            and intent.get("repository_id") == website.repository_id
            and intent.get("run_id") == run.run_id
            and str(intent.get("github_repo") or "").casefold() == website.github_repo.casefold()
            and intent.get("source_sha") == website.verified_sha
            and intent.get("base_branch") == website.branch
            and request.get("website_connection_id") == str(website.pk)
            and request.get("connection_generation") == website.generation
            and run.github_repo.casefold() == website.github_repo.casefold()
            and pull.get("merged") is True and pull.get("merge_commit_sha") == source_sha
            and pull.get("number") == intent.get("pr_number")
            and base.get("ref") == website.branch
            and head.get("sha") == intent.get("head_sha")
            and head.get("ref") == intent.get("head_branch")):
        return False
    return all((side.get("repo") or {}).get("id") == website.repository_id
        and str((side.get("repo") or {}).get("full_name") or "").casefold() == website.github_repo.casefold()
        for side in (head, base))


def _find_owned_merge(website, organization_id, source_sha):
    """Resolve webhook races with read-only authenticated PR evidence, outside locks."""
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard, require_unlocked_remote_call
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    rows = ContentFactoryRun.objects.filter(organization_id=organization_id,
        github_repo__iexact=website.github_repo,
        run_request__website_connection_id=str(website.pk), run_request__connection_generation=website.generation)
    saved = rows.filter(result__merge_status="merged", result__merge_response__sha=source_sha).first()
    identity = _merge_connection_identity(website)
    if saved:
        return {"run_id": saved.run_id, "connection_identity": identity}
    candidates = rows.filter(result__publish_merge_intent__website_connection_id=str(website.pk),
        result__publish_merge_intent__connection_generation=website.generation,
        result__publish_merge_intent__repository_id=website.repository_id,
        result__publish_merge_intent__source_sha=website.verified_sha).order_by("-updated_at")[:10]
    for run in candidates:
        intent = (run.result or {}).get("publish_merge_intent") or {}
        number = intent.get("pr_number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        credential = None
        headers = {}
        try:
            binding = {**contract_for(website), "domain": website.organization.domain, "github_repo": website.github_repo}
            with authority_guard(binding, action="read"):
                pass
            require_unlocked_remote_call()
            credential = create_installation_access_token(installation_id=website.installation_id,
                repository=website.github_repo, repository_id=website.repository_id, permission_mode="read", use_cache=False)
            headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
            response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/pulls/{number}",
                headers=headers, timeout=(3, 15))
            response.raise_for_status()
            pull = response.json()
            if not isinstance(pull, dict) or not _merge_intent_matches(website, run, pull, source_sha):
                continue
            with authority_guard(binding, action="read") as current:
                if _merge_connection_identity(current) == identity:
                    return {"run_id": run.run_id, "connection_identity": identity}
        except Exception:
            # Unavailable or ambiguous PR evidence remains an external source change.
            continue
        finally:
            if credential:
                try:
                    http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
                except Exception:
                    pass
    return None


def revoke_installation(installation_id, *, repository_ids=None, reason="github_authorization_revoked"):
    """Revoke exact GitHub identities; an install/reinstall event never reconnects."""
    rows = OrganizationContentConfig.objects.filter(website_connection__installation_id=str(installation_id),
        website_connection__state__in=["connected", "paused"]).select_related("website_connection__organization")
    if repository_ids is not None:
        rows = rows.filter(website_connection__repository_id__in=repository_ids)
    count = 0
    for config in rows:
        connection = config.website_connection
        transition_connection(config, action="revoke", expected=contract_for(connection),
            idempotency_key=f"{reason}:{connection.generation}")
        count += 1
    return count


def handle_website_github_event(event_type, payload):
    """Process verified lifecycle events using immutable IDs, fail closed on loss."""
    action = str(payload.get("action") or "")
    installation_id = (payload.get("installation") or {}).get("id")
    result = {"event": event_type, "revoked": 0}
    if event_type == "installation" and action in {"deleted", "suspend"} and installation_id:
        result["revoked"] = revoke_installation(installation_id, reason=f"github_installation_{action}")
    elif event_type == "installation_repositories" and action == "removed" and installation_id:
        ids = [r["id"] for r in payload.get("repositories_removed", []) if isinstance(r, dict) and isinstance(r.get("id"), int)]
        result["revoked"] = revoke_installation(installation_id, repository_ids=ids, reason="github_repository_removed")
    elif event_type == "repository" and action in {"deleted", "transferred", "renamed", "archived", "privatized"}:
        repository_id = (payload.get("repository") or {}).get("id")
        if repository_id:
            for inst in WebsiteConnection.objects.filter(repository_id=repository_id).values_list("installation_id", flat=True).distinct():
                result["revoked"] += revoke_installation(inst, repository_ids=[repository_id], reason=f"github_repository_{action}")
    elif event_type == "push":
        repo = payload.get("repository") or {}
        source_sha = str(payload.get("after") or "")
        from .website_contract import SHA_PATTERN
        if not isinstance(repo.get("id"), int) or not SHA_PATTERN.fullmatch(source_sha) or set(source_sha) == {"0"}:
            result["ignored"] = True
            return result
        result["invalidated"] = 0
        for config in OrganizationContentConfig.objects.filter(website_connection__repository_id=repo["id"], website_connection__state__in=["connected", "paused"]).select_related("website_connection__organization"):
            website = config.website_connection
            if payload.get("ref") != f"refs/heads/{website.branch}" or website.verified_sha == source_sha:
                continue
            owned_merge = _find_owned_merge(website, config.organization_id, source_sha)
            from organizations.models import Organization
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=config.organization_id)
                website = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
                if (website.state not in {"connected", "paused"} or website.repository_id != repo["id"]
                        or payload.get("ref") != f"refs/heads/{website.branch}" or website.verified_sha == source_sha):
                    continue
                if owned_merge and owned_merge["connection_identity"] != _merge_connection_identity(website):
                    owned_merge = None
                _, created = WebsiteScanSnapshot.objects.get_or_create(connection=website, generation=website.generation,
                    run_id=f"github-head:{source_sha}", fingerprint=evidence_digest({"source_sha": source_sha}),
                    defaults={"source_sha": source_sha, "detector_version": "github_head", "evidence": {"source": "github_push", "branch": website.branch}})
                if created:
                    website.configuration_version += 1
                    if not owned_merge:
                        website.capabilities = {**website.capabilities, "publishingReady": False, "previewSupported": False}
                    WebsiteConnectionOperation.objects.get_or_create(
                        idempotency_key=f"{website.pk}:source-reverify:{website.generation}:{source_sha}", defaults={
                            "connection": website, "generation": website.generation, "action": "source-reverify",
                            "payload": {"source_sha": source_sha, "target_id": config.default_publish_target_id,
                                "owned_merge_run_id": owned_merge["run_id"] if owned_merge else "", "scan_required": not bool(owned_merge)},
                            "receipt": {"status": "verification_queued", "repository_modified": False}})
                    if not owned_merge:
                        website.blockers = [item for item in website.blockers if item.get("code") != "repository_source_changed"] + [{"code": "repository_source_changed", "message": "The repository changed. Scan and verify the current source before publishing.", "source_sha": source_sha}]
                    website.save(update_fields=["configuration_version", "capabilities", "blockers", "updated_at"])
                    result["invalidated"] += 1
    else:
        result["ignored"] = True
    return result


def _cleanup_proposal(operation):
    """Read current files and propose changes only; never mutate a repository here."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    connection = operation.connection
    token = create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read", use_cache=False)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    repo_url = f"https://api.github.com/repos/{connection.github_repo}"
    head = http_client.get(f"{repo_url}/commits/{quote(connection.branch, safe='')}", headers=headers, timeout=(3, 15))
    head.raise_for_status()
    sha = head.json()["sha"]
    files = []
    unproven_paths = []
    for mutation in connection.repository_mutations.filter(pk__in=operation.payload.get("mutation_ids", [])):
        applied = mutation.status == "applied" and bool(mutation.head_sha)
        if applied and mutation.head_sha != sha:
            comparison = http_client.get(f"{repo_url}/compare/{mutation.head_sha}...{sha}", headers=headers, timeout=(3, 15))
            comparison.raise_for_status()
            applied = comparison.json().get("status") in {"ahead", "identical"}
        if applied:
            files.extend(mutation.files)
        else:
            unproven_paths.extend(row.get("path", "") for row in mutation.files)
    # Multiple ledger entries for one file mean shared ownership; retain by default.
    seen = set()
    for entry in files:
        if entry["path"] in seen:
            entry["ownership"] = "shared"
        seen.add(entry["path"])
    current = {}
    for entry in files:
        path = safe_repository_path(entry["path"])
        response = http_client.get(f"{repo_url}/contents/{quote(path, safe='/')}", params={"ref": sha}, headers=headers, timeout=(3, 15))
        if response.status_code == 404:
            current[path] = None
            continue
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("type") != "file" or value.get("encoding") != "base64":
            current[path] = "unverified"
        else:
            current[path] = hashlib.sha256(base64.b64decode(value["content"])).hexdigest()
    from .models import WrittenArticle
    retained_paths = unproven_paths + list(WrittenArticle.objects.filter(organization=connection.organization).exclude(content_path="").values_list("content_path", flat=True))
    plan = cleanup_plan(files, current, retained_paths=retained_paths)
    plan["retained"] = sorted(set(plan["retained"]) | set(retained_paths))
    ambiguous = {row["path"] for row in plan["conflicts"]}
    plan["deletions"] = sorted(set(plan["deletions"]) - ambiguous)
    receipt = {"status": "review_required", "source_sha": sha, "repository_modified": False,
        "requires_review": True, **plan}
    receipt["proposal_digest"] = evidence_digest(receipt)
    return receipt


def disable_pending_native_auto_merge(connection, urls):
    """Withdraw previously queued GitHub merges while retaining the pull requests."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from urllib.parse import urlparse
    if not urls:
        return []
    pending = []
    token = create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="write", use_cache=False)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    try:
        for url in urls:
            parsed = urlparse(url)
            prefix = f"/{connection.github_repo}/pull/"
            number = parsed.path.removeprefix(prefix)
            if parsed.hostname != "github.com" or not parsed.path.startswith(prefix) or not number.isdigit():
                continue
            try:
                response = http_client.get(f"https://api.github.com/repos/{connection.github_repo}/pulls/{number}", headers=headers, timeout=(3, 15))
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                pull = response.json()
                if not pull.get("auto_merge"):
                    continue
                response = http_client.post("https://api.github.com/graphql", headers=headers,
                    json={"query": "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }", "variables": {"id": pull["node_id"]}}, timeout=(3, 15))
                response.raise_for_status()
                if response.json().get("errors"):
                    pending.append(url)
            except Exception:
                pending.append(url)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass
    return pending


def _process_worker_followup(identifier, now):
    """Claim a durable follow-up, release all DB locks, then contact the worker."""
    from .website_connections import authority_guard, owner_write_guard
    from .website_contract import WebsiteAuthorityError
    from integrations.services import article_generation
    with transaction.atomic():
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, state="pending").first()
        if operation is None or (operation.next_attempt_at and operation.next_attempt_at > now):
            return None
        operation.attempts += 1
        attempt = operation.attempts
        operation.next_attempt_at = now + timedelta(minutes=5)
        operation.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
        payload = dict(operation.payload)
    binding = payload.get("binding") or {}
    kind = payload.get("kind")
    state, receipt = "pending", {"status": "retry_pending", "repository_modified": False}
    remote = {}
    try:
        action = "publish" if kind == "publish_article" else "read"
        with authority_guard(binding, action=action):
            pass
        functions = {"publish_article": article_generation.publish_article,
            "trigger_article_generation": article_generation.trigger_article_generation,
            "confirm_topic": article_generation.confirm_topic}
        remote = functions[kind](**payload["arguments"])
        with owner_write_guard(binding):
            state, receipt = "completed", {"status": "dispatched", "repository_modified": False,
                "run_id": str((remote or {}).get("run_id") or (remote or {}).get("job_id") or payload.get("source_run_id") or "")}
            if kind == "publish_article":
                from .models import ContentFactoryJob
                job = ContentFactoryJob.objects.filter(job_id=payload.get("source_run_id")).first()
                if job:
                    job.status = "generating"
                    metadata = dict(job.request_meta or {})
                    actions = dict(metadata.get("callback_actions") or {})
                    markers = list(actions.get("preview_ready_auto_approve") or [])
                    if payload.get("callback_dedupe_key") and payload["callback_dedupe_key"] not in markers:
                        markers.append(payload["callback_dedupe_key"])
                    actions["preview_ready_auto_approve"] = markers
                    job.request_meta = {**metadata, "publish_stage": "auto_approved", "callback_actions": actions}
                    job.save(update_fields=["status", "request_meta", "updated_at"])
    except WebsiteAuthorityError as exc:
        state, receipt = "cancelled", {"status": "authority_revoked", "code": exc.code, "repository_modified": False}
        if isinstance(remote, dict) and (remote.get("run_id") or remote.get("job_id")):
            from .vibe_marketing_views import _create_local_run
            _create_local_run(workflow="publish_article" if kind == "publish_article" else "article_generation",
                domain=binding.get("domain", ""), payload=binding, remote_data=remote)
    except Exception:
        receipt["last_error"] = "worker_followup_unavailable"
    # Compare-and-set the claim: a duplicate reconciler cannot replace a newer receipt.
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8))), updated_at=timezone.now())
    return state


def process_website_connection_operations(*, limit=20, now=None, connection_id=None):
    """Retry cancellation/token revocation, and build bounded cleanup proposals."""
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_tokens import revoke_generation_tokens
    now = now or timezone.now()
    due = WebsiteConnectionOperation.objects.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now), state="pending").exclude(action="workflow")
    if connection_id:
        due = due.filter(connection_id=connection_id)
    ids = list(due.order_by("next_attempt_at").values_list("id", flat=True)[:limit])
    result = {"status": "completed", "processed": 0, "completed": 0, "pending": 0}
    from .website_support import reconcile_support_verifications
    reconcile_support_verifications(limit=limit, connection_id=connection_id)
    monitor_cleanup_pull_requests(limit=limit, connection_id=connection_id)
    for identifier in ids:
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="source-reverify").exists():
            outcome = _process_source_reverification(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="worker_followup").exists():
            outcome = _process_worker_followup(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        purge_connection_id = None
        with transaction.atomic():
            # Lease this operation briefly. Neither operation nor authority rows
            # may stay locked across worker HTTP (offboarding locks both).
            op = WebsiteConnectionOperation.objects.select_for_update(of=("self",)).select_related("connection").filter(pk=identifier).first()
            if op is None:
                continue
            if op.state != "pending" or (op.next_attempt_at and op.next_attempt_at > now):
                continue
            result["processed"] += 1
            op.attempts += 1
            op.next_attempt_at = now + timedelta(minutes=5)
            op.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
            claimed_at = op.updated_at
        from .website_connections import require_unlocked_remote_call
        require_unlocked_remote_call()
        try:
            if op.action == "cleanup":
                from .website_restoration import worker_restoration
                op.receipt = worker_restoration(op) or _cleanup_proposal(op)
                op.state = "review_required"
            else:
                token_scope = f"operation:{op.payload['cancelled_operation_id']}" if op.action == "cancel-operation" else op.payload.get("previous_generation")
                tokens = revoke_generation_tokens(op.connection_id, token_scope)
                cancel_ids = op.payload.get("cancel_run_ids", [])
                preview_ids = op.payload.get("stop_preview_run_ids", [])
                pending = list(cancel_ids[20:])
                preview_pending = list(preview_ids[20:])
                remote = _content_factory_remote_config()
                for run_id in cancel_ids[:20]:
                    if not remote["enabled"]:
                        pending.append(run_id)
                        continue
                    if op.action == "cancel-operation":
                        cancelled = op.connection.operations.get(pk=op.payload["cancelled_operation_id"])
                        from .website_operations import deletion_epoch
                        response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/cancel-operation",
                            headers=_content_factory_headers(), json={**contract_for(op.connection), "run_id": run_id,
                                "operation_id": str(cancelled.pk), "operation_attempt": cancelled.payload.get("attempt", 1),
                                "deletion_epoch": deletion_epoch(op.connection)}, timeout=(3, 15))
                    else:
                        response = http_client.post(f"{remote['base_url']}/api/runs/{run_id}/cancel",
                            headers=_content_factory_headers(), json={"reason": "website_connection_changed"}, timeout=(3, 15))
                    try:
                        body = response.json()
                    except (ValueError, AttributeError):
                        body = {}
                    cleanup_pending = isinstance(body, dict) and (body.get("cleanup_pending") is True or body.get("cleanupPending") is True)
                    if response.status_code not in {200, 204, 404} or cleanup_pending:
                        pending.append(run_id)
                    if op.action == "cancel-operation" and isinstance(body, dict):
                        op.receipt["remote_outcomes"] = body.get("remote_outcomes", [])
                for run_id in preview_ids[:20]:
                    if not remote["enabled"]:
                        preview_pending.append(run_id)
                        continue
                    response = http_client.post(f"{remote['base_url']}/api/runs/{run_id}/preview/stop",
                        headers=_content_factory_headers(), json={"reason": "website_connection_changed"}, timeout=(3, 15))
                    try:
                        body = response.json()
                    except (ValueError, AttributeError):
                        body = {}
                    confirmed = response.status_code in {204, 404} or (response.status_code == 200 and isinstance(body, dict)
                        and body.get("cleanup_success") is True and body.get("cleanup_pending") is not True and body.get("cleanupPending") is not True)
                    if response.status_code == 404:
                        op.receipt.setdefault("preview_absent_run_ids", []).append(run_id)
                    if not confirmed:
                        preview_pending.append(run_id)
                pending_merges = disable_pending_native_auto_merge(op.connection, op.payload.get("disable_auto_merge_prs", []))
                op.payload = {**op.payload, "cancel_run_ids": pending, "stop_preview_run_ids": preview_pending, "disable_auto_merge_prs": pending_merges,
                    "purge_run_ids": op.payload.get("purge_run_ids", preview_ids)}
                op.receipt = {**op.receipt, "tokens": tokens, "preview_cleanup_pending": bool(preview_pending),
                    "remote_cleanup_pending": bool(pending or preview_pending or pending_merges or tokens["pending"])}
                if op.action == "purge":
                    from .website_operations import deletion_epoch
                    manifest = {"phase": "apply", "approved": True, "connection_generation": op.payload["previous_generation"],
                        "operation_id": str(op.pk), "operation_attempt": op.payload.get("attempt", 1), "deletion_epoch": op.payload["deletion_epoch"],
                        "run_ids": op.payload.get("purge_run_ids", op.payload.get("stop_preview_run_ids", [])),
                        "domain": op.connection.organization.domain, "github_repo": op.connection.github_repo,
                        "idempotency_key": op.idempotency_key}
                    if remote["enabled"]:
                        manifest["repository_id"] = op.connection.repository_id
                        plan_response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/worker-cleanup",
                            headers=_content_factory_headers(), json={**manifest, "phase": "plan"}, timeout=(3, 20))
                        plan_response.raise_for_status()
                        plan = plan_response.json()
                        if not plan.get("plan_digest") or plan.get("conflicts"):
                            raise ValueError("Unreviewable worker cleanup scope")
                        manifest["plan_digest"] = plan["plan_digest"]
                        response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/worker-cleanup",
                            headers=_content_factory_headers(), json=manifest, timeout=(3, 20))
                        response.raise_for_status()
                        cleanup = response.json()
                        op.receipt["artifact_cleanup"] = cleanup
                        worker_pending = cleanup.get("status") != "completed" or bool(cleanup.get("cleanup_pending") or cleanup.get("external_pending"))
                    else:
                        worker_pending = True
                    op.receipt["remote_cleanup_pending"] |= worker_pending
                    op.receipt["deletion_epoch"] = op.payload["deletion_epoch"]
                if not op.receipt["remote_cleanup_pending"]:
                    op.state = "completed"
                    op.receipt["status"] = "completed"
        except Exception:
            # No transport messages can accidentally include credentials or source bodies.
            op.receipt = {**op.receipt, "last_error": "remote_reconciliation_unavailable"}
        op.next_attempt_at = now + timedelta(seconds=min(3600, 15 * 2 ** min(op.attempts, 8)))
        applied = WebsiteConnectionOperation.objects.filter(pk=op.pk, state="pending", attempts=op.attempts,
            updated_at=claimed_at).update(receipt=op.receipt, payload=op.payload, state=op.state,
                next_attempt_at=op.next_attempt_at, updated_at=timezone.now())
        # Offboarding may redact the payload while transport runs. Never replace
        # that newer retention/erasure receipt with our pre-request snapshot.
        result["pending" if not applied or op.state == "pending" else "completed"] += 1
        if applied and op.state == "completed" and op.payload.get("purge_after_reconciliation"):
            purge_connection_id = op.connection_id
        if purge_connection_id:
            # Follow the same org -> connection lock order as owner lifecycle
            # writes, after releasing the operation lock to avoid inversion.
            from organizations.models import Organization
            candidate = WebsiteConnection.objects.filter(pk=purge_connection_id).first()
            if candidate:
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=candidate.organization_id)
                    candidate = WebsiteConnection.objects.select_for_update().filter(pk=purge_connection_id, authorized_by__isnull=True, state="revoked").first()
                    if candidate and not candidate.operations.filter(state="pending").exists():
                        candidate.delete()
    return result


def monitor_cleanup_pull_requests(*, limit=20, connection_id=None):
    """Observe merge/deploy stages; a removal PR is never completed removal."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import require_unlocked_remote_call
    from urllib.parse import urlparse
    require_unlocked_remote_call()
    rows = WebsiteConnectionOperation.objects.filter(action="cleanup", state__in=["awaiting_merge", "awaiting_deployment"])
    if connection_id:
        rows = rows.filter(connection_id=connection_id)
    for op in rows.select_related("connection__organization").order_by("updated_at")[:limit]:
        parsed = urlparse(str(op.receipt.get("pr_url") or ""))
        prefix = f"/{op.connection.github_repo}/pull/"
        number = parsed.path.removeprefix(prefix)
        if parsed.hostname != "github.com" or not parsed.path.startswith(prefix) or not number.isdigit():
            continue
        headers = {}
        try:
            credential = create_installation_access_token(installation_id=op.connection.installation_id,
                repository=op.connection.github_repo, repository_id=op.connection.repository_id, permission_mode="read", use_cache=False)
            headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
            response = http_client.get(f"https://api.github.com/repos/{op.connection.github_repo}/pulls/{number}", headers=headers, timeout=(3, 15))
            response.raise_for_status()
            pull = response.json()
            if ((pull.get("base") or {}).get("repo") or {}).get("id") != op.connection.repository_id or (pull.get("base") or {}).get("ref") != op.connection.branch:
                continue
            receipt, state = dict(op.receipt), op.state
            if pull.get("merged"):
                receipt.update(status="deployment_pending", default_branch_modified=True, merge_sha=pull.get("merge_commit_sha"),
                    cleanup_complete=False, deployment_verification_required=True)
                state = "awaiting_deployment"
            elif pull.get("state") == "closed":
                receipt.update(status="pull_request_closed_without_merge", default_branch_modified=False, cleanup_complete=False)
                state = "attention_required"
            WebsiteConnectionOperation.objects.filter(pk=op.pk, state=op.state, updated_at=op.updated_at).update(state=state, receipt=receipt, updated_at=timezone.now())
        except Exception:
            WebsiteConnectionOperation.objects.filter(pk=op.pk, updated_at=op.updated_at).update(
                receipt={**op.receipt, "last_error": "cleanup_merge_status_unavailable"}, updated_at=timezone.now())
        finally:
            if headers:
                try:
                    http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
                except Exception:
                    pass


def approve_cleanup_proposal(config, *, user, data):
    """Open a reviewed, exact-SHA removal PR without restoring ordinary access."""
    from .website_restoration import approve_worker_restoration
    worker = approve_worker_restoration(config, user=user, data=data)
    if worker is not None:
        return worker
    from organizations.models import Organization
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import verify_repository_access
    from .website_contract import WebsiteAuthorityError, connection_contract, evidence_digest
    binding = connection_contract(data)
    metadata = verify_repository_access(user=user, repo=config.github_repo)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        website = WebsiteConnection.objects.select_for_update().get(pk=config.website_connection_id)
        if not binding or binding['website_connection_id'] != str(website.pk) or binding['connection_generation'] != website.generation:
            raise WebsiteAuthorityError('website_connection_changed', 'Refresh the cleanup proposal for the current connection.')
        if metadata['repository_id'] != website.repository_id:
            raise WebsiteAuthorityError('website_repository_changed', 'Repository identity changed.')
        from uuid import UUID
        try:
            operation_id = UUID(str(data.get('operation_id')))
        except (ValueError, TypeError):
            raise WebsiteAuthorityError('cleanup_proposal_required', 'Select a valid cleanup proposal.')
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation_id, connection=website, action='cleanup', generation=website.generation).first()
        if op is None:
            raise WebsiteAuthorityError('cleanup_proposal_required', 'Prepare a current cleanup proposal first.')
        if op.state in {'awaiting_merge', 'awaiting_deployment', 'completed'} and op.receipt.get('pr_url'):
            return op
        if op.state not in {'review_required', 'applying'} or data.get('source_sha') != op.receipt.get('source_sha') or data.get('proposal_digest') != op.receipt.get('proposal_digest'):
            raise WebsiteAuthorityError('cleanup_proposal_changed', 'Review the current cleanup proposal before creating a pull request.')
        if op.state == 'applying' and op.next_attempt_at and op.next_attempt_at > timezone.now():
            raise WebsiteAuthorityError('cleanup_in_progress', 'Cleanup is already being reconciled.', status=409, retryable=True)
        op.state = 'applying'
        op.attempts += 1
        claim_attempt = op.attempts
        op.next_attempt_at = timezone.now() + timedelta(minutes=5)
        op.payload = {**op.payload, 'approved_cleanup': {
            'source_sha': op.receipt['source_sha'], 'proposal_digest': op.receipt['proposal_digest'],
            'deletions': list(op.receipt.get('deletions') or []), 'approved_by_user_id': str(user.pk)}}
        op.save(update_fields=['state', 'attempts', 'next_attempt_at', 'payload', 'updated_at'])
    # All GitHub reads/writes below happen after the approval transaction exits.
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    fresh = _cleanup_proposal(op)
    if fresh['proposal_digest'] != op.receipt['proposal_digest']:
        op.receipt = fresh
        op.state = 'review_required'
        op.save(update_fields=['state', 'receipt', 'updated_at'])
        raise WebsiteAuthorityError('cleanup_source_changed', 'Website files changed. Prepare and review a fresh cleanup proposal.')
    deletions = fresh['deletions']
    if not deletions:
        raise WebsiteAuthorityError('cleanup_no_owned_files', 'No unchanged, exclusively owned files can be removed automatically.')
    token = create_installation_access_token(installation_id=metadata['installation_id'], repository=website.github_repo, repository_id=website.repository_id,
        permission_mode='write', use_cache=False)
    headers = {'Authorization': f'Bearer {token.token}', 'Accept': 'application/vnd.github+json'}
    base = f'https://api.github.com/repos/{website.github_repo}'
    def request(method, path, body=None):
        response = http_client.request(method, base + path, headers=headers, json=body, timeout=(3, 20))
        response.raise_for_status()
        return response.json()
    branch = f'codex/website-cleanup-{op.pk.hex}'
    try:
        base_commit = request('GET', f"/git/commits/{fresh['source_sha']}")
        tree = request('POST', '/git/trees', {'base_tree': base_commit['tree']['sha'],
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'sha': None} for path in deletions]})
        existing = http_client.get(base + f'/git/ref/heads/{branch}', headers=headers, timeout=(3, 15))
        if existing.status_code == 404:
            commit = request('POST', '/git/commits', {'message': 'Remove reviewed MLAI integration files',
                'tree': tree['sha'], 'parents': [fresh['source_sha']]})
            request('POST', '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
        else:
            existing.raise_for_status()
            commit = request('GET', f"/git/commits/{existing.json()['object']['sha']}")
            if commit['tree']['sha'] != tree['sha']:
                raise WebsiteAuthorityError('cleanup_branch_changed', 'The cleanup branch changed. Review it before continuing.')
        existing_prs = http_client.get(base + '/pulls', params={'head': website.github_repo.split('/')[0] + ':' + branch, 'state': 'all'}, headers=headers, timeout=(3, 15))
        existing_prs.raise_for_status()
        prs = existing_prs.json()
        pr = prs[0] if prs else request('POST', '/pulls', {'title': 'Remove reviewed MLAI website integration files',
            'head': branch, 'base': website.branch,
            'body': 'Removes only unchanged files recorded as exclusively created by MLAI. Shared files, modified files, and retained dependencies are preserved. Review this pull request before merging.'})
        op.state = 'awaiting_merge'
        op.receipt = {**fresh, 'status': 'pull_request_opened', 'pr_url': pr['html_url'], 'branch': branch,
            'head_sha': commit['sha'], 'repository_modified': True, 'default_branch_modified': False, 'approved_by_user_id': str(user.pk)}
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=config.organization_id)
            current = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
            saved = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
            if current.generation != op.generation or saved.state != 'applying' or saved.attempts != claim_attempt:
                op.state = 'attention_required'
                op.receipt['status'] = 'authority_changed_after_pr_creation'
            saved.state, saved.receipt = op.state, op.receipt
            saved.save(update_fields=['state', 'receipt', 'updated_at'])
            return saved
    except WebsiteAuthorityError:
        raise
    except Exception as exc:
        raise WebsiteAuthorityError('cleanup_pull_request_unavailable', 'Cleanup pull request could not be confirmed. Retry the same proposal to reconcile its branch.') from exc
    finally:
        try:
            http_client.delete('https://api.github.com/installation/token', headers=headers, timeout=(3, 10))
        except Exception:
            pass


def _process_source_reverification(identifier, now):
    """Re-scan external changes and then authenticate current CI/live proof."""
    from .website_connections import authority_guard, require_unlocked_remote_call
    from .website_contract import WebsiteAuthorityError
    from .website_verification import discover_source_attestation, record_ci_attestation, verify_live_deployment
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers, _create_local_run
    from .website_operations import reserve_workflow_operation, bind_operation_run
    from integrations import http_client
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(pk=identifier, state="pending").first()
        if op is None or op.next_attempt_at and op.next_attempt_at > now:
            return None
        op.attempts += 1
        attempt = op.attempts
        op.next_attempt_at = now + timedelta(minutes=5)
        op.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
    binding = {**contract_for(op.connection), "domain": op.connection.organization.domain,
        "connection_generation": getattr(op, "generation", op.connection.generation),
        "github_repo": op.connection.github_repo, "expected_source_sha": op.payload["source_sha"]}
    state, receipt = "pending", dict(op.receipt)
    try:
        with authority_guard(binding, action="read") as website:
            target = website.targets.filter(generation=website.generation, target_key=op.payload.get("target_id")).first()
            if target is None and not op.payload.get("target_id") and receipt.get("scan_run_id"):
                config = OrganizationContentConfig.objects.get(website_connection=website)
                if config.default_publish_target_id:
                    target = website.targets.filter(generation=website.generation,
                        target_key=config.default_publish_target_id).first()
        # First-time scaffold merges have no publishing target yet. Their owned
        # merge receipt proves provenance, but cannot replace the inventory scan
        # that discovers the newly committed integration.
        scan_required = bool(op.payload.get("scan_required") or target is None)
        if scan_required and target is not None and not receipt.get("scan_run_id"):
            owned_merge = _find_owned_merge(website, website.organization_id, op.payload["source_sha"])
            if owned_merge:
                with authority_guard(binding, action="read") as current:
                    if owned_merge["connection_identity"] != _merge_connection_identity(current):
                        raise WebsiteAuthorityError("website_connection_changed", "Website authority changed during merge verification.", retryable=True)
                    updated_payload = {**op.payload, "owned_merge_run_id": owned_merge["run_id"], "scan_required": False}
                    changed = WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt,
                        state="pending", generation=current.generation).update(payload=updated_payload, updated_at=timezone.now())
                    if changed != 1:
                        raise WebsiteAuthorityError("website_operation_cancelled", "The source verification operation changed.")
                    op.payload = updated_payload
                    scan_required = False
        if scan_required and not receipt.get("scan_run_id"):
            require_unlocked_remote_call()
            remote = _content_factory_remote_config()
            if not remote["enabled"]:
                raise WebsiteAuthorityError("source_reverification_unavailable", "Repository scanning is unavailable.", retryable=True)
            scan_payload = {**binding, "client_request_id": f"source-rescan:{op.pk}", "force_refresh": True}
            scan_op = reserve_workflow_operation(website, workflow="repo_scan", payload=scan_payload)
            response = http_client.post(f"{remote['base_url']}/api/runs/scan", json=scan_payload,
                headers=_content_factory_headers(), timeout=(3, 30))
            response.raise_for_status()
            remote_run = response.json()
            if not remote_run.get("run_id"):
                raise WebsiteAuthorityError("source_reverification_unavailable", "The scan dispatch could not be confirmed.", retryable=True)
            run = _create_local_run(workflow="repo_scan", domain=website.organization.domain,
                github_repo=website.github_repo, payload=scan_payload, remote_data=remote_run)
            bind_operation_run(scan_op, run)
            receipt["scan_run_id"] = run.run_id
        if target is None:
            raise WebsiteAuthorityError("verified_target_required", "Waiting for a current verified publishing target.", retryable=True)
        proof = discover_source_attestation(website, target, op.payload["source_sha"])
        ci = record_ci_attestation(proof)
        config = OrganizationContentConfig.objects.get(website_connection=website)
        deployment = verify_live_deployment(config, data=ci.receipt)
        state, receipt = "completed", {**receipt, "status": "verified", "source_sha": op.payload["source_sha"],
            "ci_operation_id": str(ci.pk), "deployment_operation_id": str(deployment.pk), "repository_modified": False}
    except WebsiteAuthorityError as exc:
        receipt.update(status="verification_pending", code=exc.code, retryable=exc.retryable)
        if exc.code in {"website_disconnected", "website_connection_changed", "website_operation_cancelled"}:
            state = "cancelled"
    except Exception:
        receipt.update(status="verification_pending", code="source_reverification_unavailable", retryable=True)
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8))), updated_at=timezone.now())
    return state
