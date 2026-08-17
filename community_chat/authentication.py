import hashlib
import hmac

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import CommunityChatBootstrapToken, TokenUsageAccount
from .account_cookies import ACCESS_COOKIE
from .account_sessions import (
    ACCESS_TOKEN_PREFIX,
    InvalidAccountSession,
    authenticate_access_token,
)


TOKEN_PREFIX = "mlai_chat_"
USAGE_TOKEN_PREFIX = "mlai_usage_"


class TokenUsageAuthentication(BaseAuthentication):
    """Authenticate the reporter token a member mints for the leaderboard.

    Deliberately narrow: this credential can only write that member's own
    usage rows. It carries no account access, so a leaked token is a
    leaderboard problem and never an MLAI account problem.
    """

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        raw_token = header.removeprefix("Bearer ").strip()
        # Returning None on a prefix miss lets DRF fall through to the other
        # authenticators instead of rejecting an account session outright.
        if not raw_token.startswith(USAGE_TOKEN_PREFIX):
            return None

        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        account = (
            TokenUsageAccount.objects.select_related("user")
            .filter(token_hash=token_hash)
            .first()
        )
        if account is None or not account.user.is_active:
            raise AuthenticationFailed("Token usage credential is invalid.")
        if not hmac.compare_digest(account.token_hash, token_hash):
            raise AuthenticationFailed("Token usage credential is invalid.")

        request.token_usage_account = account
        return account.user, account

    def authenticate_header(self, request):
        return "Bearer"


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
        request.community_chat_installation_id = token.installation_id
        request.community_chat_client_id = token.client_id
        request.community_chat_origin = token.origin
        request.community_chat_platform = token.platform
        request.community_chat_device_name = token.name
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"


class CommunityChatAccountAuthentication(BaseAuthentication):
    """Authenticate the narrowly scoped access token for an MLAI Chat session."""

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        raw_token = ""
        cookie_authenticated = False
        if header.startswith("Bearer "):
            raw_token = header.removeprefix("Bearer ").strip()
            if not raw_token.startswith(ACCESS_TOKEN_PREFIX):
                return None
        elif request.COOKIES.get(ACCESS_COOKIE):
            raw_token = request.COOKIES[ACCESS_COOKIE]
            cookie_authenticated = True
        else:
            return None
        try:
            session = authenticate_access_token(raw_token)
        except InvalidAccountSession as exc:
            raise AuthenticationFailed("MLAI Chat session has expired.") from exc
        if cookie_authenticated and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
            "TRACE",
        }:
            origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
            trusted = {
                str(item).strip().rstrip("/")
                for item in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS
            }
            if not origin or origin not in trusted or not hmac.compare_digest(
                origin,
                session.origin,
            ):
                raise AuthenticationFailed("MLAI Chat session origin is invalid.")
        request.community_chat_account_session = session
        request.community_chat_public_key = session.public_key
        request.community_chat_installation_id = session.installation_id
        request.community_chat_client_id = session.client_id
        request.community_chat_origin = session.origin
        request.community_chat_platform = session.platform
        request.community_chat_device_name = session.name
        return session.user, session

    def authenticate_header(self, request):
        return "Bearer"
