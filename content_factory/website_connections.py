"""Website lifecycle authority shared by browser, service and scheduler paths."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import hashlib
import re
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response

from organizations.models import Organization
from .models import OrganizationContentConfig
from .website_models import (
    WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget,
    WebsiteRepositoryMutation, WebsiteScanSnapshot, WebsiteTemplateRevision,
)
from .website_contract import (
    CAPABILITY_KEYS, CONNECTION_FIELDS, WebsiteAuthorityError, connection_contract,
    evidence_digest, safe_repository_path, sanitized_evidence, template_validation,
    validate_authority,
)
from .portable_drafts import REPOSITORY_CONFIG_FIELDS, portable_run_update_allowed


REPOSITORY_WORKFLOWS = frozenset({
    "repo_scan", "content_factory_scan", "article_system_setup", "direct_generate",
    "confirmed_topic", "article_revision", "publish_article", "article_publish",
    "publish_bundle", "component_revision", "article_generation", "scaffold_articles",
})


_owner_contract = ContextVar("website_owner_contract", default=None)
_authority_depth = ContextVar("website_authority_depth", default=0)


@contextmanager
def owner_operation_scope(payload):
    """Retain the reviewed identity across remote calls without retaining locks."""
    token = _owner_contract.set(dict(payload))
    try:
        yield
    finally:
        _owner_contract.reset(token)


def owner_operation_contract():
    """Return the original reviewed identity, never today's replacement binding."""
    return dict(_owner_contract.get() or {})


def owner_write_guard(payload=None):
    """Fence a local persistence phase after remote work against revocation."""
    original = payload if payload is not None else owner_operation_contract()
    return authority_guard(original, action="read") if connection_contract(original) else nullcontext()


def guarded_local_run_write(method):
    """Guard local-only run projection helpers, including background poll writes."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        run = kwargs.get("run") or (args[0] if args else None)
        payload = scoped_run_contract(run) if run is not None else owner_operation_contract()
        try:
            with owner_write_guard(payload):
                return method(*args, **kwargs)
        except WebsiteAuthorityError:
            if owner_operation_contract():
                raise
            if run is not None and getattr(run, "pk", None):
                run.refresh_from_db()
            return run
    return wrapped


def require_unlocked_remote_call():
    """Reject a cross-service call that could reenter our authority transaction."""
    explicit_transactions = [block for block in transaction.get_connection().atomic_blocks
        if not getattr(block, "_from_testcase", False)]
    if _authority_depth.get() or explicit_transactions:
        raise RuntimeError("Content Factory HTTP must run outside database transactions")


def contract_for(connection):
    """Project only non-secret identifiers for a worker operation."""
    return {
        "website_connection_id": str(connection.pk),
        "connection_generation": connection.generation,
        "repository_id": connection.repository_id,
        "github_repo": connection.github_repo,
        "app_root": connection.app_root, "branch": connection.branch,
        "domain": connection.organization.domain,
    }


def summary_for(config, *, company_id=None):
    """Canonical UI capability summary; null means legacy, false means denied."""
    connection = getattr(config, "website_connection", None) if config else None
    if connection is None:
        return None
    capabilities = {key: bool((connection.capabilities or {}).get(key)) for key in CAPABILITY_KEYS}
    if connection.state != "connected":
        capabilities["publishingReady"] = False
    if connection.state not in {"connected", "paused"}:
        capabilities = {key: False for key in CAPABILITY_KEYS}
        actions = ["reconnect"]
    else:
        actions = ["scan", "disconnect", "reset"]
        if connection.state == "connected":
            actions.append("setup")
        if capabilities["generationReady"]:
            actions.append("generate")
        if capabilities["previewSupported"]:
            actions.append("preview")
        if capabilities["publishingReady"]:
            actions.append("publish")
        actions.append("pause" if connection.state == "connected" else "reconnect")
    if connection.repository_mutations.exists():
        actions.append("cleanup")
    target_blocked = connection.app_root or any(item.get("code") in {"APPLICATION_ROOT_VERIFICATION_REQUIRED", "SETUP_BRANCH_VERIFICATION_REQUIRED"} for item in connection.blockers)
    if target_blocked:
        capabilities.update(publishingReady=False, previewSupported=False)
        actions = [action for action in actions if action not in {"setup", "publish", "preview"}]
    latest = connection.operations.order_by("-created_at").first()
    summary = {
        "latestOperation": {"id": str(latest.pk), "action": latest.action, "state": latest.state, "receipt": latest.receipt} if latest else None,
        "connectionId": str(connection.pk), "connectionGeneration": connection.generation,
        "companyId": str(company_id) if company_id else None,
        "repositoryId": connection.repository_id, "githubRepo": connection.github_repo,
        "appRoot": connection.app_root, "branch": connection.branch, "siteUrl": connection.site_url,
        "status": connection.state, "capabilities": capabilities, "allowedActions": actions,
        "blockers": connection.blockers, "verifiedSha": connection.verified_sha or None,
        "configurationVersion": connection.configuration_version,
    }

    from .website_rollout import apply_repository_write_policy
    return apply_repository_write_policy(summary, domain=connection.organization.domain)


def _payload_with_context(data):
    payload = dict(data or {})
    for nested_key in ("run_request", "request"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            nested_contract = connection_contract(nested)
            explicit = connection_contract(payload)
            if explicit and nested_contract and explicit != nested_contract:
                raise WebsiteAuthorityError("website_contract_conflict", "Conflicting website connection identities.")
            for key in (*CONNECTION_FIELDS, "domain", "github_repo", "expected_source_sha", "source_sha", "repo_head_sha"):
                if key not in payload and key in nested:
                    payload[key] = nested[key]
    return payload


def observed_source_sha(connection):
    """Most recently observed selected-branch identity, independent of consent."""
    snapshot = connection.scan_snapshots.filter(generation=connection.generation, detector_version="github_head").order_by("-created_at").first()
    return snapshot.source_sha if snapshot else ""


def check_source_identity(connection, payload, *, required=False):
    """Deny stale source work without treating an ordinary push as disconnection."""
    from .website_contract import SHA_PATTERN
    expected = str(payload.get("expected_source_sha") or payload.get("source_sha") or payload.get("repo_head_sha") or payload.get("last_scanned_sha") or "")
    if expected and not SHA_PATTERN.fullmatch(expected):
        raise WebsiteAuthorityError("invalid_source_sha", "An exact source commit is required.", status=422)
    observed = observed_source_sha(connection)
    if required and observed and not expected:
        raise WebsiteAuthorityError("website_source_required", "Current source identity is required for this repository operation.")
    if observed and expected and expected != observed:
        # Webhooks can arrive out of order. Only GitHub's current selected ref
        # may supersede an observation; a worker cannot simply assert a new SHA.
        verify_repository_head(connection, expected)
        WebsiteScanSnapshot.objects.create(connection=connection, generation=connection.generation,
            run_id=f"head-check:{uuid.uuid4()}", source_sha=expected, detector_version="github_head",
            fingerprint=evidence_digest({"source_sha": expected}), evidence={"source": "github_head_verification"})
    return expected


def read_repository_native_target(connection):
    """Read immutable identity and the provider's current default branch."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    try:
        credential = create_installation_access_token(installation_id=connection.installation_id,
            repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read")
        response = http_client.get(f"https://api.github.com/repos/{connection.github_repo}",
            headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}, timeout=(3, 15))
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise WebsiteAuthorityError("github_source_unavailable", "GitHub could not verify the current repository target. Retry when GitHub is available.", status=503, retryable=True) from exc


