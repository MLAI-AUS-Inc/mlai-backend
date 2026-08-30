"""One-click Slack DM migration controls for Community Home."""

from urllib.parse import urlencode

from django.conf import settings
from django.db import DatabaseError
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from slack_sdk.errors import SlackApiError, SlackClientError

from hospital.authentication import CustomJWTAuthentication
from integrations.models import SlackDmMirrorGrant
from integrations.services.slack_dm_mirror import (
    REQUIRED_SCOPES,
    SlackDmMirrorCredentialError,
    SlackDmMirrorError,
    SlackDmMirrorUpstreamError,
    activate_connection,
    active_grant_for_user,
    backfill_grant,
    open_slack_dm,
    pause_grant,
    resume_grant,
    revoke_user_grant,
    search_slack_users,
    slack_connection_for_user,
    status_payload,
)
from integrations.views import mint_connector_connect_ticket

from .authentication import (
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
)
from .throttles import CommunityChatScopedThrottle


SLACK_AUTH_ERROR_CODES = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "missing_scope",
        "no_permission",
        "not_allowed_token_type",
        "not_authed",
        "org_login_required",
        "token_expired",
        "token_revoked",
    }
)


def _slack_api_error_code(exc: SlackApiError) -> str:
    response = getattr(exc, "response", None)
    try:
        return str(response.get("error") or "").strip().lower()
    except (AttributeError, TypeError):
        return ""


def _slack_api_status_code(exc: SlackApiError) -> int:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _slack_retry_after_seconds(exc: SlackApiError) -> int:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        raw_value = headers.get("Retry-After") or headers.get("retry-after") or 1
        return max(1, int(raw_value))
    except (AttributeError, TypeError, ValueError):
        return 1


