from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .assertions import ActorAssertionError, verify_and_resolve_actor_assertion
from .service_principals import (
    ServicePrincipalCredentialError,
    ServicePrincipalUser,
    authenticate_service_principal_token,
    record_service_principal_audit,
)


class ServicePrincipalAuthentication(BaseAuthentication):
    keyword = "ServicePrincipal"

    def authenticate(self, request):
        authorization = str(request.headers.get("Authorization", "") or "").strip()
        if not authorization:
            return None
        prefix = f"{self.keyword} "
        if not authorization.startswith(prefix):
            return None
        token = authorization[len(prefix):].strip()
        try:
            auth = authenticate_service_principal_token(token)
        except ServicePrincipalCredentialError as exc:
            raise AuthenticationFailed(str(exc)) from exc

        record_service_principal_audit(
            "authentication_succeeded",
            principal=auth.principal,
            credential=auth.credential,
            request_id=str(request.headers.get("X-Request-ID", "") or ""),
            remote_address=request.META.get("REMOTE_ADDR") or None,
        )
        return ServicePrincipalUser(auth.principal), auth

    def authenticate_header(self, request):
        return self.keyword


class OrgMemoryActorAuthentication(ServicePrincipalAuthentication):
    """Strict private-memory authentication; legacy API keys never reach it."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, auth = result
        try:
            request.org_memory_actor = verify_and_resolve_actor_assertion(request, auth)
        except ActorAssertionError as exc:
            raise AuthenticationFailed(str(exc)) from exc
        return user, auth


class RooGatewayActorAuthentication(ServicePrincipalAuthentication):
    """Authenticate the single Slack-facing Roo gateway without memory access."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, auth = result
        try:
            request.org_memory_actor = verify_and_resolve_actor_assertion(
                request,
                auth,
                required_surface="roo_gateway",
            )
        except ActorAssertionError as exc:
            raise AuthenticationFailed(str(exc)) from exc
        return user, auth