def verify_repository_native_target(connection):
    """Native mutation adapters currently support only the root/default branch.

    Inventory remains available for every selection. Comparing live branch names
    is necessary even when selected and default branches happen to share a SHA.
    """
    if connection.app_root:
        raise WebsiteAuthorityError("APPLICATION_ROOT_VERIFICATION_REQUIRED", "This application root can be scanned, but native website changes need an adapter verified for that root.")
    metadata = read_repository_native_target(connection)
    if (metadata.get("id") != connection.repository_id
            or str(metadata.get("full_name") or "").casefold() != connection.github_repo.casefold()):
        raise WebsiteAuthorityError("website_repository_changed", "The immutable GitHub repository identity changed.")
    if not metadata.get("default_branch") or metadata["default_branch"] != connection.branch:
        raise WebsiteAuthorityError("SETUP_BRANCH_VERIFICATION_REQUIRED", "This branch can be scanned, but native website changes need an adapter verified for this selected branch.")


def verify_repository_head(connection, expected_sha):
    """Recheck GitHub's selected branch before promoting publication readiness."""
    from urllib.parse import quote
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    try:
        credential = create_installation_access_token(installation_id=connection.installation_id, repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read")
        response = http_client.get(f"https://api.github.com/repos/{connection.github_repo}/commits/{quote(connection.branch, safe='')}",
            headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}, timeout=(3, 15))
        response.raise_for_status()
        current_sha = str(response.json().get("sha") or "")
    except Exception as exc:
        raise WebsiteAuthorityError("github_source_unavailable", "GitHub could not verify the current repository source. Retry when GitHub is available.", status=503, retryable=True) from exc
    if current_sha != expected_sha:
        raise WebsiteAuthorityError("website_source_changed", "The repository changed since this verification. Verify the current source again.")
    return current_sha


