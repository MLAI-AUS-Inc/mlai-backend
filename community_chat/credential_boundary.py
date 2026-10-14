"""Shared locked credential/admission policy, manually gated for staging."""
import hashlib,secrets
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from .account_cookies import access_cookie_name
from .models import CommunityChatAccountSession,CommunityChatBootstrapToken

def _is_eligible(user):
    from .onboarding import has_community_access
    return bool(user and user.is_authenticated and has_community_access(user))


def _require_eligible(user):
    if not _is_eligible(user):
        raise PermissionDenied("This MLAI account is not eligible for community chat.")


def _require_current_chat_credential_locked(request, locked_user):
    """Revalidate a Chat credential after taking the shared user row lock."""

    now = timezone.now()
    _require_eligible(locked_user)
    account_session = getattr(request, "community_chat_account_session", None)
    if account_session is not None:
        authorization = str(request.headers.get("Authorization") or "")
        if authorization.startswith("Bearer "):
            raw_access_token = authorization.removeprefix("Bearer ").strip()
        else:
            raw_access_token = str(request.COOKIES.get(access_cookie_name()) or "")
        presented_hash = hashlib.sha256(raw_access_token.encode("utf-8")).hexdigest()
        current = (
            CommunityChatAccountSession.objects.select_for_update(of=("self",))
            .filter(
                pk=account_session.pk,
                user_id=locked_user.pk,
                revoked_at__isnull=True,
                access_expires_at__gt=now,
                expires_at__gt=now,
                auth_version=locked_user.auth_version,
            )
            .first()
        )
        if current is None or not secrets.compare_digest(
            current.access_token_hash,
            presented_hash,
        ):
            raise PermissionDenied("MLAI Chat session has expired.")
        request.community_chat_account_session = current

    bootstrap_token = getattr(request, "community_chat_bootstrap_token", None)
    if bootstrap_token is not None:
        authorization = str(request.headers.get("Authorization") or "")
        raw_bootstrap_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else ""
        )
        presented_hash = hashlib.sha256(
            raw_bootstrap_token.encode("utf-8")
        ).hexdigest()
        current = (
            CommunityChatBootstrapToken.objects.select_for_update()
            .filter(
                pk=bootstrap_token.pk,
                user_id=locked_user.pk,
                revoked_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if current is None or not secrets.compare_digest(
            current.token_hash,
            presented_hash,
        ):
            raise PermissionDenied("Community chat authorization has expired.")
        request.community_chat_bootstrap_token = current


