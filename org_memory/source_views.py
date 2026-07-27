import re
from dataclasses import asdict

from django.db.models import Count
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OrgMemoryActorAuthentication
from .connectors.registry import connector_registry
from .control_plane import (
    SourceControlError,
    approve_configuration,
    attach_connection,
    create_preview,
    discover_scopes,
    get_configuration,
    pause_configuration,
    request_backfill,
    request_delete,
    request_runtime_action,
    resume_configuration,
    run_dry_run,
    select_scopes,
    serialize_configuration,
    serialize_preview,
    serialize_scope,
)
from .models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemorySourcePolicy,
)
from .permissions import HasOrgMemoryCapability, HasOrgMemoryServiceScope
from .reconciliation import serialize_connection_health_snapshot


IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _serialize_action(action, *, created=True):
    return {
        "id": str(action.pk),
        "action": action.action,
        "status": action.status,
        "created": created,
        "request_id": action.request_id,
        "requested_at": action.requested_at,
    }


class OrgMemorySourceControlView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (HasOrgMemoryServiceScope, HasOrgMemoryCapability)
    required_service_scope = "source.manage"
    required_actor_capability = "manage_sources"

    def handle_exception(self, exc):
        if isinstance(exc, SourceControlError):
            response_status = status.HTTP_400_BAD_REQUEST
            if exc.code == "not_found":
                response_status = status.HTTP_404_NOT_FOUND
            elif exc.code in {
                "provider_disabled",
                "governance_denied",
                "backfill_not_approved",
            }:
                response_status = status.HTTP_409_CONFLICT
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=response_status,
            )
        if isinstance(exc, ValueError):
            return Response(
                {"detail": str(exc), "code": "invalid_provider"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().handle_exception(exc)

    def configuration(self, request, configuration_id):
        return get_configuration(
            configuration_id,
            request.org_memory_actor.organization,
        )

    def idempotency_key(self, request):
        key = str(request.headers.get("Idempotency-Key", "") or "").strip()
        if key and not IDEMPOTENCY_PATTERN.fullmatch(key):
            raise SourceControlError("Idempotency-Key is invalid.")
        return key or None


class MemoryProviderListView(OrgMemorySourceControlView):
    def get(self, request):
        organization = request.org_memory_actor.organization
        policies = MemorySourcePolicy.objects.filter(
            organization=organization,
            is_active=True,
        )
        policy_counts = {
            row["provider"]: row["count"]
            for row in policies.values("provider").annotate(count=Count("id"))
        }
        providers = []
        for provider in connector_registry.providers():
            definition = connector_registry.definition(provider)
            providers.append(
                {
                    "provider": provider,
                    "label": definition.label,
                    "default_scope_type": definition.default_scope_type,
                    "supports_webhooks": definition.supports_webhooks,
                    "structured_aggregates_only": definition.structured_aggregates_only,
                    "enablement": connector_registry.enablement(organization, provider),
                    "policy_count": policy_counts.get(provider, 0),
                    "conformance_errors": connector_registry.validate_conformance(provider),
                }
            )
        return Response({"providers": providers})


class MemoryProviderConnectView(OrgMemorySourceControlView):
    def post(self, request, provider):
        configuration, created = attach_connection(
            organization=request.org_memory_actor.organization,
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            provider=provider,
            external_connection_id=request.data.get("external_connection_id"),
            google_connection_id=request.data.get("google_connection_id"),
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            serialize_configuration(configuration),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class MemoryConnectionListView(OrgMemorySourceControlView):
    def get(self, request):
        configurations = MemoryConnectionConfiguration.objects.filter(
            organization=request.org_memory_actor.organization,
        ).select_related("external_connection", "google_connection")
        return Response(
            {"connections": [serialize_configuration(row) for row in configurations]}
        )


class MemoryConnectionScopeView(OrgMemorySourceControlView):
    def get(self, request, configuration_id):
        configuration = self.configuration(request, configuration_id)
        scopes = configuration.source_scopes.select_related("policy")
        return Response(
            {
                "connection": serialize_configuration(configuration),
                "scopes": [serialize_scope(scope) for scope in scopes],
            }
        )

    def put(self, request, configuration_id):
        configuration = self.configuration(request, configuration_id)
        rows = request.data.get("scopes")
        if not isinstance(rows, list):
            raise SourceControlError("scopes must be a list.")
        configuration = select_scopes(
            configuration,
            rows,
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_configuration(configuration))


class MemoryConnectionDiscoverView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        configuration = self.configuration(request, configuration_id)
        page = discover_scopes(
            configuration,
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
            cursor=request.data.get("cursor"),
        )
        return Response(
            {
                "scopes": [asdict(scope) for scope in page.scopes],
                "next_cursor": page.next_cursor,
                "warnings": list(page.warnings),
            }
        )


class MemoryConnectionPreviewView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        preview = create_preview(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_preview(preview), status=status.HTTP_201_CREATED)


class MemoryConnectionDryRunView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        preview = run_dry_run(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_preview(preview))


class MemoryConnectionApproveView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        if request.data.get("confirm") is not True:
            raise SourceControlError("Approval requires confirm=true.")
        configuration = approve_configuration(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_configuration(configuration))


class MemoryConnectionBackfillView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        if request.data.get("confirm") is not True:
            raise SourceControlError("Backfill requires confirm=true.")
        action, created = request_backfill(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
            idempotency_key=self.idempotency_key(request),
        )
        return Response(_serialize_action(action, created=created), status=status.HTTP_202_ACCEPTED)


class MemoryConnectionRuntimeActionView(OrgMemorySourceControlView):
    action = None

    def post(self, request, configuration_id):
        scope_external_ids = request.data.get("scope_external_ids") or []
        if not isinstance(scope_external_ids, list):
            raise SourceControlError("scope_external_ids must be a list.")
        action, created = request_runtime_action(
            self.configuration(request, configuration_id),
            action=self.action,
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
            idempotency_key=self.idempotency_key(request),
            scope_external_ids=[str(value) for value in scope_external_ids],
        )
        return Response(_serialize_action(action, created=created), status=status.HTTP_202_ACCEPTED)


class MemoryConnectionSyncView(MemoryConnectionRuntimeActionView):
    action = MemoryActionType.SYNC


class MemoryConnectionReprocessView(MemoryConnectionRuntimeActionView):
    action = MemoryActionType.REPROCESS


class MemoryConnectionPermissionRefreshView(MemoryConnectionRuntimeActionView):
    action = MemoryActionType.REFRESH_PERMISSIONS


class MemoryConnectionPauseView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        configuration = pause_configuration(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_configuration(configuration))


class MemoryConnectionResumeView(OrgMemorySourceControlView):
    def post(self, request, configuration_id):
        configuration = resume_configuration(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
        )
        return Response(serialize_configuration(configuration))


class MemoryConnectionHealthView(OrgMemorySourceControlView):
    def get(self, request, configuration_id):
        configuration = self.configuration(request, configuration_id)
        health = connector_registry.get(configuration.provider).health(configuration)
        pending = configuration.action_requests.filter(
            status__in=(MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING),
        ).values("action").annotate(count=Count("id"))
        latest_daily_snapshot = configuration.daily_health_snapshots.select_related(
            "report"
        ).order_by("-report__report_date", "-updated_at").first()
        return Response(
            {
                "connection": serialize_configuration(configuration),
                "provider_health": asdict(health),
                "enablement": connector_registry.enablement(
                    configuration.organization,
                    configuration.provider,
                ),
                "pending_actions": {
                    row["action"]: row["count"] for row in pending
                },
                "daily_reconciliation": (
                    {
                        "report_id": str(latest_daily_snapshot.report_id),
                        "report_date": latest_daily_snapshot.report.report_date,
                        "report_status": latest_daily_snapshot.report.status,
                        **serialize_connection_health_snapshot(latest_daily_snapshot),
                    }
                    if latest_daily_snapshot
                    else None
                ),
            }
        )


class MemoryConnectionDeleteView(OrgMemorySourceControlView):
    def delete(self, request, configuration_id):
        if request.data.get("confirm") is not True:
            raise SourceControlError("Deletion requires confirm=true.")
        action, created = request_delete(
            self.configuration(request, configuration_id),
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
            idempotency_key=self.idempotency_key(request),
        )
        return Response(_serialize_action(action, created=created), status=status.HTTP_202_ACCEPTED)