@contextmanager
def authority_guard(data, *, action="read", domain="", github_repo="", require_selected=True):
    """Serialize consent checks with disconnect and hold the fence through a DB write.

    Network consumers must recheck before each external mutation; a completed
    authorization read is not a lease allowing subsequent writes after revocation.
    """
    payload = _payload_with_context(data)
    contract = connection_contract(payload)
    if not contract:
        raise WebsiteAuthorityError("website_connection_required", "Reload the web app or update MLAI, then reconnect this website to continue.")
    domain = str(domain or payload.get("domain") or "").lower().strip()
    github_repo = str(github_repo or payload.get("github_repo") or "").strip()
    with transaction.atomic():
        candidate = WebsiteConnection.objects.filter(pk=contract["website_connection_id"]).first()
        if candidate is None:
            raise WebsiteAuthorityError("website_connection_not_found", "Website connection was not found.")
        # All write paths lock org before connection/config, including rebind.
        Organization.objects.select_for_update().get(pk=candidate.organization_id)
        connection = WebsiteConnection.objects.select_for_update().select_related("organization").get(pk=candidate.pk)
        validate_authority(connection, payload, action=action, domain=domain, github_repo=github_repo)
        from .website_rollout import require_repository_write_policy
        require_repository_write_policy(action=action, domain=connection.organization.domain)
        if action in {"setup", "publish", "merge", "preview"}:
            verify_repository_native_target(connection)
        if action == "portable":
            from .website_contract import SHA_PATTERN
            expected_source = str(payload.get("expected_source_sha") or payload.get("source_sha") or payload.get("repo_head_sha") or "").lower()
            if expected_source and not SHA_PATTERN.fullmatch(expected_source):
                raise WebsiteAuthorityError("invalid_source_sha", "An exact source commit is required.", status=422)
            observed = str(observed_source_sha(connection) or "").lower()
            if expected_source and observed and expected_source != observed:
                raise WebsiteAuthorityError("website_source_changed", "The reviewed repository source changed. Refresh before continuing.")
        else:
            expected_source = check_source_identity(connection, payload, required=action in {"publish", "merge"})
        if action in {"publish", "merge"} or (action == "preview" and expected_source):
            verify_repository_head(connection, expected_source or connection.verified_sha)
        if require_selected and not OrganizationContentConfig.objects.filter(
            organization_id=connection.organization_id, website_connection=connection, github_repo__iexact=connection.github_repo,
        ).exists():
            raise WebsiteAuthorityError("website_connection_changed", "This website is no longer selected for repository work.")
        target_key = contract.get("connection_target_id")
        if not target_key and action in {"publish", "merge"}:
            target_key = OrganizationContentConfig.objects.filter(website_connection=connection).values_list("default_publish_target_id", flat=True).first()
        target = connection.targets.filter(target_key=target_key, generation=connection.generation).first() if target_key else None
        if target_key and target is None:
            raise WebsiteAuthorityError("website_target_changed", "The publishing target changed. Scan this website again.")
        if action in {"publish", "merge"} and (target is None or not target.verified_at or not target.capabilities.get("publishingReady") or target.source_sha != connection.verified_sha):
            raise WebsiteAuthorityError("website_target_verification_required", "Select a verified publishing target for this repository.")
        token = _authority_depth.set(_authority_depth.get() + 1)
        try:
            yield connection
        finally:
            _authority_depth.reset(token)


def scoped_run_contract(run):
    """Never retrofit old run consent from today's selected connection."""
    payload = _payload_with_context(getattr(run, "run_request", None) or {})
    payload.setdefault("domain", getattr(run, "domain", ""))
    payload.setdefault("github_repo", getattr(run, "github_repo", ""))
    return payload


def guarded_service_write(action, *, only_repository=False, remote_actions=(), portable=False):
    """Fence legacy service handlers without weakening their existing permissions."""
    def decorate(method):
        @wraps(method)
        def wrapped(self, request, *args, **kwargs):
            data = request.query_params if request.method == "GET" else request.data
            payload = dict(data.items())
            if kwargs.get("run_id"):
                payload["run_id"] = kwargs["run_id"]
            if portable:
                from workflow_runs.models import ContentFactoryRun
                run_id = str(payload.get("run_id") or payload.get("job_id") or "")
                with transaction.atomic():
                    original = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id).first() if run_id else None
                    if portable_run_update_allowed(original, payload, event_type=str(payload.get("event_type") or payload.get("event") or "")):
                        return method(self, request, *args, **kwargs)
            if only_repository and not needs_repository_authority(payload):
                return method(self, request, *args, **kwargs)
            try:
                if kwargs.get("action") in {"cancel", "deny"}:
                    return method(self, request, *args, **kwargs)
                effective_action = "setup" if kwargs.get("action") == "resume" else action
                with authority_guard(payload, action=effective_action) as connection:
                    from workflow_runs.models import ContentFactoryRun
                    run_id = str(kwargs.get("run_id") or payload.get("run_id") or payload.get("job_id") or "")
                    existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first() if run_id else None
                    if existing_run:
                        if existing_run.organization_id != connection.organization_id:
                            raise WebsiteAuthorityError("website_scope_mismatch", "Run does not belong to this website company.")
                        saved_contract = connection_contract(existing_run.run_request or {})
                        if not saved_contract or any(saved_contract.get(key) != value for key, value in connection_contract(payload).items() if key in {"website_connection_id", "connection_generation", "repository_id"}):
                            raise WebsiteAuthorityError("website_run_changed", "This run was not dispatched for the current website connection.")
                    if action == "config_write":
                        if REPOSITORY_CONFIG_FIELDS.intersection(payload):
                            source_payload = {**payload, **(payload.get("repository_inventory") if isinstance(payload.get("repository_inventory"), dict) else {})}
                            check_source_identity(connection, source_payload, required=True)
                        validate_template_update(payload)
                    if kwargs.get("action") not in remote_actions:
                        response = method(self, request, *args, **kwargs)
                    else:
                        response = None
                    if action == "config_write" and response is not None and response.status_code < 300:
                        record_scan_evidence(connection, payload)
                        if run_id:
                            run = ContentFactoryRun.objects.filter(run_id=run_id).first()
                            if run:
                                run.run_request = {**(run.run_request or {}), **connection_contract(payload)}
                                run.save(update_fields=["run_request", "updated_at"])
                    if response is not None:
                        return response
                # These actions synchronously call the worker, which calls our
                # authority/token/config APIs back. Release every row lock first.
                with owner_operation_scope(payload):
                    return method(self, request, *args, **kwargs)
            except WebsiteAuthorityError as exc:
                return Response(exc.as_dict(), status=exc.status)
        return wrapped
    return decorate


