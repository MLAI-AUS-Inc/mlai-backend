"""Owner-scoped website management and internal worker authorization APIs."""

from functools import wraps
import uuid

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .website_contract import WebsiteAuthorityError, connection_contract, sanitized_evidence
from .website_connections import (
    authority_guard, bind_website, contract_for, scoped_run_contract,
    summary_for, transition_connection, owner_operation_scope,
    run_action_authority,
)


def _context(request):
    from .vibe_marketing_views import _resolve_context_or_response, _explicit_company_scope_required_response, _get_config
    context, error = _resolve_context_or_response(request)
    if error:
        return None, None, error
    error = _explicit_company_scope_required_response(request, context)
    return context, _get_config(context.organization), error


def guarded_owner_operation(action, *, bind_selected=False, run_operation=False, local_only=False):
    """Check browser-provided generation before entering legacy mutation paths."""
    def decorate(method):
        @wraps(method)
        def wrapped(self, request, *args, **kwargs):
            from .vibe_marketing_views import _run_belongs_to_context
            from workflow_runs.models import ContentFactoryRun
            context, config, error = _context(request)
            if error:
                return error
            try:
                from .portable_drafts import explicit_portable_request, original_portable_run
                if not run_operation and action == "read" and explicit_portable_request(request.data):
                    expected = connection_contract(request.data)
                    if expected:
                        current = config.website_connection
                        if current is None or any(connection_contract(contract_for(current)).get(key) != value for key, value in expected.items() if key != "connection_target_id"):
                            raise WebsiteAuthorityError("website_connection_changed", "The reviewed website connection changed. Refresh before continuing.")
                        with authority_guard({**dict(request.data), "domain": context.organization.domain, "github_repo": current.github_repo}, action="portable"):
                            pass
                    return method(self, request, *args, **kwargs)
                run_action = kwargs.get("action")
                if run_operation and run_action in {"cancel", "deny", "refresh-setup-pr-status"}:
                    return method(self, request, *args, **kwargs)
                if run_operation:
                    run_id = kwargs.get("run_id") or (args[0] if args else None)
                    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
                    if run is None or not _run_belongs_to_context(run, context):
                        return Response({"detail": "Run not found."}, status=404)
                    if original_portable_run(run) and run_action not in {"approve", "publish-pr", "promote-bundle", "merge-publish-pr", "retry-preview-quality"}:
                        if connection_contract(request.data):
                            raise WebsiteAuthorityError("website_connection_changed", "A portable draft cannot acquire repository authority.")
                        if any(request.data.get(key) not in (None, "", "content_only") for key in (
                            "delivery_mode", "deliveryMode", "resolved_delivery_mode", "resolvedDeliveryMode",
                        )):
                            raise WebsiteAuthorityError("portable_repository_access_denied", "A confirmed portable draft cannot switch to repository delivery. Start a separately reviewed repository run.")
                        return method(self, request, *args, **kwargs)
                    payload = scoped_run_contract(run)
                    expected = connection_contract(request.data)
                    if expected and any(connection_contract(payload).get(key) != value for key, value in expected.items()):
                        raise WebsiteAuthorityError("website_connection_changed", "This run belongs to a previous website connection.")
                else:
                    requested_repo = str(request.data.get("github_repo") or request.data.get("githubRepo") or config.github_repo or "").strip()
                    expected = connection_contract(request.data)
                    current = config.website_connection
                    newly_bound = False
                    requested_root = request.data.get("appRoot", request.data.get("app_root", current.app_root if current else ""))
                    requested_branch = request.data.get("branch", current.branch if current else "")
                    root_changed = current is not None and str(requested_root or "").strip("/") not in {current.app_root, "." if not current.app_root else current.app_root}
                    branch_changed = current is not None and "branch" in request.data and requested_branch != current.branch
                    if bind_selected and (current is None or current.github_repo.casefold() != requested_repo.casefold() or root_changed or branch_changed):
                        current = bind_website(config, user=request.user, repo=requested_repo,
                            app_root=requested_root, branch=requested_branch, expected=request.data)
                        newly_bound = True
                    if current is None:
                        raise WebsiteAuthorityError("website_connection_required", "Connect this website before continuing.")
                    if not expected and not newly_bound:
                        raise WebsiteAuthorityError("website_connection_required", "Reload the web app or update MLAI, then reconnect this website.")
                    payload = contract_for(current) if newly_bound else expected
                    payload.update(domain=context.organization.domain, github_repo=requested_repo)
                effective_action = action
                if run_operation and run_action:
                    effective_action = run_action_authority(run, run_action)
                with authority_guard(payload, action=effective_action):
                    if local_only:
                        return method(self, request, *args, **kwargs)
                # The worker synchronously reauthorizes dispatch and mutations.
                # Preserve consent identity across HTTP, never the DB row locks.
                with owner_operation_scope(payload):
                    return method(self, request, *args, **kwargs)
            except WebsiteAuthorityError as exc:
                return Response(exc.as_dict(), status=exc.status)
        return wrapped
    return decorate


