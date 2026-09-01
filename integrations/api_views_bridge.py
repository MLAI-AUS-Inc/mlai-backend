import json
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.models import CommunityBridgePlatform
from integrations.services.community_bridge.buzz import BuzzBridgeClient
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.store import ingest_inbound_event, ingest_slack_event
from integrations.services.slack_dm_mirror import ingest_mlai_dm_event, ingest_slack_dm_event


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

        result = ingest_slack_dm_event(payload) or ingest_slack_event(payload)
        logger.info(
            "community_bridge_slack_event status=%s receipt_id=%s delivery_ids=%s",
            result.get("status"),
            result.get("receipt_id"),
            result.get("delivery_ids")
            or ([result.get("delivery_id")] if result.get("delivery_id") else []),
        )
        return Response({"ok": True, "status": result.get("status", "accepted")}, status=status.HTTP_200_OK)


class BuzzCommunityBridgeEventView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.META.get("HTTP_X_MLAI_BRIDGE_SIGNATURE", "")
        timestamp = request.META.get("HTTP_X_MLAI_BRIDGE_TIMESTAMP", "")
        raw_body = request.body or b""
        if len(raw_body) > 256 * 1024:
            return Response(
                {"error": "payload_too_large"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not BuzzBridgeClient.validate_callback_signature(raw_body, timestamp, signature):
            return Response({"error": "invalid_signature"}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return Response({"error": "invalid_json"}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(payload, dict):
            return Response({"error": "invalid_payload"}, status=status.HTTP_400_BAD_REQUEST)
        normalized_event = payload.get("normalized_event")
        if not isinstance(normalized_event, dict):
            return Response({"error": "invalid_payload"}, status=status.HTTP_400_BAD_REQUEST)

        private_result = ingest_mlai_dm_event(payload)
        if private_result is not None:
            return Response(
                {"ok": True, "status": private_result.get("status", "accepted")},
                status=status.HTTP_200_OK,
            )

        result = ingest_inbound_event(
            source_platform=CommunityBridgePlatform.BUZZ,
            receipt_key=str(payload.get("receipt_key") or ""),
            source_channel_id=str(payload.get("source_channel_id") or ""),
            event_type=str(payload.get("event_type") or ""),
            normalized_event=normalized_event,
            raw_payload=(
                payload.get("raw_payload")
                if isinstance(payload.get("raw_payload"), dict)
                else {}
            ),
        )
        logger.info(
            "community_bridge_buzz_event status=%s receipt_id=%s delivery_id=%s",
            result.get("status"),
            result.get("receipt_id"),
            result.get("delivery_id"),
        )
        return Response({"ok": True, "status": result.get("status", "accepted")}, status=status.HTTP_200_OK)