def queue_website_followup(kind, *, data, arguments):
    """Persist a callback follow-up without worker HTTP inside the callback lock."""
    if kind not in {"publish_article", "trigger_article_generation", "confirm_topic"}:
        raise ValueError("Unsupported website follow-up")
    binding = {**connection_contract(data), "domain": str(data.get("domain") or "")}
    with authority_guard(binding, action="config_write") as website:
        arguments = sanitized_evidence(arguments)
        if kind == "trigger_article_generation":
            article = dict(arguments.get("article_request") or {})
            original = connection_contract(article)
            if original and any(binding.get(key) != value for key, value in original.items()):
                raise WebsiteAuthorityError("website_connection_changed", "Pending article belongs to a previous website connection.")
            arguments["article_request"] = {**article, **binding, "github_repo": website.github_repo,
                "client_request_id": article.get("client_request_id") or f"callback:{data.get('job_id')}:{kind}"}
        elif kind == "confirm_topic":
            arguments["source_run_id"] = str(data.get("job_id") or "")
        digest = evidence_digest({"kind": kind, "job_id": data.get("job_id"), "dedupe_key": data.get("dedupe_key"), "arguments": arguments})
        operation, _ = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"{website.pk}:followup:{digest}", defaults={
                "connection": website, "generation": website.generation, "action": "worker_followup",
                "payload": {"kind": kind, "arguments": arguments, "binding": binding, "source_run_id": str(data.get("job_id") or ""), "callback_dedupe_key": str(data.get("dedupe_key") or "")},
                "receipt": {"status": "queued", "repository_modified": False},
            })
        return operation




def needs_repository_authority(data):
    """Research-only callbacks remain independent of website publishing consent."""
    if any(key in data for key in CONNECTION_FIELDS) or REPOSITORY_CONFIG_FIELDS.intersection(data) or (data.get("github_repo") and not data.get("workflow")):
        return True
    if str(data.get("workflow") or "") in REPOSITORY_WORKFLOWS:
        return True
    event = str(data.get("event_type") or data.get("event") or "")
    if event.startswith(("scan_", "scaffold_", "article_system_setup_", "generation_", "preview_", "publish_", "article_")):
        return True
    run_id = data.get("run_id") or data.get("job_id")
    if run_id:
        from workflow_runs.models import ContentFactoryRun
        return ContentFactoryRun.objects.filter(run_id=run_id, workflow__in=REPOSITORY_WORKFLOWS).exists()
    return False


def archive_repository_templates(config, connection=None):
    """Keep templates under their original repository identity before invalidation."""
    connection = connection or config.website_connection
    if connection:
        for purpose in ("article_template", "design_guide", "resource_prompt"):
            body = getattr(config, purpose) or ""
            if body:
                verdict = template_validation(body)
                WebsiteTemplateRevision.objects.get_or_create(connection=connection, purpose=purpose,
                    digest=hashlib.sha256(body.encode()).hexdigest(), defaults={"generation": connection.generation,
                    "body": body, "provenance": "legacy_saved", "status": "validated" if verdict["valid"] else "quarantined", "validation": verdict})


def invalidate_repository_config(config, *, archive_templates=True):
    """Clear only repository-derived projections; retain company/editorial facts."""
    if archive_templates:
        archive_repository_templates(config)
    fields = []
    for name in REPOSITORY_CONFIG_FIELDS:
        try:
            field = config._meta.get_field(name)
        except Exception:
            continue
        value = "" if name in {"article_path_pattern", "registry_path"} else field.get_default()
        setattr(config, name, value)
        fields.append(name)
    config.save(update_fields=[*fields, "updated_at"])
    # Source may remain in immutable snapshots, but mutable library projections
    # must not cross a repository or generation boundary.
    from .models import GeneratedComponent, ComponentMapping, WebsiteDesignSnapshot
    GeneratedComponent.objects.filter(organization=config.organization).delete()
    ComponentMapping.objects.filter(organization=config.organization).delete()
    WebsiteDesignSnapshot.objects.filter(organization=config.organization, is_active=True).update(is_active=False, status="superseded")


def verify_repository_access(*, user, repo):
    """Verify exact selected-repo access and immutable identity before binding."""
    from integrations import http_client
    from integrations.services.github_installations import installation_for_repo
    from integrations.services.github_app import create_installation_access_token
    inst = installation_for_repo(user, repo)
    if inst is None:
        raise WebsiteAuthorityError("github_authorization_required", "Authorize the GitHub App for this repository first.")
    try:
        credential = create_installation_access_token(installation_id=inst.installation_id, repository=repo, permission_mode="read")
        response = http_client.get(f"https://api.github.com/repos/{repo}", headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}, timeout=(3, 15))
        response.raise_for_status()
        metadata = response.json()
        if not isinstance(metadata.get("id"), int) or metadata["id"] < 1 or str(metadata.get("full_name") or "").casefold() != repo.casefold():
            raise ValueError("Repository identity mismatch")
    except Exception as exc:
        raise WebsiteAuthorityError("github_repository_unavailable", "GitHub could not verify access to this selected repository. Reconnect GitHub and retry.") from exc
    return {"repository_id": metadata["id"], "github_repo": metadata["full_name"], "installation_id": str(inst.installation_id), "branch": str(metadata.get("default_branch") or "")}


