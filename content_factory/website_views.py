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
                if not run_operation and action == "read" and not explicit_portable_request(request.data):
                    from .channel_artifacts import enabled as artifacts_enabled, prepare_generation
                    if artifacts_enabled() and config.website_connection and "_cms_binding" in (config.website_connection.capabilities or {}):
                        prepare_generation(context, config, user=request.user)
                        return method(self, request, *args, **kwargs)
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
                    if "publishing_channel_artifact" in (run.run_request or {}):
                        from .channel_artifacts import artifact_run_control_allowed
                        if action not in {"read", "setup"} or not artifact_run_control_allowed(run, run_action, dict(request.data)):
                            raise WebsiteAuthorityError("publishing_adapter_required", "Use this CMS artifact's editorial controls or its separate publication review.")
                        return method(self, request, *args, **kwargs)
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
                    from .vibe_marketing_views import _github_repo_company_conflict_response
                    conflict = _github_repo_company_conflict_response(context=context, requested_repo=requested_repo)
                    if conflict:
                        return conflict
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
        from .website_status import add_status
        payload = {"websiteConnection": summary_for(config, company_id=context.company.pk), "websiteJourney": journey_for_context(context, config)}
        from .cms_goal_start import current_projection
        try:
            native_goal = current_projection(context, config, user=request.user)
        except WebsiteAuthorityError:
            native_goal = None
        if native_goal is not None: payload["cmsGoal"] = native_goal
        return Response(add_status(payload, context, config, journey=payload["websiteJourney"], user=request.user),
            headers={"Cache-Control": "private, no-store"})

    def post(self, request):
        context, config, error = _context(request)
        if error:
            return error
        try:
            connection = bind_website(config, user=request.user,
                repo=request.data.get("githubRepo") or request.data.get("github_repo") or config.github_repo,
                app_root=request.data.get("appRoot", ""), branch=request.data.get("branch", ""),
                site_url=request.data.get("siteUrl", ""), expected=request.data)
            from .website_publishing_channels import enabled, projection, scope_for, update_choice
            channel = projection(context, config)
            if enabled() and channel is not None and channel["recommendation"] is None:
                try:
                    # bind_website has released its transaction before public HTTP.
                    update_choice(context, config, expected_scope=scope_for(context, config), expected_revision=channel["revision"])
                except WebsiteAuthorityError:
                    # Superseding consent must win without undoing a successful bind.
                    config.refresh_from_db()
                channel = projection(context, config)
            payload = {"websiteConnection": summary_for(config, company_id=context.company.pk), **contract_for(connection)}
            if channel is not None:
                payload["publishingChannel"] = channel
            return Response(payload)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class WebsiteConnectionActionView(APIView):
    """Independent pause/disconnect/reconnect/reset/cleanup consent actions."""

    def get(self, request, action):
        """Read the original owner's CMS access destination without key export."""
        headers = {"Cache-Control": "private, no-store"}
        if action not in {"cms-access", "cms-initial", "cms-unpublish"}:
            return Response({"detail": "Unknown website read."}, status=404, headers=headers)
        context, config, error = _context(request)
        if error:
            return error
        try:
            if action == "cms-unpublish":
                from .cms_unpublish_status import packet
                return Response(packet(context, config, user=request.user, publication_id=request.query_params.get("publication_operation_id"),
                    request_id=request.query_params.get("client_request_id")), headers=headers)
            if action == "cms-initial":
                from .cms_initial_status import packet
                return Response(packet(context, config, user=request.user, operation_id=request.query_params.get("operation_id"),
                    request_id=request.query_params.get("client_request_id")), headers=headers)
            from .cms_owner_access import for_operation, connection_receipt
            payload = for_operation(context, config, user=request.user, operation_id=request.query_params.get("operation_id"))
            from .cms_goal_status import for_context
            current = for_context(context, config, user=request.user)
            if current is not None: payload["websiteStatus"] = current
            if connection_receipt(context, config, user=request.user) != payload["cmsConnectionReceipt"]:
                raise WebsiteAuthorityError("website_connection_changed", "Refresh the current CMS access request.")
            return Response(payload, headers=headers)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status, headers=headers)

    def post(self, request, action):
        if action not in {"prepare", "pause", "disconnect", "reconnect", "reset", "cleanup", "purge", "cancel-operation", "reconcile", "verify-access", "verify", "custom-contract", "ci-attestation", "github-revoke", "verify-cleanup", "inspect-channel", "select-channel", "cms-connect", "cms-restore-access", "cms-onboarding-quote", "cms-connect-and-setup", "cms-restore-initial", "cms-configure", "cms-start-articles", "cms-setup-articles", "cms-unpublish", "cms-review-article", "cms-approve-article", "capture-existing"}:
            return Response({"code": "invalid_connection_action", "detail": "Unknown website action."}, status=400)
        context, config, error = _context(request)
        if error:
            return error
        try:
            if action == "cms-onboarding-quote":
                from .cms_onboarding_offer import for_context
                result = for_context(context, config, user=request.user, data=dict(request.data))
                if result is None: raise WebsiteAuthorityError("publishing_adapter_required", "Initial CMS setup is unavailable.", status=404)
                return Response({"cmsOnboardingOffer": result}, headers={"Cache-Control": "private, no-store"})
            if action in {"cms-connect-and-setup", "cms-restore-initial"}:
                from .cms_initial_start import start, restore
                from .cms_initial_status import packet
                operation = (start if action == "cms-connect-and-setup" else restore)(context, config, user=request.user, data=dict(request.data))
                return Response({"accepted": True, "operationId": str(operation.pk), **packet(context, config, user=request.user, operation_id=str(operation.pk))},
                    status=202, headers={"Cache-Control": "private, no-store"})
            if action == "cms-unpublish":
                from .cms_unpublish_start import start
                from .cms_unpublish_status import packet
                job = start(context, config, user=request.user, data=dict(request.data))
                return Response({"accepted": True, **packet(context, config, user=request.user, request_id=job.payload["intent"]["request_id"])},
                    status=202 if job.state in {"pending", "running"} else 200, headers={"Cache-Control": "private, no-store"})
            if action == "cms-restore-access":
                from .cms_access_start import start
                from .cms_access_status import projection
                job = start(context, config, user=request.user, data=dict(request.data))
                return Response({"cmsAccessOperation": projection(job)}, status=202 if job.state in {"pending", "running"} else 200,
                    headers={"Cache-Control": "private, no-store"})
            if action == "cms-setup-articles":
                from .cms_setup_start import start
                from .cms_goal_status import for_context as native_status
                operation = start(context, config, user=request.user, data=dict(request.data))
                result = {"accepted": True, "operationId": str(operation.pk)}
                selected = native_status(context, config, user=request.user)
                if selected is not None:
                    result["websiteStatus"] = selected
                return Response(result, status=200 if operation.state == "completed" else 202,
                    headers={"Cache-Control": "private, no-store"})
            if action == "cms-start-articles":
                from .cms_goal_start import start, projection
                operation = start(context, config, user=request.user, data=dict(request.data))
                return Response({"cmsGoal": projection(operation, user=request.user)}, status=200 if operation.state == "completed" else 202,
                    headers={"Cache-Control": "private, no-store"})
            if action in {"cms-review-article", "cms-approve-article"}:
                from .cms_article_approval import review, approve
                if action == "cms-review-article":
                    result = review(context, config, user=request.user, data=dict(request.data))
                    return Response({"cmsArticleReview": result}, headers={"Cache-Control": "private, no-store"})
                result = approve(context, config, user=request.user, data=dict(request.data))
                return Response({"cmsArticleReview": result}, status=200 if result["approvalRecorded"] else 202,
                    headers={"Cache-Control": "private, no-store"})
            if action == "cms-configure":
                from .cms_publication_policy import configure, projection
                saved = configure(context, config, user=request.user, data=dict(request.data))
                return Response({"cmsPublicationPolicy": projection(saved),
                    "websiteConnection": summary_for(config, company_id=context.company.pk)}, headers={"Cache-Control": "private, no-store"})
            if action == "capture-existing":
                current = config.website_connection
                if current is None or "_cms_binding" not in (current.capabilities or {}):
                    raise WebsiteAuthorityError("publishing_adapter_required", "Use repository scan for a Git website or choose the current CMS connection.")
                from .publishing_channels import execute
                from .publishing_channel_contract import ChannelRequest
                from .website_operations import operation_summary
                operation = execute("capture_existing", ChannelRequest(context=context, config=config,
                    user=request.user, data=dict(request.data)))
                return Response({"operation": operation_summary(operation)}, headers={"Cache-Control": "private, no-store"})
            if action == "cms-connect":
                from .cms_owner_connection import connect
                from .website_publishing_channels import projection
                from .cms_owner_access import connection_receipt
                connect(context, config, user=request.user, data=dict(request.data))
                config.refresh_from_db()
                return Response({"websiteConnection": summary_for(config, company_id=context.company.pk),
                    "publishingChannel": projection(context, config),
                    "cmsConnectionReceipt": connection_receipt(context, config, user=request.user)}, headers={"Cache-Control": "private, no-store"})
            if action in {"inspect-channel", "select-channel"}:
                from .website_publishing_channels import update_choice
                selected = request.data.get("channel") if action == "select-channel" else None
                if action == "select-channel" and selected is None:
                    raise WebsiteAuthorityError("website_prepare_blocked", "Choose an offered channel.", status=422)
                options = {}
                if action == "select-channel" and "cms_platform" in request.data:
                    from .cms_onboarding_offer import entry_for_context
                    if selected != "cms" or entry_for_context(context, config, user=request.user) is None:
                        raise WebsiteAuthorityError("website_prepare_blocked", "Refresh the initial CMS owner form.", status=422)
                    options["platform"] = request.data["cms_platform"]
                    if options["platform"] is None:
                        raise WebsiteAuthorityError("website_prepare_blocked", "Choose WordPress or Ghost.", status=422)
                record = update_choice(context, config, expected_scope=request.data.get("channel_scope"),
                    expected_revision=request.data.get("channel_revision"), selected=selected, **options)
                return Response({"publishingChannel": record}, headers={"Cache-Control": "private, no-store"})
            current = config.website_connection
            if current and "_cms_binding" in (current.capabilities or {}) and action not in {"disconnect", "purge", "pause"}:
                raise WebsiteAuthorityError("publishing_adapter_required", "Use the CMS connection actions for this website.")
            revision = request.data.get("configuration_revision")
            if action != "disconnect" and revision is not None and config.website_connection and str(revision) != str(config.website_connection.configuration_version):
                raise WebsiteAuthorityError("website_configuration_changed", "Refresh the current website settings before changing them.")
            key = str(request.headers.get("Idempotency-Key") or request.data.get("idempotency_key") or request.data.get("client_request_id") or uuid.uuid4())
            if action == "purge" and request.data.get("preserve_published_articles", True) is not True:
                raise WebsiteAuthorityError("published_content_review_required", "Published articles require a separately reviewed removal proposal.")
            if action == "prepare":
                from .website_prepare import start_prepare, advance_prepare
                operation = start_prepare(config, expected=dict(request.data), company_id=context.company.pk,
                    user=request.user, retry=True, target=request.data.get("goal_target", "ready"), consent=request.data.get("setup_consent"))
                advance_prepare(operation.pk)
                operation.refresh_from_db()
            elif action == "disconnect":
                from .publishing_channels import enabled as channels_enabled, execute as execute_channel
                if current and "_cms_binding" in (current.capabilities or {}):
                    from .publishing_channel_adapters import CmsChannel
                    from .publishing_channel_contract import ChannelRequest
                    operation = CmsChannel().disconnect(ChannelRequest(context=context, config=config,
                        user=request.user, data=dict(request.data)))
                elif channels_enabled():
                    from .publishing_channel_contract import ChannelRequest
                    operation = execute_channel("disconnect", ChannelRequest(context=context, config=config,
                        user=request.user, data=dict(request.data)))
                else:
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
                operation = config.website_connection.operations.filter(generation=config.website_connection.generation,
                    action="source-reverify", state="pending", payload__site_recipe_reconnect=True).first()
                if operation:
                    from .website_reconciliation import process_website_connection_operations
                    process_website_connection_operations(limit=5, connection_id=config.website_connection.pk)
                else:
                    operation = start_prepare(config, company_id=context.company.pk, user=request.user)
                    advance_prepare(operation.pk)
                operation.refresh_from_db()
            else:
                operation = transition_connection(config, action=action, expected=request.data,
                    idempotency_key=key)
            config.refresh_from_db()
            if config.website_connection and "_cms_binding" in (config.website_connection.capabilities or {}):
                from .website_operations import operation_summary
                return Response({"websiteConnection": summary_for(config, company_id=context.company.pk),
                    "operation": operation_summary(operation) if operation else None}, headers={"Cache-Control": "private, no-store"})
            from .website_journey import journey_for_context
            from .website_operations import operation_summary
            from .website_status import add_status
            payload = {"websiteConnection": summary_for(config, company_id=context.company.pk), "websiteJourney": journey_for_context(context, config),
                "operation": operation_summary(operation) if operation else None}
            return Response(add_status(payload, context, config, journey=payload["websiteJourney"], user=request.user))
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
        if operation is None or operation.action in {"goal-usage", "goal-alert", "usage-refresh", "channel-credential", "cms-artifact", "cms-review", "cms-media", "cms-stage", "cms-policy", "cms-approval", "cms-publish", "cms-unpublish", "cms-goal", "cms-goal-refund", "cms-setup", "cms-setup-intent", "cms-setup-refund", "cms-credential-renewal", "cms-initial-connect", "cms-initial-grant"}:
            return Response({"detail": "Operation not found."}, status=404)
        return Response({"operation": operation_summary(operation)})


