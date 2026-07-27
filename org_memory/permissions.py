from rest_framework.permissions import BasePermission

from .authorization import (
    OrganizationAuthorizationError,
    resolve_actor_authorization,
)
from .service_principals import ServicePrincipalAuthContext
from .pilot_deployment import actor_has_active_pilot_access


class HasOrgMemoryServiceScope(BasePermission):
    message = "The service principal does not have the required organisational-memory scope."

    def has_permission(self, request, view):
        auth = getattr(request, "auth", None)
        actor = getattr(request, "org_memory_actor", None)
        if not isinstance(auth, ServicePrincipalAuthContext) or actor is None:
            return False
        if actor.organization.pk != auth.principal.organization_id:
            return False
        required_scope = str(getattr(view, "required_service_scope", "org_memory.read"))
        return auth.principal.has_scope(required_scope)


class HasOrgMemoryCapability(BasePermission):
    message = "The acting user does not have the required organisational-memory capability."

    def has_permission(self, request, view):
        actor = getattr(request, "org_memory_actor", None)
        if actor is None:
            return False
        try:
            authorization = resolve_actor_authorization(actor)
        except OrganizationAuthorizationError:
            return False
        request.org_memory_authorization = authorization
        required = str(
            getattr(view, "required_actor_capability", "view_general_memory")
        )
        return authorization.has_capability(required)


class HasActiveOrgMemoryPilotAccess(BasePermission):
    message = "The acting user is not authorised for the active Admin Roo pilot."

    def has_permission(self, request, view):
        return actor_has_active_pilot_access(
            getattr(request, "org_memory_actor", None)
        )