def bind_website(config, *, user, repo, app_root="", branch="", site_url="", reconnect=False, expected=None):
    """Explicitly authorize a selected website; stale work cannot call this path."""
    repo = str(repo or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[-1] in {".", ".."}:
        raise WebsiteAuthorityError("invalid_github_repository", "Choose a GitHub owner/repository.", status=400)
    app_root = safe_repository_path(app_root, allow_empty=True)
    if len(app_root) > 500:
        raise WebsiteAuthorityError("invalid_repository_path", "Application root is too long.", status=400)
    branch = str(branch or "").strip()
    if branch and (len(branch) > 255 or branch.startswith(("-", "/")) or branch.endswith(("/", ".", ".lock")) or any(piece in branch for piece in ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "[")) or any(ord(char) < 32 for char in branch)):
        raise WebsiteAuthorityError("invalid_repository_branch", "Use a valid branch name for the selected repository.", status=400)
    metadata = verify_repository_access(user=user, repo=repo)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        current = config.website_connection
        if reconnect and current and not connection_contract(expected or {}):
            raise WebsiteAuthorityError("website_connection_required", "Review the current connection before reconnecting.")
        if expected and current:
            contract = connection_contract(expected)
            if contract and (contract["website_connection_id"] != str(current.pk) or contract["connection_generation"] != current.generation):
                raise WebsiteAuthorityError("website_connection_changed", "The connection changed during authorization. Refresh and retry.")
        same = bool(current and current.repository_id == metadata["repository_id"] and current.app_root == app_root
                    and current.branch == (branch or metadata["branch"]) and current.site_url == (site_url or config.organization.domain))
        if same and current.state == "connected" and not reconnect:
            return current
        if same and current.state != "connected" and not reconnect:
            raise WebsiteAuthorityError("website_reconnect_required", "Explicitly reconnect this website before continuing.")
        if current:
            archive_repository_templates(config, current)
        elif any(getattr(config, key) for key in ("article_template", "design_guide", "resource_prompt")):
            legacy = WebsiteConnection.objects.create(organization=config.organization, github_repo=config.github_repo or repo,
                site_url=config.organization.domain, state="disconnected", blockers=[{"code": "legacy_archived", "message": "Historical templates require fresh repository verification."}])
            archive_repository_templates(config, legacy)
        if current:
            transition_connection(config, action="reconnect" if same else "disconnect", expected=contract_for(current), idempotency_key=f"bind:{uuid.uuid4()}", verified_reconnect=same)
            current.refresh_from_db()
        if same:
            connection = current
            connection.installation_id = metadata["installation_id"]
            connection.authorized_by = user
            connection.save(update_fields=["installation_id", "authorized_by", "updated_at"])
        else:
            connection = WebsiteConnection.objects.create(organization=config.organization, authorized_by=user, app_root=app_root,
                site_url=site_url or config.organization.domain, **{**metadata, "branch": branch or metadata["branch"]})
        target_blocker = None
        if connection.app_root:
            target_blocker = {"code": "APPLICATION_ROOT_VERIFICATION_REQUIRED", "message": "Inventory is available. Native website changes need an adapter verified for this application root."}
        elif connection.branch != metadata["branch"]:
            target_blocker = {"code": "SETUP_BRANCH_VERIFICATION_REQUIRED", "message": "Inventory is available. Native website changes need an adapter verified for this selected branch."}
        if target_blocker:
            connection.blockers = [*connection.blockers, target_blocker]
            connection.save(update_fields=["blockers", "updated_at"])
        config.website_connection = connection
        invalidate_repository_config(config, archive_templates=False)
        config.github_repo = metadata["github_repo"]
        config.github_installation_id = metadata["installation_id"]
        config.save(update_fields=["website_connection", "github_repo", "github_installation_id", "updated_at"])
        return connection