class WebsiteCostUsageView(APIView):
    """Read recorded accounting for an explicitly selected authorized company."""

    def get(self, request):
        context, _, error = _context(request)
        if error:
            return error
        from . import website_cost_ledger as ledger
        if not ledger.enabled():
            return Response({"detail": "Accounting not available."}, status=404)
        goal_id = request.query_params.get("goal_id")
        if goal_id is not None:
            try:
                parsed = str(uuid.UUID(goal_id))
                if parsed != goal_id:
                    raise ValueError("Noncanonical goal ID")
            except (ValueError, AttributeError, TypeError):
                return Response({"detail": "Invalid goal ID."}, status=400)
        return Response({"usage": ledger.read(company_id=context.company.pk, organization_id=context.organization.pk, goal_id=goal_id)},
            headers={"Cache-Control": "private, no-store"})


class WebsiteConnectionAuthorizeView(APIView):
    """Check a worker's exact consent tuple; a denial never supplies credentials."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        try:
            action = str(request.query_params.get("action") or "read")
            if action == "article_source_stop":
                from .website_article_source_stop import admission_receipt
                return Response(admission_receipt(dict(request.query_params.items())), headers={"Cache-Control": "private, no-store"})
            if action == "article_source_refresh_inspect":
                from .website_article_refresh import admission_receipt
                return Response(admission_receipt(dict(request.query_params.items())), headers={"Cache-Control": "private, no-store"})
            if action == "channel_artifact":
                from .channel_artifacts import admission_receipt
                return Response(admission_receipt(dict(request.query_params.items())), headers={"Cache-Control": "private, no-store"})
            if action == "channel_artifact_read":
                from .channel_artifacts import artifact_read_receipt
                return Response(artifact_read_receipt(dict(request.query_params.items())), headers={"Cache-Control": "private, no-store"})
            with authority_guard(dict(request.query_params.items()), action="read" if action == "ci_attestation" else action) as connection:
                if action == "ci_attestation":
                    from .website_verification import ci_attestation_for
                    return Response(ci_attestation_for(connection, dict(request.query_params.items())))
                portable = request.query_params.get("action") == "portable"
                recipe_inputs = {}
                if action == "custom_contract" and any(key in request.query_params for key in ("site_contract_digest", "site_recipe_version")):
                    from .website_site_recipe import authorization_inputs
                    recipe_inputs["site_recipe_verification"] = authorization_inputs(connection, dict(request.query_params.items()))
                from .website_operations import deletion_epoch
                return Response({"allowed": True, **contract_for(connection), "state": connection.state,
                    "operation_id": request.query_params.get("operation_id"), "operation_attempt": request.query_params.get("operation_attempt", 1),
                    "deletion_epoch": deletion_epoch(connection), "authorized_action": action,
                    "permission_mode": "none" if portable else "read", "capabilities": {} if portable else connection.capabilities,
                    "expected_source_sha": str(request.query_params.get("expected_source_sha") or request.query_params.get("source_sha") or request.query_params.get("repo_head_sha") or "").lower(), **recipe_inputs})
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
