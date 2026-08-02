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
                return (user, validated_token)
            except (exceptions.AuthenticationFailed, TokenError):
                return None
        
        # No valid authentication found
        return None