class WebsiteConnectionView(APIView):
    """Read or explicitly bind the selected company's website repository."""

    def get(self, request):
        context, config, error = _context(request)
        if error:
            return error
        from .website_journey import journey_for_context
        return Response({"websiteConnection": summary_for(config, company_id=context.company.pk), "websiteJourney": journey_for_context(context, config)})

    def post(self, request):
        context, config, error = _context(request)
        if error:
            return error
        try:
            connection = bind_website(config, user=request.user,
                repo=request.data.get("githubRepo") or request.data.get("github_repo") or config.github_repo,
                app_root=request.data.get("appRoot", ""), branch=request.data.get("branch", ""),
                site_url=request.data.get("siteUrl", ""), expected=request.data)
            return Response({"websiteConnection": summary_for(config, company_id=context.company.pk), **contract_for(connection)})
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class WebsiteConnectionActionView(APIView):
    """Independent pause/disconnect/reconnect/reset/cleanup consent actions."""

    def post(self, request, action):
        if action not in {"prepare", "pause", "disconnect", "reconnect", "reset", "cleanup", "purge", "cancel-operation", "reconcile", "verify-access", "verify", "custom-contract", "ci-attestation", "github-revoke", "verify-cleanup"}:
            return Response({"code": "invalid_connection_action", "detail": "Unknown website action."}, status=400)
        context, config, error = _context(request)
        if error:
            return error
        try:
            revision = request.data.get("configuration_revision")
            if action != "disconnect" and revision is not None and config.website_connection and str(revision) != str(config.website_connection.configuration_version):
                raise WebsiteAuthorityError("website_configuration_changed", "Refresh the current website settings before changing them.")
            key = str(request.headers.get("Idempotency-Key") or request.data.get("idempotency_key") or request.data.get("client_request_id") or uuid.uuid4())
            if action == "purge" and request.data.get("preserve_published_articles", True) is not True:
                raise WebsiteAuthorityError("published_content_review_required", "Published articles require a separately reviewed removal proposal.")
            if action == "prepare":
                from .website_prepare import start_prepare, advance_prepare
                operation = start_prepare(config, expected=dict(request.data), company_id=context.company.pk,
                    user=request.user, retry=True)
                advance_prepare(operation.pk)
                operation.refresh_from_db()
            elif action == "disconnect":
                from .website_disconnect import start_disconnect
                operation = start_disconnect(config, user=request.user, data=dict(request.data))
            elif action == "github-revoke":
                from .website_github_revocation import revocation_plan, apply_revocation
                if request.data.get("phase", "plan") == "plan":
                    return Response({"githubRevocation": revocation_plan(request.user)})
                if request.data.get("phase") != "apply":
                    raise WebsiteAuthorityError("github_revocation_phase_invalid", "Choose plan or apply.", status=422)
                operation = apply_revocation(request.user, config, data=request.data, idempotency_key=key)
                if isinstance(operation, dict):
                    return Response({"githubRevocation": operation["githubRevocation"], "receipt": operation})
            elif action == "verify-cleanup":
                from .website_cleanup_verification import verify_cleanup_deployment
                operation = verify_cleanup_deployment(config, data=request.data)
            elif action in {"custom-contract", "ci-attestation"}:
                from .website_support import owner_support_operation
                operation = owner_support_operation(context, config, action=action, data=dict(request.data))
            elif action == "cancel-operation":
                from .website_operations import cancel_operation
                operation = cancel_operation(config, data=request.data, idempotency_key=key)
            elif action in {"verify-access", "verify", "reconcile"}:
                current = config.website_connection
                expected = connection_contract(request.data)
                if current is None or expected != connection_contract(contract_for(current)):
                    # repository_id is optional on owner consent tuples.
                    if current is None or not expected or any(connection_contract(contract_for(current)).get(k) != v for k, v in expected.items()):
                        raise WebsiteAuthorityError("website_connection_changed", "Refresh the current website connection.")
                from .vibe_marketing_views import _article_capabilities_for_context
                _article_capabilities_for_context(context, config, force=True)
                if action == "verify":
                    from .website_verification import verify_live_deployment
                    from .website_support import current_verification_data
                    verification = dict(request.data) if request.data.get("evidence_digest") else current_verification_data(config, dict(request.data))
                    operation = verify_live_deployment(config, data=verification)
                else:
                    operation = None
                if action == "reconcile":
                    from .website_reconciliation import process_website_connection_operations
                    process_website_connection_operations(limit=5, connection_id=current.pk)
            elif action == "cleanup" and (request.data.get("approve_cleanup") is True or request.data.get("approved") is True):
                from .website_reconciliation import approve_cleanup_proposal
                operation = approve_cleanup_proposal(config, user=request.user, data=request.data)
            elif action == "reconnect":
                current = config.website_connection
                if current is None:
                    raise WebsiteAuthorityError("website_connection_required", "Select a website repository first.")
                bind_website(config, user=request.user, repo=current.github_repo, app_root=current.app_root,
                    branch=current.branch, site_url=current.site_url, reconnect=True, expected=request.data)
                from .website_prepare import start_prepare, advance_prepare
                config.refresh_from_db()
                operation = start_prepare(config, company_id=context.company.pk, user=request.user)
                advance_prepare(operation.pk)
                operation.refresh_from_db()
            else:
                operation = transition_connection(config, action=action, expected=request.data,
                    idempotency_key=key)
            config.refresh_from_db()
            from .website_journey import journey_for_context
            from .website_operations import operation_summary
            return Response({"websiteConnection": summary_for(config, company_id=context.company.pk), "websiteJourney": journey_for_context(context, config),
                "operation": operation_summary(operation) if operation else None})
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class WebsiteConnectionOperationView(APIView):
    """Read a receipt only within the explicitly selected founder company."""

    def get(self, request, operation_id):
        context, config, error = _context(request)
        if error:
            return error
        from .website_models import WebsiteConnectionOperation
        from .website_operations import operation_summary
        operation = WebsiteConnectionOperation.objects.filter(pk=operation_id, connection__organization=context.organization).first()
        if operation is None:
            return Response({"detail": "Operation not found."}, status=404)
        return Response({"operation": operation_summary(operation)})