def transition_connection(config, *, action, expected, idempotency_key="", verified_reconnect=False):
    """Commit revocation before async cancellation, retaining customer content."""
    if action not in {"pause", "disconnect", "reconnect", "reset", "cleanup", "revoke"}:
        raise WebsiteAuthorityError("invalid_connection_action", "Unknown website action.", status=400)
    if action == "reconnect" and not verified_reconnect:
        raise WebsiteAuthorityError("github_verification_required", "Verify GitHub access before reconnecting.")
    contract = connection_contract(expected)
    if len(idempotency_key) > 200:
        raise WebsiteAuthorityError("invalid_operation_key", "Operation key is too long.", status=400)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        if not config.website_connection_id or not contract:
            raise WebsiteAuthorityError("website_connection_required", "Connect this website before changing its lifecycle.")
        connection = WebsiteConnection.objects.select_for_update().get(pk=config.website_connection_id)
        key = f"{connection.pk}:{idempotency_key or uuid.uuid4()}"
        existing = WebsiteConnectionOperation.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.action != action:
                raise WebsiteAuthorityError("operation_key_conflict", "This operation key was used for another action.")
            return existing
        if str(connection.pk) != contract["website_connection_id"] or connection.generation != contract["connection_generation"]:
            raise WebsiteAuthorityError("website_connection_changed", "The website connection changed. Refresh and retry.")
        if action == "cleanup":
            # A cleanup request proposes a patch; it is never authority to push
            # after disconnection or to delete untracked customer files.
            return WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation,
                idempotency_key=key, action=action, state="pending", payload={"mutation_ids": [str(x) for x in connection.repository_mutations.values_list("id", flat=True)]},
                receipt={"status": "proposal_requested", "requires_review": True, "repository_modified": False})
        old_generation = connection.generation
        connection.operations.filter(action="worker_followup", generation=old_generation, state="pending").update(
            state="cancelled", receipt={"status": "authority_revoked", "repository_modified": False}, updated_at=timezone.now())
        connection.generation += 1
        connection.configuration_version += 1
        connection.state = {"pause": "paused", "disconnect": "disconnected", "revoke": "revoked", "reconnect": "connected", "reset": connection.state}[action]
        connection.disconnected_at = timezone.now() if action in {"disconnect", "revoke"} else None
        connection.capabilities = {**(connection.capabilities or {}), "publishingReady": False, "previewSupported": False} if action == "pause" else {}
        connection.verified_sha = ""
        connection.last_verified_at = None
        connection.blockers = [{"code": "scan_required", "message": "Scan the current repository to verify its capabilities.", "retryable": False}] if action in {"reset", "reconnect"} else []
        connection.save()
        config.publish_targets = []
        config.default_publish_target_id = None
        config.auto_publish = False
        config.daily_discovery_enabled = False
        config.article_system = {**(config.article_system or {}), "publish_disconnected_at": timezone.now().isoformat()}
        config.save(update_fields=["publish_targets", "default_publish_target_id", "auto_publish", "daily_discovery_enabled", "article_system", "updated_at"])
        if action in {"reset", "reconnect"}:
            invalidate_repository_config(config)
        if action in {"disconnect", "revoke"}:
            from .models import ResearchAutomation
            ResearchAutomation.objects.filter(organization=connection.organization, status="active").update(status="paused", updated_at=timezone.now())
        from workflow_runs.models import ContentFactoryRun
        scoped_runs = ContentFactoryRun.objects.filter(organization=connection.organization, workflow__in=REPOSITORY_WORKFLOWS).filter(
            Q(run_request__website_connection_id=str(connection.pk)) | Q(github_repo__iexact=connection.github_repo, run_request__website_connection_id__isnull=True))
        runs = list(scoped_runs.exclude(status__in=["completed", "failed", "cancelled", "denied"]).values_list("run_id", flat=True))
        # A terminal run can still own a hosted preview. Stop its runtime without
        # changing completed article history or claiming that a PR was undone.
        stop_preview_runs = list(scoped_runs.values_list("run_id", flat=True))
        pending_auto_merge_prs = []
        for saved in ContentFactoryRun.objects.filter(organization=connection.organization, github_repo__iexact=connection.github_repo).values_list("result", flat=True):
            if not isinstance(saved, dict):
                continue
            if saved.get("publish_auto_merge_state") or saved.get("auto_merge") or saved.get("auto_merge_enabled"):
                nested = saved.get("article_system_setup") if isinstance(saved.get("article_system_setup"), dict) else {}
                url = saved.get("pr_url") or nested.get("pr_url")
                if url:
                    pending_auto_merge_prs.append(str(url))
        # Keep the records and remote outcome evidence; this is a durable consent
        # cancellation, not a claim that an already-issued remote write vanished.
        ContentFactoryRun.objects.filter(run_id__in=runs).update(status="cancelled", error="Website connection changed.", resume_available=False, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation, idempotency_key=key,
            action=action, state="pending", payload={"cancel_run_ids": runs, "stop_preview_run_ids": stop_preview_runs, "previous_generation": old_generation, "disable_auto_merge_prs": sorted(set(pending_auto_merge_prs))},
            receipt={"authority_revoked": action != "reconnect", "repository_modified": False,
                "retained": ["published_articles", "website_files", "company_details", "editorial_policy", "history"],
                "artifact_retention": {"status": "retained", "erasure_performed": False,
                    "categories": ["worker_run_artifacts", "worker_checkpoints", "generated_media", "repository_copies"]},
                "remote_cleanup_pending": True})



