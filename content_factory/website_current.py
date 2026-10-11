"""Recover a run's website lease without transferring repository or tenant consent."""
from copy import deepcopy
import re
from uuid import UUID
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_contract import WebsiteAuthorityError, connection_contract


def validate_current_binding(run, current, original, *, domain, repository_id):
    """Rebinding never permits a changed organisation, installation or repository."""
    if (run is None or current is None or original is None or not repository_id
            or str(run.domain).casefold() != str(domain).casefold()
            or run.status in {"cancelled", "denied"}
            or current.state not in {"connected", "paused"}
            or run.organization_id != current.organization_id or run.organization_id != original.organization_id
            or current.repository_id != original.repository_id or current.repository_id != repository_id
            or current.installation_id != original.installation_id
            or str(current.github_repo).casefold() != str(original.github_repo).casefold()):
        raise WebsiteAuthorityError("github_reconnect_required", "Website access changed. Reconnect GitHub to prepare this website.", status=410)


def portable_recovery_request(original):
    """Revoke repository capabilities while retaining the paid drafting inputs."""
    from .portable_drafts import REPOSITORY_CONFIG_FIELDS
    from .website_contract import CONNECTION_FIELDS, SECRET_KEYS
    removed = REPOSITORY_CONFIG_FIELDS | set(CONNECTION_FIELDS) | SECRET_KEYS | {
        "operation_id", "operation_attempt", "deletion_epoch", "connection_contract", "configuration_revision",
        "github_installation_id", "expected_source_sha", "publish_target", "app_root", "branch", "setup_run_id"}
    removed |= {"source_sha", "repo_head_sha", "commit_sha", "head_sha", "verified_sha", "pr_url", "pr_number",
                "pull_request_url", "live_preview", "live_preview_url", "preview_url", "preview_commit_sha", "capabilities",
                "website_connection", "article_system_setup"}
    def clean(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
                if normalized in removed:
                    continue
                result[key] = ("content_only" if normalized in {"delivery_mode", "resolved_delivery_mode",
                    "requested_delivery_mode", "publish_resolution"} else clean(item))
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return {**clean(original),
        "github_repo": "", "delivery_mode": "content_only", "resolved_delivery_mode": "content_only",
        "delivery_mode_confirmed": True, "website_access_lost": True}


def current_run_binding(*, run_id, domain, repository_id):
    """Return and retain a proven current lease; offboarding remains irreversible for a run."""
    from .website_connections import contract_for
    from .website_operations import deletion_epoch
    if isinstance(repository_id, bool):
        repository_id = None
    try:
        repository_id = int(repository_id)
    except (TypeError, ValueError):
        repository_id = 0
    if isinstance(repository_id, bool):
        repository_id = 0
    with transaction.atomic():
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run is None or not run.organization_id:
            raise WebsiteAuthorityError("github_reconnect_required", "The original dispatch could not be verified.", status=410)
        organization_id = run.organization_id
        Organization.objects.select_for_update().get(pk=organization_id)
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id, organization_id=organization_id).first()
        if run is None:
            raise WebsiteAuthorityError("github_reconnect_required", "The original dispatch changed.", status=410)
        saved = deepcopy(run.run_request or {})
        original_contract = connection_contract(saved)
        original = WebsiteConnection.objects.filter(pk=original_contract.get("website_connection_id")).first()
        config = OrganizationContentConfig.objects.filter(organization_id=run.organization_id).first()
        current = WebsiteConnection.objects.select_for_update().filter(pk=config.website_connection_id).first() if config else None
        if (original is None or not original_contract or run.organization_id != original.organization_id
                or str(run.domain).casefold() != str(domain).casefold()
                or repository_id != original_contract.get("repository_id", original.repository_id)):
            raise WebsiteAuthorityError("github_reconnect_required", "The original repository identity could not be verified.", status=410)
        if run.status in {"cancelled", "denied"}:
            raise WebsiteAuthorityError("run_cancelled", "This run was cancelled and cannot recover website access.")
        try:
            operation_id = str(UUID(str(saved.get("operation_id"))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WebsiteAuthorityError("github_reconnect_required", "The original operation could not be verified.", status=410) from exc
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation_id, connection=original).first()
        if (operation is None or operation.payload.get("run_id") not in (None, "", run.run_id)
                or operation.state in {"denied", "deleted"}
                or operation.state == "cancelled" and operation.receipt.get("status") != "authority_revoked"
                or original.operations.filter(action__in=["disconnect", "revoke", "purge"], generation__gt=original_contract["connection_generation"]).exists()):
            raise WebsiteAuthorityError("run_cancelled", "This run's website operation was stopped.")
        if current and saved.get("deletion_epoch", 0) != deletion_epoch(current):
            raise WebsiteAuthorityError("run_cancelled", "This run's website data was deleted.")
        try:
            validate_current_binding(run, current, original, domain=domain, repository_id=repository_id)
            expected_installation = operation.payload.get("installation_id") or saved.get("github_installation_id")
            expected_repository = operation.payload.get("repository_id") or saved.get("repository_id")
            if (not expected_installation or not expected_repository
                    or str(expected_installation) != str(current.installation_id)
                    or str(expected_repository) != str(current.repository_id)):
                raise WebsiteAuthorityError("github_reconnect_required", "The original installation and repository could not be verified.", status=410)
        except WebsiteAuthorityError:
            from .portable_drafts import PORTABLE_WORKFLOWS
            if run.workflow in PORTABLE_WORKFLOWS:
                run.run_request = portable_recovery_request(saved)
                run.result = {**(run.result or {}), "website_access_lost": True}
                run.save(update_fields=["run_request", "result", "updated_at"])
                return {"allowed": False, "http_status": 410, "code": "github_reconnect_required",
                        "detail": "Website access changed. Continue as a portable draft.", "portable_draft": True}
            raise
        if saved.get("deletion_epoch", 0) != deletion_epoch(current):
            raise WebsiteAuthorityError("run_cancelled", "This run's website data was deleted.")
        if operation.generation != current.generation or operation.connection_id != current.pk:
            operation, _ = WebsiteConnectionOperation.objects.get_or_create(
                idempotency_key=f"{operation.pk}:rebind:{current.pk}:{current.generation}", defaults={
                    "connection": current, "generation": current.generation, "action": "workflow", "state": "running",
                    "payload": {**operation.payload, "run_id": run.run_id, "attempt": 1, "deletion_epoch": deletion_epoch(current)},
                    "receipt": {"status": "connection_rebound", "source_operation_id": saved.get("operation_id")}})
        binding = {**contract_for(current), "configuration_revision": current.configuration_version,
                   "github_installation_id": current.installation_id, "operation_id": str(operation.pk),
                   "operation_attempt": operation.payload.get("attempt", 1), "deletion_epoch": deletion_epoch(current)}
        candidate = {**saved, **binding}
        if candidate != saved:
            history = list((run.result or {}).get("connection_rebinds") or [])
            history.append({"from": original_contract, "to": connection_contract(binding), "at": timezone.now().isoformat()})
            run.run_request = candidate
            run.result = {**(run.result or {}), "connection_rebinds": history[-20:]}
            run.save(update_fields=["run_request", "result", "updated_at"])
        return {"allowed": True, "domain": run.domain, "github_repo": current.github_repo,
                "state": current.state, **binding}


class WebsiteCurrentConnectionView(APIView):
    """Service-only recovery of the original run's unchanged repository lease."""
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        try:
            result = current_run_binding(run_id=request.query_params.get("run_id"),
                domain=request.query_params.get("domain"), repository_id=request.query_params.get("repository_id"))
            return Response(result, status=result.get("http_status", 200))
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
