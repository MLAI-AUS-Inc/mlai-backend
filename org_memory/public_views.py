from __future__ import annotations

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .authentication import ServicePrincipalAuthentication
from .public_knowledge import PublicKnowledgeError, answer_public_knowledge_query
from .service_principals import ServicePrincipalAuthContext


class PublicKnowledgeRateThrottle(SimpleRateThrottle):
    scope = "public_knowledge"

    def get_cache_key(self, request, view):
        auth = getattr(request, "auth", None)
        if isinstance(auth, ServicePrincipalAuthContext):
            ident = f"principal:{auth.principal.pk}"
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PublicKnowledgeAnswerView(APIView):
    """Public-only answer surface; deliberately has no private retrieval fallback."""

    authentication_classes = (ServicePrincipalAuthentication,)
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (PublicKnowledgeRateThrottle,)

    def post(self, request):
        if not getattr(settings, "ORG_MEMORY_PUBLICATION_ENABLED", False):
            return Response(
                {"detail": "The public knowledge API is not enabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        data = request.data if isinstance(request.data, dict) else {}
        query = str(data.get("query") or "").strip()
        requested_domain = str(data.get("organization_domain") or "").strip().casefold()
        auth = getattr(request, "auth", None)
        organization_id = None
        if isinstance(auth, ServicePrincipalAuthContext):
            principal = auth.principal
            if (
                not principal.allows_surface("public_roo")
                or not principal.has_scope("public_knowledge.read")
            ):
                return Response(
                    {"detail": "This service principal cannot access public knowledge."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            organization_id = principal.organization_id
            if (
                requested_domain
                and requested_domain != principal.organization.domain.casefold()
            ):
                return Response(
                    {"detail": "organization_domain does not match the service principal."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            organization_domain = None
        else:
            if not requested_domain or len(requested_domain) > 255:
                return Response(
                    {"detail": "organization_domain is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            organization_domain = requested_domain
        try:
            payload = answer_public_knowledge_query(
                query=query,
                organization_id=organization_id,
                organization_domain=organization_domain,
                limit=getattr(settings, "ORG_MEMORY_PUBLIC_RESULT_LIMIT", 5),
            )
        except (PublicKnowledgeError, TypeError, ValueError) as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(payload)