def _slack_endpoint_error_response(exc: Exception) -> Response:
    """Translate infrastructure failures without exposing provider details."""

    if isinstance(exc, DatabaseError):
        return Response(
            {"error": "slack_storage_unavailable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, SlackDmMirrorCredentialError):
        return Response(
            {"error": "slack_reauthorization_required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if isinstance(exc, SlackDmMirrorUpstreamError):
        return Response(
            {"error": "slack_upstream_unavailable"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if isinstance(exc, SlackApiError):
        error_code = _slack_api_error_code(exc)
        if error_code in SLACK_AUTH_ERROR_CODES:
            return Response(
                {"error": "slack_reauthorization_required"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if error_code == "ratelimited" or _slack_api_status_code(exc) == 429:
            retry_after = _slack_retry_after_seconds(exc)
            response = Response(
                {
                    "error": "slack_rate_limited",
                    "retry_after_seconds": retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response["Retry-After"] = str(retry_after)
            return response
    if isinstance(exc, SlackClientError):
        return Response(
            {"error": "slack_upstream_unavailable"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    raise exc


class SlackDmMirrorApiView(APIView):
    authentication_classes = (
        CommunityChatAccountAuthentication,
        CommunityChatBootstrapAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_home"


class SlackDmMirrorView(SlackDmMirrorApiView):
    """Inspect, connect, pause, resume, or disconnect Slack DM mirroring."""

    def get(self, request):
        return Response(
            status_payload(
                request.user,
                authenticated_public_key=getattr(
                    request, "community_chat_public_key", None
                ),
            )
        )

    def post(self, request):
        connection = slack_connection_for_user(request.user)
        if connection is not None and REQUIRED_SCOPES.issubset(
            set(connection.scopes or [])
        ):
            try:
                activate_connection(connection)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
            return Response(
                status_payload(
                    request.user,
                    authenticated_public_key=getattr(
                        request, "community_chat_public_key", None
                    ),
                ),
                status=status.HTTP_200_OK,
            )

        connect_url = self._authorization_url(request)
        payload = status_payload(
            request.user,
            authenticated_public_key=getattr(
                request, "community_chat_public_key", None
            ),
        )
        payload["authorization_url"] = connect_url
        payload["consent"] = {
            "version": SlackDmMirrorGrant.CONSENT_VERSION,
            "summary": (
                "Mirror all direct and group Slack DMs visible to your Slack account into "
                "private, owner-controlled conversations in MLAI Chat. The other person "
                "or group members do not need to link Slack and cannot see your imported "
                "copy unless they link independently. Up to 30 days of history is imported; DMs are "
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
        grants = SlackDmMirrorGrant.objects.filter(user=request.user)
        grant = (
            grants.filter(status="active", revoked_at__isnull=True)
            .order_by("-updated_at")
            .first()
            or grants.order_by("-updated_at").first()
        )
        if grant is None:
            return Response(
                {"error": "slack_not_linked"}, status=status.HTTP_404_NOT_FOUND
            )
        action = str(request.data.get("action") or "").strip().lower()
        if action == "pause":
            pause_grant(grant)
        elif action == "resume":
            try:
                resume_grant(grant)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
        elif action == "backfill":
            try:
                backfill_grant(grant)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
        elif action == "backfill_all":
            try:
                backfill_grant(grant, full_history=True)
            except SlackDmMirrorError as exc:
                raise ValidationError({"slack": str(exc)}) from exc
        else:
            raise ValidationError(
                {"action": "Use pause, resume, backfill, or backfill_all."}
            )
        return Response(
            status_payload(
                request.user,
                authenticated_public_key=getattr(
                    request, "community_chat_public_key", None
                ),
            )
        )

    def delete(self, request):
        revoke_user_grant(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SlackUserDirectoryView(SlackDmMirrorApiView):
    """Search internal human users visible to the owner's Slack token."""

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 20))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"limit": "Use a number between 1 and 50."}) from exc
        try:
            grant = active_grant_for_user(request.user)
            payload = search_slack_users(
                grant,
                query=request.query_params.get("q", ""),
                limit=limit,
                cursor=request.query_params.get("cursor", ""),
            )
        except (SlackDmMirrorCredentialError, SlackDmMirrorUpstreamError) as exc:
            return _slack_endpoint_error_response(exc)
        except SlackDmMirrorError as exc:
            raise ValidationError({"slack": str(exc)}) from exc
        except (SlackClientError, DatabaseError) as exc:
            return _slack_endpoint_error_response(exc)
        return Response(payload)


class SlackDmStartView(SlackDmMirrorApiView):
    """Start a Slack IM/MPIM and return its immediately usable MLAI mirror."""

    def post(self, request):
        public_key = str(
            getattr(request, "community_chat_public_key", "") or ""
        ).strip()
        if not public_key:
            raise ValidationError(
                {
                    "device": (
                        "Use an MLAI Chat account or bootstrap session with a "
                        "verified device key."
                    )
                }
            )
        slack_user_ids = request.data.get("slack_user_ids")
        if slack_user_ids is None and request.data.get("slack_user_id"):
            slack_user_ids = [request.data.get("slack_user_id")]
        if not isinstance(slack_user_ids, list):
            raise ValidationError(
                {"slack_user_ids": "Provide a list containing one to eight users."}
            )
        try:
            grant = active_grant_for_user(request.user)
            payload = open_slack_dm(
                grant,
                slack_user_ids=slack_user_ids,
                authenticated_public_key=public_key,
            )
        except (SlackDmMirrorCredentialError, SlackDmMirrorUpstreamError) as exc:
            return _slack_endpoint_error_response(exc)
        except SlackDmMirrorError as exc:
            raise ValidationError(
                {
                    "slack": str(exc),
                    "code": getattr(exc, "code", "slack_dm_mirror_error"),
                }
            ) from exc
        except (SlackClientError, DatabaseError) as exc:
            return _slack_endpoint_error_response(exc)
        return Response(payload, status=status.HTTP_200_OK)
