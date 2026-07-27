from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OrgMemoryActorAuthentication
from .permissions import (
    HasActiveOrgMemoryPilotAccess,
    HasOrgMemoryCapability,
    HasOrgMemoryServiceScope,
)


class OrgMemoryActorContextView(APIView):
    """A data-free probe for validating the private-memory trust boundary."""

    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (HasOrgMemoryServiceScope, HasOrgMemoryCapability)
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
