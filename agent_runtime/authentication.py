"""Distinct owner, delegated-agent, and worker authentication boundaries."""

from dataclasses import dataclass
import hashlib
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, NotFound, PermissionDenied, ValidationError


def require_enabled():
    if not getattr(settings, "AGENT_RUNTIME_ENABLED", False):
        raise NotFound("Agent runtime is not available.")


def bearer_value(request):
    header = str(request.headers.get("Authorization", ""))
    if not header.startswith("Bearer "):
        raise AuthenticationFailed("A Bearer credential is required.")
    value = header[7:]
    if not value or len(value) > 512 or any(c.isspace() for c in value):
        raise AuthenticationFailed("Invalid Bearer credential.")
    return value


def token_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentPrincipal:
    grant: object
    credential: object
    task: object = None

    @property
    def owner(self):
        return self.grant.installation.owner


def validate_credential(credential, *, audience, allowed_kinds, now=None):
    """Pure validation reused at admission; every operation also checks policy."""
    now = now or timezone.now()
    if credential is None:
        raise AuthenticationFailed("Agent credential is invalid.")
    grant = credential.grant
    if (credential.kind not in allowed_kinds or credential.audience != audience
            or credential.revoked_at is not None or credential.expires_at <= now
            or credential.generation != grant.generation or grant.revoked_at is not None
            or grant.expires_at <= now or not grant.installation.is_active
            or not grant.installation.owner.is_active
            or not set(credential.scopes).issubset(set(grant.scopes))):
        raise AuthenticationFailed("Agent authority has expired or changed.")
    task = credential.task if credential.task_id else None
    if credential.kind.startswith("task_"):
        if (task is None or task.grant_id != grant.pk or task.status != "running"
                or task.generation != grant.generation
                or task.lease_epoch != credential.lease_epoch
                or task.lease_expires_at is None or task.lease_expires_at <= now
                or task.deadline_at is None or task.deadline_at <= now):
            raise AuthenticationFailed("Task lease is no longer current.")
    elif task is not None:
        raise AuthenticationFailed("Invalid credential binding.")
    return AgentPrincipal(grant, credential, task)


def authenticate_bearer(request, *, audience="agent-tools", allowed_kinds=("access", "task_tool")):
    require_enabled()
    from .models import AgentCredential
    raw = bearer_value(request)
    if not raw.startswith("mlai_agent_"):
        raise AuthenticationFailed("An agent credential is required.")
    credential = AgentCredential.objects.select_related(
        "grant__installation__owner", "task", "oauth_binding"
    ).filter(token_hash=token_hash(raw)).first()
    expected_audience = (getattr(settings, "AGENT_RUNTIME_RESOURCE", "")
                         if audience == "agent-tools" else audience)
    principal = validate_credential(credential, audience=expected_audience, allowed_kinds=allowed_kinds)
    if credential.kind == "access":
        from .oauth import configured_client, permitted_scopes
        binding = getattr(credential, "oauth_binding", None)
        if binding is None:
            raise AuthenticationFailed("Agent client binding is missing.")
        try:
            client = configured_client(binding.client_id)
        except ValidationError as exc:
            raise AuthenticationFailed("Agent client is no longer registered.") from exc
        if not set(credential.scopes).issubset(set(client.get("scopes", [])) & permitted_scopes()):
            raise AuthenticationFailed("Agent client permissions have changed.")
    request.agent_principal = principal
    return principal


def require_task_principal(request, *, audience="agent-model"):
    principal = authenticate_bearer(request, audience=audience, allowed_kinds=("task_model",))
    from .policy import authorize, AgentPermissionDenied
    from .sources import live_access
    if principal.task.resource:
        try:
            authorize(principal.grant, "messages:read", principal.task.resource,
                      expected_generation=principal.credential.generation, access_checker=live_access)
        except AgentPermissionDenied as exc:
            raise PermissionDenied(exc.code) from exc
    return principal


class AgentAuthentication(BaseAuthentication):
    def authenticate(self, request):
        principal = authenticate_bearer(request)
        return principal.owner, principal

    def authenticate_header(self, request):
        return "Bearer"


class OwnerAuthentication(BaseAuthentication):
    """Existing human sessions only; new UI has a configured same-origin fence."""
    def authenticate(self, request):
        from urllib.parse import urlsplit
        from community_chat.authentication import CommunityChatAccountAuthentication
        from community_chat.account_cookies import ACCESS_COOKIE
        from community_chat.account_sessions import authenticate_access_token, InvalidAccountSession
        from core.authentication import CustomJWTAuthentication
        from rest_framework_simplejwt.exceptions import TokenError
        if request.headers.get("Authorization"):
            return (CommunityChatAccountAuthentication().authenticate(request)
                    or CustomJWTAuthentication().authenticate(request))
        platform_cookie = request.COOKIES.get("access_token")
        if platform_cookie:
            verifier = CustomJWTAuthentication()
            try:
                token = verifier.get_validated_token(platform_cookie)
                user = verifier.get_user(token)
            except (AuthenticationFailed, TokenError):
                pass
            else:
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    issuer = urlsplit(getattr(settings, "AGENT_RUNTIME_ISSUER", ""))
                    issuer_origin = f"{issuer.scheme}://{issuer.netloc}"
                    origin = str(request.headers.get("Origin", ""))
                    if not origin or origin not in {issuer_origin, *getattr(settings, "CSRF_TRUSTED_ORIGINS", [])}:
                        raise AuthenticationFailed("Owner session origin is invalid.")
                return user, token
        raw = request.COOKIES.get(ACCESS_COOKIE)
        if raw:
            try:
                session = authenticate_access_token(raw)
            except InvalidAccountSession as exc:
                raise AuthenticationFailed("Sign in to MLAI again.") from exc
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                issuer = urlsplit(getattr(settings, "AGENT_RUNTIME_ISSUER", ""))
                issuer_origin = f"{issuer.scheme}://{issuer.netloc}"
                origin = str(request.headers.get("Origin", ""))
                trusted = set(getattr(settings, "COMMUNITY_CHAT_ALLOWED_ORIGINS", []))
                if not origin or (origin != issuer_origin and not (origin in trusted and origin == session.origin)):
                    raise AuthenticationFailed("Owner session origin is invalid.")
            request.community_chat_account_session = session
            return session.user, session
        return CustomJWTAuthentication().authenticate(request)

    def authenticate_header(self, request):
        return "Bearer"


def require_service_key(request, *, setting="AGENT_RUNTIME_WORKER_KEY"):
    require_enabled()
    expected = str(getattr(settings, setting, "") or "")
    # Misconfiguration fails closed, including an empty configured secret.
    if len(expected) < 32 or not secrets.compare_digest(bearer_value(request), expected):
        raise AuthenticationFailed("Service credential is invalid.")


def require_owner(request):
    require_enabled()
    if not request.user or not request.user.is_authenticated or not request.user.is_active:
        raise PermissionDenied("Sign in to your MLAI account.")
    return request.user