def offboard_website_connections(organization, *, user=None, purge=False):
    """Revoke departing-owner authority and purge backend website evidence.

    Only minimal provider/cancellation references survive until the durable
    revocation outbox completes. Worker artifacts follow their separate retention
    policy. A shared company's other owner's grant remains.
    """
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config = OrganizationContentConfig.objects.filter(organization=organization).first()
        websites = WebsiteConnection.objects.select_for_update().filter(organization=organization)
        if not purge:
            websites = websites.filter(authorized_by=user)
        for website in list(websites):
            if config and config.website_connection_id == website.pk:
                transition_connection(config, action="revoke", expected=contract_for(website))
                website.refresh_from_db()
            elif website.state not in {"revoked", "disconnected"}:
                previous_generation = website.generation
                website.generation += 1
                website.state = "revoked"
                website.capabilities = {}
                website.verified_sha = ""
                website.save(update_fields=["generation", "state", "capabilities", "verified_sha", "updated_at"])
                WebsiteConnectionOperation.objects.create(connection=website, generation=website.generation,
                    idempotency_key=f"{website.pk}:offboard:{uuid.uuid4()}", action="revoke",
                    payload={"previous_generation": previous_generation, "cancel_run_ids": [], "disable_auto_merge_prs": []})
            if not purge:
                continue
            website.scan_snapshots.all().delete()
            website.template_revisions.all().delete()
            website.targets.all().delete()
            website.repository_mutations.all().delete()
            # Cleanup approval ceases when the owning company is deleted.
            website.operations.filter(Q(action__in=["cleanup", "worker_followup"]) | ~Q(state="pending")).delete()
            pending = list(website.operations.filter(state="pending"))
            for operation in pending:
                operation.payload = {key: operation.payload[key] for key in
                    ("previous_generation", "cancel_run_ids", "stop_preview_run_ids", "disable_auto_merge_prs") if key in operation.payload}
                operation.payload["purge_after_reconciliation"] = True
                operation.receipt = {"authority_revoked": True, "website_database_evidence_erased": True,
                    "database_erasure_scope": ["scan_snapshots", "template_revisions", "publication_targets", "mutation_receipts"],
                    "artifact_retention": {"status": "retained", "erasure_performed": False,
                        "categories": ["worker_run_artifacts", "worker_checkpoints", "generated_media", "repository_copies"]},
                    "repository_modified": False, "remote_cleanup_pending": True}
                operation.save(update_fields=["payload", "receipt", "updated_at"])
            if pending:
                website.authorized_by = None
                website.site_url = ""
                website.app_root = ""
                website.blockers = []
                website.save(update_fields=["authorized_by", "site_url", "app_root", "blockers", "updated_at"])
            else:
                website.delete()


def record_scan_evidence(connection, data):
    """Promote validated snapshots only while the caller holds the consent lock."""
    data = sanitized_evidence(data)
    inventory = data.get("repository_inventory") if isinstance(data.get("repository_inventory"), dict) else {}
    sha = str(data.get("source_sha") or data.get("repo_head_sha") or data.get("commit_sha") or data.get("last_scanned_sha") or inventory.get("source_sha") or inventory.get("repo_head_sha") or "")
    from .website_contract import SHA_PATTERN
    if sha and not SHA_PATTERN.fullmatch(sha):
        raise WebsiteAuthorityError("invalid_source_sha", "Repository evidence requires an exact source commit.", status=422)
    if sha:
        evidence = {key: data[key] for key in ("repository_inventory", "tech_stack", "repo_execution_contract", "article_system", "publish_targets", "scan_summary", "capability_snapshot") if key in data}
        fingerprint = evidence_digest(evidence)
        WebsiteScanSnapshot.objects.get_or_create(connection=connection, generation=connection.generation,
            run_id=str(data.get("run_id") or data.get("job_id") or f"config:{fingerprint}"), fingerprint=fingerprint,
            defaults={"source_sha": sha, "detector_version": str(data.get("detector_version") or "legacy"), "evidence": evidence})
        caps = dict(connection.capabilities or {})
        if connection.verified_sha and connection.verified_sha != sha:
            caps.update(publishingReady=False, previewSupported=False)
        if inventory.get("discovery_complete") is True or any(key in data for key in ("tech_stack", "article_system")):
            caps["inventoryReady"] = True
        # Direct publishing needs an adapter's verification evidence at this SHA;
        # a detected file/legacy scaffold cache alone is not verification.
        targets = data.get("publish_targets") if isinstance(data.get("publish_targets"), list) else []
        verified = False
        preview = False
        native_allowed = True
        if any(isinstance(target, dict) and (target.get("verification") or {}).get("status") in {"passed", "verified", "preview_verified"} for target in targets):
            try:
                verify_repository_native_target(connection)
            except WebsiteAuthorityError as exc:
                if exc.code not in {"APPLICATION_ROOT_VERIFICATION_REQUIRED", "SETUP_BRANCH_VERIFICATION_REQUIRED"}:
                    raise
                native_allowed = False
                connection.blockers = [item for item in connection.blockers if item.get("code") != exc.code] + [{"code": exc.code, "message": str(exc)}]
        for target in targets:
            if not isinstance(target, dict) or not target.get("target_id"):
                continue
            proof = target.get("verification") if isinstance(target.get("verification"), dict) else {}
            ready = native_allowed and proof.get("status") in {"passed", "verified"} and proof.get("source_sha") == sha
            verified |= bool(ready and target.get("publish_capability") in {"direct", "hook"})
            preview_ready = native_allowed and proof.get("status") == "preview_verified" and proof.get("base_sha") == sha and bool(SHA_PATTERN.fullmatch(str(proof.get("source_sha") or "")))
            preview |= bool((ready or preview_ready) and proof.get("preview_capable"))
            WebsiteConnectionTarget.objects.update_or_create(connection=connection, target_key=str(target["target_id"]), defaults={
                "generation": connection.generation, "adapter": str(target.get("delivery_adapter") or ""),
                "adapter_version": str(target.get("adapter_version") or ""), "source_sha": sha, "contract": target,
                "capabilities": {"publishingReady": bool(ready)}, "verified_at": timezone.now() if ready else None})
        if verified:
            check_source_identity(connection, {"source_sha": sha}, required=True)
            verify_repository_head(connection, sha)
            connection.blockers = [item for item in connection.blockers if item.get("code") != "repository_source_changed"]
        if "publish_targets" in data:
            caps["publishingReady"] = verified and connection.state == "connected"
            caps["previewSupported"] = preview
        connection.capabilities = caps
        connection.verified_sha = sha
        connection.last_verified_at = timezone.now()
    for purpose in ("article_template", "design_guide", "resource_prompt"):
        body = data.get(purpose)
        if body is None:
            continue
        validation = template_validation(body)
        digest = hashlib.sha256(str(body).encode()).hexdigest()
        WebsiteTemplateRevision.objects.get_or_create(connection=connection, purpose=purpose, digest=digest, defaults={
            "generation": connection.generation, "source_sha": sha, "provenance": str(data.get("template_provenance") or "worker_generated"),
            "status": "validated" if validation["valid"] else "quarantined", "body": body, "validation": validation})
    config = OrganizationContentConfig.objects.get(website_connection=connection)
    caps = dict(connection.capabilities or {})
    caps["generationReady"] = all(template_validation(getattr(config, field))["valid"] for field in ("article_template", "design_guide"))
    connection.capabilities = caps
    connection.save(update_fields=["capabilities", "verified_sha", "last_verified_at", "blockers", "updated_at"])


