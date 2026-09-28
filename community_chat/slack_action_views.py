"""Account-authenticated handoff to Roo's existing, owner-checked Slack actions."""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeMessageLink,
    SlackDmMirrorConversation,
    SlackDmMirrorDelivery,
)
from integrations.services.slack_dm_mirror import (
    active_grant_for_user,
    SlackDmMirrorError,
    _capture_slack_grant_api_authority,
    _lock_slack_grant_api_authority,
)
from integrations.services.community_bridge.formatting import sanitize_slack_message
from integrations.services.community_bridge.slack_actions import supported_action
from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class SlackMessageActionView(APIView):
    """Read a source card or perform one explicitly selected current Roo action."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def get(self, request):
        return self._handoff(request, request.query_params, perform=False)

    def post(self, request):
        return self._handoff(request, request.data, perform=True)

    def _handoff(self, request, data, *, perform):
        if not isinstance(data, Mapping):
            return Response({"detail": "Invalid action request."}, status=400)
        fields = {
            key: str(data.get(key) or "")
            for key in (
                "workspace_id",
                "channel_id",
                "message_ts",
                "thread_ts",
                "action_id",
                "action_hash",
            )
        }
        fields["thread_ts"] = fields["thread_ts"] or fields["message_ts"]
        for key, pattern in (
            ("workspace_id", r"T[A-Z0-9]+"),
            ("channel_id", r"[CDG][A-Z0-9]+"),
            ("message_ts", r"\d+\.\d+"),
            ("thread_ts", r"\d+\.\d+"),
        ):
            if len(fields[key]) > 100 or not re.fullmatch(pattern, fields[key]):
                return Response(
                    {"detail": "Invalid Slack message reference."}, status=400
                )
        if perform and (
            not supported_action(fields["action_id"])
            or not re.fullmatch(r"[a-f0-9]{64}", fields["action_hash"])
        ):
            return Response({"detail": "Unsupported Slack action."}, status=400)
        try:
            grant = active_grant_for_user(request.user)
        except SlackDmMirrorError:
            return Response(
                {"detail": "Connect your Slack account to use this action."}, status=403
            )
        if not grant or grant.slack_workspace_id != fields["workspace_id"]:
            return Response(
                {"detail": "Connect your Slack account to use this action."}, status=403
            )
        conversation = SlackDmMirrorConversation.objects.filter(
            grant=grant,
            slack_conversation_id=fields["channel_id"],
            status="live",
        ).first()
        public = CommunityBridgeChannel.objects.filter(
            enabled=True,
            slack_workspace_id=fields["workspace_id"],
            slack_channel_id=fields["channel_id"],
            destination_platform="buzz",
        ).exists()
        if not (conversation or public):
            return Response(
                {"detail": "This Slack conversation is unavailable."}, status=403
            )
        if not data.get("thread_ts"):
            if conversation:
                metadata = (
                    SlackDmMirrorDelivery.objects.filter(
                        conversation=conversation,
                        source_platform="slack",
                        source_message_id=fields["message_ts"],
                        operation="create",
                    )
                    .values_list("metadata", flat=True)
                    .first()
                    or {}
                )
                fields["thread_ts"] = str(
                    metadata.get("thread_ts") or fields["message_ts"]
                )
            else:
                parent = (
                    CommunityBridgeMessageLink.objects.filter(
                        channel__slack_workspace_id=fields["workspace_id"],
                        source_platform="slack",
                        source_channel_id=fields["channel_id"],
                        source_message_id=fields["message_ts"],
                        destination_platform="buzz",
                    )
                    .values_list("source_parent_message_id", flat=True)
                    .first()
                )
                fields["thread_ts"] = parent or fields["message_ts"]
        base = str(getattr(settings, "ROO_SERVICE_URL", "") or "").rstrip("/")
        key = str(getattr(settings, "ROO_INTERNAL_MENTION_API_KEY", "") or "")
        parsed = urlsplit(base)
        if (
            not key
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return Response(
                {"detail": "Roo actions are not available yet."}, status=503
            )
        # Lock the message, not the individual option: two choices cannot race.
        lock = f"slack-card-action:{fields['workspace_id']}:{fields['channel_id']}:{fields['message_ts']}"
        if perform and not cache.add(lock, True, timeout=180):
            return Response(
                {
                    "detail": "An action is already in progress. Refresh the message shortly."
                },
                status=409,
            )
        try:
            authority = _capture_slack_grant_api_authority(grant)
            with transaction.atomic():
                _lock_slack_grant_api_authority(authority, required_scopes=set())
            # Authorize the request before dispatch, then release the user lock.
            # Roo calls this backend for billing; holding it across that callback
            # would deadlock the same member's points debit. No user OAuth token
            # crosses this boundary: Roo uses its own bot credential.
            result = requests.post(
                base + "/api/chat-actions",
                headers={"Authorization": f"Bearer {key}"},
                json={**fields, "user_id": authority.slack_user_id, "perform": perform},
                timeout=(5, 120),
                allow_redirects=False,
            )
            if result.status_code != 200:
                if perform and result.status_code in {400, 403, 404, 409, 429}:
                    cache.delete(lock)
                return Response(
                    {
                        "detail": "Roo could not accept this action. Refresh the message or open it in Slack."
                    },
                    status=(
                        result.status_code
                        if result.status_code in {403, 404, 409, 429}
                        else 502
                    ),
                )
            payload = result.json()
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, dict):
                return Response(
                    {"detail": "Roo returned an invalid message."}, status=502
                )
            if perform:
                cache.delete(lock)
            return Response(
                {
                    "content": sanitize_slack_message(
                        message,
                        workspace_id=fields["workspace_id"],
                        channel_id=fields["channel_id"],
                    ),
                    "status": "processed" if perform else "ready",
                }
            )
        except SlackDmMirrorError:
            if perform:
                cache.delete(lock)
            return Response(
                {
                    "detail": "Your Slack connection changed. Reconnect before continuing."
                },
                status=403,
            )
        except (requests.RequestException, ValueError):
            # Leave the short lease in place: a timeout may have performed the action.
            return Response(
                {
                    "detail": "Roo has not confirmed the result. Refresh the message before trying again."
                },
                status=503,
            )
