"""Own-account cosmetic API using the existing Chat session boundary."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.throttles import CommunityChatScopedThrottle

from .policy import RooAvatarError
from .services import read_avatar, update_avatar


class RooAvatarView(APIView):
    """Read or replace the caller's outfit; never accept another account ID."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, RooAvatarError):
            body = {"error": exc.code, "outcome": exc.code}
            if exc.slots:
                body["slots"] = list(exc.slots)
            return Response(body, status=exc.status)
        return super().handle_exception(exc)

    def get(self, request):
        return Response(
            read_avatar(authenticated_session=request.community_chat_account_session)
        )

    def patch(self, request):
        return Response(
            update_avatar(
                authenticated_session=request.community_chat_account_session,
                values=request.data,
            )
        )
