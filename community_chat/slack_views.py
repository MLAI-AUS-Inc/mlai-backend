"""One-click Slack DM migration controls for Community Home."""

from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hospital.authentication import CustomJWTAuthentication
from integrations.models import SlackDmMirrorGrant
from integrations.services.slack_dm_mirror import (
    REQUIRED_SCOPES,
    SlackDmMirrorError,
    activate_connection,
    pause_grant,
    resume_grant,
    revoke_grant,
    slack_connection_for_user,
    status_payload,
)
from integrations.views import mint_connector_connect_ticket

from .authentication import (
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
)
from .throttles import CommunityChatScopedThrottle


class SlackDmMirrorView(APIView):
    """Inspect, connect, pause, resume, or disconnect Slack DM mirroring."""

    authentication_classes = (
        CommunityChatAccountAuthentication,
        CommunityChatBootstrapAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        return Response(status_payload(request.user))

    def post(self, request):
        connection = slack_connection_for_user(request.user)
        if connection is not None and REQUIRED_SCOPES.issubset(set(connection.scopes or [])):
            try:
                activate_connection(connection)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
            return Response(status_payload(request.user), status=status.HTTP_200_OK)

        connect_url = self._authorization_url(request)
        payload = status_payload(request.user)
        payload["authorization_url"] = connect_url
        payload["consent"] = {
            "version": SlackDmMirrorGrant.CONSENT_VERSION,
            "summary": (
                "Mirror all one-to-one Slack DMs visible to your Slack account into "
                "private, owner-controlled conversations in MLAI Chat. The other person "
                "does not need to link Slack and cannot see your imported copy unless "
                "they link independently. Up to 30 days of history is imported; DMs are "
                "excluded from Roo, organization memory, public search, and analytics."
            ),
        }
        return Response(payload, status=status.HTTP_200_OK)

    @staticmethod
    def _authorization_url(request):
        ticket = mint_connector_connect_ticket(request.user, "slack")
        frontend = str(settings.COMMUNITY_CHAT_FRONTEND_URL).strip().rstrip("/")
        next_url = f"{frontend}/home?slack=connected"
        path = reverse("connector_connect", kwargs={"provider": "slack"})
        return request.build_absolute_uri(
            f"{path}?{urlencode({'ticket': ticket, 'next': next_url})}"
        )

    def patch(self, request):
        grant = SlackDmMirrorGrant.objects.filter(user=request.user).order_by("-updated_at").first()
        if grant is None:
            return Response({"error": "slack_not_linked"}, status=status.HTTP_404_NOT_FOUND)
        action = str(request.data.get("action") or "").strip().lower()
        if action == "pause":
            pause_grant(grant)
        elif action == "resume":
            try:
                resume_grant(grant)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
        else:
            raise ValidationError({"action": "Use pause or resume."})
        return Response(status_payload(request.user))

    def delete(self, request):
        grant = SlackDmMirrorGrant.objects.filter(user=request.user).order_by("-updated_at").first()
        if grant is not None:
            revoke_grant(grant)
        return Response(status=status.HTTP_204_NO_CONTENT)
