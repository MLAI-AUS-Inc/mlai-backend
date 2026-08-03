import hashlib
import logging
import secrets
import time

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.crypto import salted_hmac
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_cookies import clear_auth_cookies, clear_django_session_cookies
from .auth_serializers import (
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
)
from .auth_throttles import (
    client_ip,
    enforce_password_change_limits,
    enforce_password_reset_confirm_limits,
    enforce_password_reset_request_limits,
)
from .password_auth import (
    InvalidPasswordResetToken,
    change_password,
    consume_password_reset,
    issue_password_reset,
    normalize_account_email,
    password_validation_messages,
)


logger = logging.getLogger(__name__)
GENERIC_RESET_RESPONSE = {
    'status': 'accepted',
    'message': 'If an eligible MLAI account exists, password instructions have been sent.',
}


class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        started_at = time.monotonic()
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = normalize_account_email(serializer.validated_data['email'])
        enforce_password_reset_request_limits(request, email)
        ip_hash = salted_hmac(
            'password-reset-request-ip',
            client_ip(request),
            algorithm='sha256',
        ).hexdigest()
        issue_password_reset(email, requested_ip_hash=ip_hash)
        minimum = settings.PASSWORD_RESET_MIN_RESPONSE_SECONDS
        jitter = secrets.randbelow(21) / 1000
        remaining = minimum + jitter - (time.monotonic() - started_at)
        if remaining > 0:
            time.sleep(remaining)
        return Response(GENERIC_RESET_RESPONSE, status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['token']
        selector = hashlib.sha256(token.split('.', 1)[0].encode('utf-8')).hexdigest()
        enforce_password_reset_confirm_limits(request, selector)
        try:
            consume_password_reset(token, serializer.validated_data['new_password'])
        except InvalidPasswordResetToken:
            return Response(
                {'error': 'invalid_or_expired_token'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DjangoValidationError as exc:
            return Response(
                {
                    'error': 'password_validation_failed',
                    'details': password_validation_messages(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'password_set'}, status=status.HTTP_200_OK)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enforce_password_change_limits(request)
        try:
            changed = change_password(
                request.user,
                serializer.validated_data['current_password'],
                serializer.validated_data['new_password'],
            )
        except DjangoValidationError as exc:
            return Response(
                {
                    'error': 'password_validation_failed',
                    'details': password_validation_messages(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not changed:
            return Response(
                {'error': 'invalid_current_password'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response = Response(
            {'status': 'password_changed', 'reauthentication_required': True},
            status=status.HTTP_200_OK,
        )
        try:
            auth_logout(request._request)
        finally:
            clear_auth_cookies(response)
            clear_django_session_cookies(response)
        return response
