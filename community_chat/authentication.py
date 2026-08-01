import hashlib
import hmac

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import CommunityChatBootstrapToken


TOKEN_PREFIX = "mlai_chat_"


class CommunityChatBootstrapAuthentication(BaseAuthentication):
    """Authenticate a temporary token that is valid only for this Django app."""

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        raw_token = header.removeprefix("Bearer ").strip()
        if not raw_token.startswith(TOKEN_PREFIX) or len(raw_token) < 48:
            return None

        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = (
            CommunityChatBootstrapToken.objects.select_related("user")
            .filter(token_hash=token_hash)
            .first()
        )
        if (
            token is None
            or token.revoked_at is not None
            or token.expires_at <= timezone.now()
            or not token.user.is_active
        ):
            raise AuthenticationFailed("Community chat authorization has expired.")
        if not hmac.compare_digest(token.token_hash, token_hash):
            raise AuthenticationFailed("Community chat authorization is invalid.")

        request.community_chat_public_key = token.public_key
        request.community_chat_bootstrap_token = token
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"
