"""Account-session-only transport for changing the canonical email."""

import re

from django.db import transaction
from django.utils.cache import add_never_cache_headers
from rest_framework import serializers
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import User
from .account_cookies import ACCESS_COOKIE, clear_account_session_cookies
from .authentication import CommunityChatAccountAuthentication
from .email_change import apply_action, available_for
from .views import _require_current_chat_credential_locked


class EmailChangeStartSerializer(serializers.Serializer):
    new_email = serializers.EmailField(max_length=254)

    def validate_new_email(self, value):
        return User.objects.normalize_email(value)


class EmailChangeRequestSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()


class EmailChangeCodeSerializer(EmailChangeRequestSerializer):
    code = serializers.RegexField(r"\A[0-9]{6}\Z", trim_whitespace=False)


class EmailChangeView(APIView):
    """Run one email-change action using only the current scoped account."""

    action = "status"
    authentication_classes = [CommunityChatAccountAuthentication]
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response

    def get(self, request):
        if self.action != "status":
            return self.http_method_not_allowed(request)
        return self._run(request, {}, "")

    def post(self, request):
        if self.action == "status":
            return self.http_method_not_allowed(request)
        key = request.headers.get("Idempotency-Key", "")
        if self.action != "cancel" and not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
            return Response({"error": "invalid_idempotency_key"}, status=400)
        if self.action == "start":
            serializer_class = EmailChangeStartSerializer
        elif self.action in {"verify-current", "confirm"}:
            serializer_class = EmailChangeCodeSerializer
        else:
            serializer_class = EmailChangeRequestSerializer
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(request, serializer.validated_data, key)

    @transaction.atomic
    def _run(self, request, values, key):
        cookie_authenticated = ACCESS_COOKIE in request.COOKIES and not request.headers.get("Authorization")
        if cookie_authenticated and not request.headers.get("X-MLAI-Public-Key"):
            raise NotAuthenticated("Sign in to MLAI Chat again to continue.")
        user = User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        if not available_for(user):
            return Response({"error": "email_change_unavailable"}, status=404)
        _require_current_chat_credential_locked(request, user, session_expired=NotAuthenticated)
        status, payload = apply_action(request, user, request.community_chat_account_session, self.action, values, key)
        response = Response(payload, status=status)
        if cookie_authenticated and payload.get("status") == "reauthentication_required":
            clear_account_session_cookies(response)
        return response
