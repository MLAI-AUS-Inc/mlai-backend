import secrets

from django.conf import settings
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError

from core.refresh_sessions import ensure_token_auth_version


class CustomJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        try:
            ensure_token_auth_version(validated_token, user=user)
        except TokenError as exc:
            raise exceptions.AuthenticationFailed(
                'Account session is no longer valid.'
            ) from exc
        return user

    def authenticate(self, request):
        # First try to get token from Authorization header
        header = self.get_header(request)
        if header:
            return super().authenticate(request)

        # Fall back to cookie authentication
        access_token = request.COOKIES.get('access_token')
        if access_token:
            try:
                validated_token = self.get_validated_token(access_token)
                user = self.get_user(validated_token)
                self._enforce_cookie_csrf_origin(request)
                return (user, validated_token)
            except (exceptions.AuthenticationFailed, TokenError):
                return None

        # No valid authentication found
        return None

    @staticmethod
    def _enforce_cookie_csrf_origin(request):
        """Require an exact trusted Origin for unsafe cookie-authenticated calls.

        Browser cookies are ambient credentials, unlike an explicit Bearer
        header. Requiring the browser-controlled Origin header blocks cross-site
        mutations without imposing CSRF tokens on native/API Bearer clients.
        """

        if request.method in {'GET', 'HEAD', 'OPTIONS', 'TRACE'}:
            return
        origin = str(request.headers.get('Origin') or '').strip().rstrip('/')
        trusted = {
            str(item).strip().rstrip('/')
            for item in settings.CSRF_TRUSTED_ORIGINS
            if str(item).strip()
        }
        if not origin or not any(
            secrets.compare_digest(origin, candidate) for candidate in trusted
        ):
            raise exceptions.PermissionDenied(
                'Cookie-authenticated mutations require an approved Origin.'
            )
