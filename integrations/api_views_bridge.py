import json
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.store import ingest_slack_event


logger = logging.getLogger(__name__)


class SlackCommunityBridgeEventView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.META.get("HTTP_X_SLACK_SIGNATURE", "")
        timestamp = request.META.get("HTTP_X_SLACK_REQUEST_TIMESTAMP", "")
        raw_body = request.body or b""

        if not SlackBridgeClient.validate_signature(raw_body, timestamp, signature):
            return Response({"error": "invalid_signature"}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except ValueError:
            return Response({"error": "invalid_json"}, status=status.HTTP_400_BAD_REQUEST)

        if str(payload.get("type") or "").strip() == "url_verification":
            return Response({"challenge": payload.get("challenge", "")}, status=status.HTTP_200_OK)

        result = ingest_slack_event(payload)
        logger.info(
            "community_bridge_slack_event status=%s receipt_id=%s delivery_id=%s",
            result.get("status"),
            result.get("receipt_id"),
            result.get("delivery_id"),
        )
        return Response({"ok": True, "status": result.get("status", "accepted")}, status=status.HTTP_200_OK)
