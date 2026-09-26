"""Keep Roo link capabilities in an API-host HttpOnly cookie during sign-in."""

import re
from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from core.views import SlackFounderLinkCompleteView, SlackFounderLinkPreviewView
from .api import MyStartupViewMixin

COOKIE = "mlai_startup_roo_link"
COOKIE_PATH = "/api/v1/my-startup/roo-link/"


class CaptureRooLinkView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        origin = str(request.headers.get("Origin") or "").rstrip("/")
        trusted = {
            str(value).rstrip("/") for value in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS
        }
        if not origin or origin not in trusted:
            return Response({"detail": "Invalid request origin."}, status=403)
        token = request.data.get("token")
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{40,128}", token
        ):
            return Response({"detail": "This Roo link is invalid."}, status=400)
        response = Response(
            {"status": "ready"},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=1800,
            httponly=True,
            secure=not settings.DEBUG,
            samesite="Lax",
            path=COOKIE_PATH,
        )
        return response


class PendingRooTokenMixin(MyStartupViewMixin):
    def post(self, request):
        # Request bodies cannot substitute an identity after the preview.
        request._full_data = {"token": request.COOKIES.get(COOKIE)}
        response = super().post(request)
        terminal = getattr(response, "data", {}).get("code") in {
            "expired_token",
            "invalid_token",
            "token_already_used",
            "link_conflict",
        }
        if terminal or (
            isinstance(self, CompleteRooLinkView) and response.status_code < 300
        ):
            response.delete_cookie(COOKIE, path=COOKIE_PATH, samesite="Lax")
        return response


class PreviewRooLinkView(PendingRooTokenMixin, SlackFounderLinkPreviewView):
    pass


class CompleteRooLinkView(PendingRooTokenMixin, SlackFounderLinkCompleteView):
    pass