class WebsiteConnectionAuthorizeView(APIView):
    """Check a worker's exact consent tuple; a denial never supplies credentials."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        try:
            action = str(request.query_params.get("action") or "read")
            with authority_guard(dict(request.query_params.items()), action="read" if action == "ci_attestation" else action) as connection:
                if action == "ci_attestation":
                    from .website_verification import ci_attestation_for
                    return Response(ci_attestation_for(connection, dict(request.query_params.items())))
                portable = request.query_params.get("action") == "portable"
                from .website_operations import deletion_epoch
                return Response({"allowed": True, **contract_for(connection), "state": connection.state,
                    "operation_id": request.query_params.get("operation_id"), "operation_attempt": request.query_params.get("operation_attempt", 1),
                    "deletion_epoch": deletion_epoch(connection), "authorized_action": action,
                    "permission_mode": "none" if portable else "read", "capabilities": {} if portable else connection.capabilities,
                    "expected_source_sha": str(request.query_params.get("expected_source_sha") or request.query_params.get("source_sha") or request.query_params.get("repo_head_sha") or "").lower()})
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class WebsiteMutationView(APIView):
    """Persist validated write ownership, idempotently and under the consent fence."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from .website_contract import SHA_PATTERN, evidence_digest, safe_repository_path
        from .website_models import WebsiteRepositoryMutation
        data = sanitized_evidence(dict(request.data))
        try:
            authority = {**data, "expected_source_sha": data.get("expected_source_sha") or data.get("base_sha")}
            # Ledger state describes a Git effect, not a workflow callback.
            # Preserve it in the receipt without treating proposed/applied as
            # a request to transition the original workflow operation.
            authority.pop("status", None)
            mutation_id = str(data.get("mutation_id") or data.get("operation_id") or "").strip()
            if not data.get("mutation_id"):
                # Historical operation_id was a patch hash. Preserve that key
                # while deriving any workflow fence from the saved run.
                authority.pop("operation_id", None)
            with authority_guard(authority, action="config_write") as connection:
                operation_id = mutation_id
                files = data.get("files")
                if not operation_id or len(operation_id) > 160 or not SHA_PATTERN.fullmatch(str(data.get("base_sha") or "")) or not isinstance(files, list) or len(files) > 1000:
                    raise WebsiteAuthorityError("invalid_mutation_ledger", "An operation ID, exact base SHA and file ledger are required.", status=400)
                allowed_fields = {"path", "kind", "ownership", "before_sha256", "after_sha256", "before_blob_sha", "after_blob_sha", "retained_dependencies", "operation", "before_mode", "after_mode"}
                for entry in files:
                    if not isinstance(entry, dict):
                        raise WebsiteAuthorityError("invalid_mutation_ledger", "Each file needs explicit ownership.", status=400)
                    if set(entry) - allowed_fields:
                        raise WebsiteAuthorityError("invalid_mutation_ledger", "File evidence accepts hashes and ownership, never source bytes.", status=400)
                    entry["path"] = safe_repository_path(entry.get("path"))
                    for hash_key in ("before_sha256", "after_sha256"):
                        value = entry.get(hash_key)
                        if value and (not isinstance(value, str) or len(value) != 64 or not all(char in "0123456789abcdefABCDEF" for char in value)):
                            raise WebsiteAuthorityError("invalid_mutation_ledger", "File evidence needs SHA-256 content hashes.", status=400)
                    for hash_key in ("before_blob_sha", "after_blob_sha"):
                        if entry.get(hash_key) and not SHA_PATTERN.fullmatch(str(entry[hash_key])):
                            raise WebsiteAuthorityError("invalid_mutation_ledger", "File evidence needs exact Git blob identities.", status=400)
                    if entry.get("ownership") not in {"created", "shared", "modified"}:
                        raise WebsiteAuthorityError("invalid_mutation_ledger", "Each file needs explicit ownership.", status=400)
                if len(str(data.get("status") or "")) > 32 or len(str(data.get("branch") or "")) > 255 or len(str(data.get("run_id") or "")) > 100 or len(str(data.get("pr_url") or "")) > 1000:
                    raise WebsiteAuthorityError("invalid_mutation_ledger", "Mutation metadata exceeds its supported limits.", status=400)
                if data.get("head_sha") and not SHA_PATTERN.fullmatch(str(data["head_sha"])):
                    raise WebsiteAuthorityError("invalid_mutation_ledger", "Mutation head needs an exact commit SHA.", status=400)
                defaults = {key: data.get(key, "") for key in ("base_sha", "head_sha", "branch", "pr_url", "run_id")}
                defaults.update(connection=connection, generation=connection.generation, files=files,
                    patch_digest=evidence_digest(files), status=str(data.get("status") or "proposed"))
                # Namespacing prevents a caller in one tenant claiming another's
                # operation key, while retries preserve the same ledger identity.
                key = f"{connection.pk}:{operation_id}"
                row, created = WebsiteRepositoryMutation.objects.get_or_create(operation_id=key, defaults=defaults)
                if not created and (row.patch_digest != defaults["patch_digest"] or row.base_sha != defaults["base_sha"]):
                    raise WebsiteAuthorityError("mutation_identity_conflict", "This operation already records a different patch.")
                if not created:
                    for key in ("head_sha", "pr_url", "branch", "status"):
                        if data.get(key) and not (key == "status" and row.status == "applied" and data[key] != "applied"):
                            setattr(row, key, data[key])
                    row.save(update_fields=["head_sha", "pr_url", "branch", "status", "updated_at"])
                return Response({"id": str(row.pk), "created": created, "patch_digest": row.patch_digest}, status=201 if created else 200)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
