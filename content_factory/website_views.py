"""Owner-scoped website management and internal worker authorization APIs."""

from functools import wraps
import uuid

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .website_contract import WebsiteAuthorityError, connection_contract, sanitized_evidence
from .website_connections import (
    authority_guard, bind_website, contract_for, scoped_run_contract,
    summary_for, transition_connection,
)


def _context(request):
    from .vibe_marketing_views import _resolve_context_or_response, _explicit_company_scope_required_response, _get_config
    context, error = _resolve_context_or_response(request)
    if error:
        return None, None, error
    error = _explicit_company_scope_required_response(request, context)
    return context, _get_config(context.organization), error


def guarded_owner_operation(action, *, bind_selected=False, run_operation=False):
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
                if not run_operation and action == "read" and str(request.data.get("delivery_mode") or request.data.get("deliveryMode") or config.article_delivery_mode) == "content_only":
                    return method(self, request, *args, **kwargs)
                run_action = kwargs.get("action")
                if run_operation and run_action in {"cancel", "deny", "refresh-setup-pr-status"}:
                    return method(self, request, *args, **kwargs)
                if run_operation:
                    run_id = kwargs.get("run_id") or (args[0] if args else None)
                    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
                    if run is None or not _run_belongs_to_context(run, context):
                        return Response({"detail": "Run not found."}, status=404)
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
                        raise WebsiteAuthorityError("website_connection_required", "Refresh the website connection before continuing.")
                    payload = contract_for(current) if newly_bound else expected
                    payload.update(domain=context.organization.domain, github_repo=requested_repo)
                effective_action = action
                if run_operation and run_action:
                    effective_action = "merge" if run_action == "merge-publish-pr" else "publish" if run_action in {"approve", "publish-pr", "promote-bundle"} else "setup"
                with authority_guard(payload, action=effective_action):
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
        return Response({"websiteConnection": summary_for(config, company_id=context.company.pk)})

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
        if action not in {"pause", "disconnect", "reconnect", "reset", "cleanup"}:
            return Response({"code": "invalid_connection_action", "detail": "Unknown website action."}, status=400)
        context, config, error = _context(request)
        if error:
            return error
        try:
            if action == "cleanup" and request.data.get("approve_cleanup") is True:
                from .website_reconciliation import approve_cleanup_proposal
                operation = approve_cleanup_proposal(config, user=request.user, data=request.data)
            elif action == "reconnect":
                current = config.website_connection
                if current is None:
                    raise WebsiteAuthorityError("website_connection_required", "Select a website repository first.")
                bind_website(config, user=request.user, repo=current.github_repo, app_root=current.app_root,
                    branch=current.branch, site_url=current.site_url, reconnect=True, expected=request.data)
                operation = None
            else:
                operation = transition_connection(config, action=action, expected=request.data,
                    idempotency_key=str(request.headers.get("Idempotency-Key") or request.data.get("client_request_id") or uuid.uuid4()))
            config.refresh_from_db()
            return Response({"websiteConnection": summary_for(config, company_id=context.company.pk),
                "operation": {"id": str(operation.pk), "state": operation.state, "receipt": operation.receipt} if operation else None})
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class WebsiteConnectionAuthorizeView(APIView):
    """Check a worker's exact consent tuple; a denial never supplies credentials."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        try:
            with authority_guard(dict(request.query_params.items()), action=str(request.query_params.get("action") or "read")) as connection:
                return Response({"allowed": True, **contract_for(connection), "state": connection.state, "capabilities": connection.capabilities})
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
            with authority_guard({**data, "expected_source_sha": data.get("expected_source_sha") or data.get("base_sha")}, action="config_write") as connection:
                operation_id = str(data.get("operation_id") or "").strip()
                files = data.get("files")
                if not operation_id or len(operation_id) > 160 or not SHA_PATTERN.fullmatch(str(data.get("base_sha") or "")) or not isinstance(files, list) or len(files) > 1000:
                    raise WebsiteAuthorityError("invalid_mutation_ledger", "An operation ID, exact base SHA and file ledger are required.", status=400)
                allowed_fields = {"path", "kind", "ownership", "before_sha256", "after_sha256", "retained_dependencies", "operation", "before_mode", "after_mode"}
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