def validate_template_update(data):
    """Bad optional generation cannot replace a previously valid template."""
    for purpose in ("article_template", "design_guide", "resource_prompt"):
        if purpose not in data:
            continue
        verdict = template_validation(data[purpose])
        if not verdict["valid"]:
            raise WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", f"{purpose}: {verdict['message']}", status=422,
                field_errors=[{"artifact": purpose, "path": purpose, "code": verdict["code"], "message": verdict["message"]}])


def dispatch_contract(domain, payload, *, action="read", source_run_id=""):
    """Bind a new dispatch, or preserve immutable consent from its source run."""
    from workflow_runs.models import ContentFactoryRun
    if source_run_id:
        run = ContentFactoryRun.objects.filter(run_id=source_run_id).first()
        if run is None:
            from .models import ContentFactoryJob
            job = ContentFactoryJob.objects.filter(job_id=source_run_id).first()
            if job is None:
                raise WebsiteAuthorityError("website_source_run_required", "This repository operation requires its original run.")
            binding = {**(job.request_meta or {}), "domain": job.domain}
        else:
            binding = scoped_run_contract(run)
        if not domain:
            domain = binding.get("domain") or ""
    else:
        config = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(organization__domain=domain).first()
        if not config or not config.website_connection:
            raise WebsiteAuthorityError("website_connection_required", "Connect the selected website before repository work.")
        supplied = connection_contract(payload)
        binding = dict(payload) if supplied else contract_for(config.website_connection)
    binding = {**binding, "domain": domain, "github_repo": payload.get("github_repo") or binding.get("github_repo")}
    with authority_guard(binding, action=action):
        return connection_contract(binding)



def validate_setup_merge_source(run, head_sha):
    """Only merge the precise setup commit whose preview was verified."""
    from .website_contract import SHA_PATTERN
    binding = connection_contract(scoped_run_contract(run))
    if not binding or not SHA_PATTERN.fullmatch(str(head_sha or "")):
        raise WebsiteAuthorityError("setup_verification_required", "Verify this setup pull request before publishing.")
    website = WebsiteConnection.objects.get(pk=binding["website_connection_id"])
    for target in website.targets.filter(generation=binding["connection_generation"]):
        proof = target.contract.get("verification") or {}
        if proof.get("status") == "preview_verified" and proof.get("source_sha") == head_sha and proof.get("base_sha") == website.verified_sha:
            verify_repository_head(website, website.verified_sha)
            return
    raise WebsiteAuthorityError("setup_verification_required", "The setup pull request changed or its exact preview has not passed verification.")


def validate_publish_merge_source(run, head_sha, branch):
    """Require the exact article commit recorded by this publishing run.

    Successful checks alone do not authorize an external contributor's later
    push to an MLAI pull request. The merge CAS below this guard must match the
    applied, generation-bound ownership receipt, not merely a result URL.
    """
    from .website_contract import SHA_PATTERN
    binding = connection_contract(scoped_run_contract(run))
    if not binding or not branch or not SHA_PATTERN.fullmatch(str(head_sha or "")):
        raise WebsiteAuthorityError("publish_commit_unverified", "This publication needs an exact recorded repository commit before merging.")
    owned = WebsiteRepositoryMutation.objects.filter(
        connection_id=binding["website_connection_id"],
        generation=binding["connection_generation"], run_id=run.run_id,
        status="applied", branch=branch, head_sha=head_sha,
    ).exists()
    if not owned:
        raise WebsiteAuthorityError("publish_commit_unverified", "The publication pull request changed or its exact commit has no applied ownership receipt. Review and regenerate it before merging.")


def guarded_backend_run_action(action):
    """Fence direct backend GitHub mutations, including background auto-polls."""
    def decorate(method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            run = kwargs.get("run")
            try:
                with authority_guard(scoped_run_contract(run), action=action):
                    return method(*args, **kwargs)
            except WebsiteAuthorityError as exc:
                return {"outcome": "error", "detail": str(exc), "code": exc.code, "checks": {}, "run": run}
        return wrapped
    return decorate
