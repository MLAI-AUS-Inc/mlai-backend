from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OrgMemoryActorAuthentication, RooGatewayActorAuthentication
from .authorization import (
    OrganizationAuthorizationError,
    actor_is_active_committee_points_admin,
    resolve_actor_authorization,
)
from .pilot_deployment import actor_has_active_pilot_access
from .permissions import (
    HasActiveOrgMemoryPilotAccess,
    HasCommitteePointsAdminClass,
    HasOrgMemoryCapability,
    HasOrgMemoryServiceScope,
)
from .service_principals import record_service_principal_audit


class OrgMemoryActorContextView(APIView):
    """A data-free probe for validating the private-memory trust boundary."""

    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (
        HasOrgMemoryServiceScope,
        HasOrgMemoryCapability,
        HasCommitteePointsAdminClass,
    )
    required_service_scope = "org_memory.read"
    required_actor_capability = "view_general_memory"

    def get(self, request):
        actor = request.org_memory_actor
        authorization = request.org_memory_authorization
        return Response(
            {
                "organization_id": actor.organization.pk,
                "organization_domain": actor.organization.domain,
                "surface": actor.surface,
                "slack_team_id": actor.slack_team_id,
                "acting_slack_user_id": actor.slack_user_id,
                "user_id": actor.user.pk if actor.user else None,
                "slack_channel_id": actor.slack_channel_id,
                "slack_thread_ts": actor.slack_thread_ts,
                "event_id": actor.event_id,
                "request_id": actor.request_id,
                "membership_id": authorization.membership.pk,
                "capabilities": sorted(authorization.allowed_capabilities),
                "memory_class_access": authorization.memory_class_access,
            }
        )


class OrgMemoryPilotAccessProbeView(APIView):
    """Content-free proof of the complete live Admin Roo access boundary."""

    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (
        HasOrgMemoryServiceScope,
        HasOrgMemoryCapability,
        HasCommitteePointsAdminClass,
        HasActiveOrgMemoryPilotAccess,
    )
    required_service_scope = "org_memory.read"
    required_actor_capability = "view_general_memory"

    def get(self, request):
        if not settings.ORG_MEMORY_QUERY_API_ENABLED:
            return Response(
                {
                    "schema_version": "org-memory-pilot-access-probe-v1",
                    "ready": False,
                    "code": "private_query_api_disabled",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "schema_version": "org-memory-pilot-access-probe-v1",
                "ready": True,
                "code": "active_pilot_access_granted",
            }
        )


class RooGatewayEligibilityView(APIView):
    """Return a content-free Admin Brain routing decision to the Slack gateway."""

    authentication_classes = (RooGatewayActorAuthentication,)
    permission_classes = (HasOrgMemoryServiceScope,)
    required_service_scope = "org_memory.route"

    def post(self, request):
        actor = request.org_memory_actor
        try:
            authorization = resolve_actor_authorization(actor)
        except OrganizationAuthorizationError:
            authorization = None

        has_capability = bool(
            authorization
            and authorization.has_capability("view_general_memory")
        )
        is_committee = actor_is_active_committee_points_admin(actor)
        approved_context_allowed = actor_has_active_pilot_access(
            actor,
            allowed_surfaces=("roo_gateway",),
        )
        eligible = bool(
            settings.ORG_MEMORY_QUERY_API_ENABLED
            and has_capability
            and is_committee
            and approved_context_allowed
        )
        record_service_principal_audit(
            "routing_eligibility_checked",
            principal=request.auth.principal,
            credential=request.auth.credential,
            request_id=actor.request_id,
            remote_address=request.META.get("REMOTE_ADDR") or None,
            metadata={
                "eligible": eligible,
                "approved_context_allowed": approved_context_allowed,
                "policy_version": "roo-unified-routing-v2",
            },
        )
        return Response(
            {
                "schema_version": "roo-admin-routing-eligibility-v1",
                "admin_brain_eligible": eligible,
                # Do not expose which individual policy dimension denied the
                # route; callers receive one aggregate decision only.
                # Retained for wire compatibility with already-deployed Roo
                # gateways. It represents the aggregate approved-context
                # decision, which may now include explicitly approved public
                # channels for pilot admins.
                "private_context_allowed": eligible,
                "policy_version": "roo-unified-routing-v2",
            }
        )
