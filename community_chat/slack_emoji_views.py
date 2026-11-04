"""Authenticated workspace emoji metadata for retained Slack shortcodes."""
from django.db import DatabaseError
from django.conf import settings
from rest_framework.response import Response
from slack_sdk.errors import SlackClientError

from .slack_views import SlackDmMirrorApiView, _slack_endpoint_error_response
from .models import CommunityChatDevice
from .onboarding import require_community_access
from integrations.services import slack_custom_emoji as emoji
from integrations.services import slack_dm_mirror as mirror
from integrations.services.message_sync.scheduler import BudgetDeferred


class SlackEmojiDirectoryView(SlackDmMirrorApiView):
    """Resolve requested workspace names without disclosing conversation usage.

    Like the community people directory, this is workspace metadata available
    to verified community accounts. Reads use a current consenting member and
    never a bot. No private message, participant list or membership is returned.
    """
    community_chat_throttle_scope = "community_chat_slack_directory"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def get(self, request):
        workspace = request.query_params.get("workspace_id", "")
        names = request.query_params.get("names", "").split(",")
        if (workspace != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
                or not workspace or len(names) > 100
                or any(not emoji.SLACK_CUSTOM_NAME_RE.fullmatch(name) for name in names)):
            return Response({"error": "invalid_emoji_request"}, status=400)
        key = getattr(request, "community_chat_public_key", None)
        def authorized():
            require_community_access(request.user)
            return CommunityChatDevice.objects.filter(
                user=request.user, public_key=key, status="verified", revoked_at__isnull=True,
                user__is_active=True,
            ).exists()
        if not authorized():
            return Response({"error": "device_unverified"}, status=403)
        try:
            catalog = emoji.load_catalog(workspace, requested_names=names)
            # A revoked requesting device may not receive a response that was
            # fetched while it was still current. Reader authority is separately
            # fenced inside load_catalog, including cache hits.
            if not authorized():
                return Response({"error": "device_unverified"}, status=403)
            standard = emoji._emoji_maps()[0]
            by_shortcode = {item.get("shortcode"): item for item in catalog.values()}
            result = {}
            for name in names:
                item = catalog.get(name) or by_shortcode.get(name)
                if item:
                    result[name] = item
                elif name in standard:
                    result[name] = {"unicode": standard[name]}
            return Response({"workspace_id": workspace, "emoji": result})
        except (BudgetDeferred, SlackClientError, DatabaseError,
                mirror.SlackDmMirrorCredentialError, mirror.SlackDmMirrorUpstreamError) as exc:
            return _slack_endpoint_error_response(exc)
        except mirror.SlackDmMirrorError:
            return Response({"error": "slack_emoji_unavailable"}, status=503)
