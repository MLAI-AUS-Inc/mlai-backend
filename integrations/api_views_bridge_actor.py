"""Read-only identity handoff from the Chat bridge to Public Roo."""

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasStrictRooApiKey
from integrations.services.community_bridge.roo_actor import BridgeActorError, resolve_roo_actor


class CommunityBridgeRooActorView(APIView):
    """Require Roo's credential before exposing the verified message author."""

    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def get(self, request):
        """Resolve the original actor for an exact Slack app_mention."""
        try:
            actor = resolve_roo_actor(**{
                name: request.query_params.get(name, "")
                for name in ("workspace_id", "channel_id", "message_id", "thread_ts", "bridge_user_id")
            })
        except BridgeActorError as exc:
            return Response({"error": exc.code}, status=exc.status)
        return Response(actor, headers={"Cache-Control": "no-store"})
