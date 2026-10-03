"""Chat-authenticated MCP setup, startup selection, consent and revocation."""
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from founder_tools.models import VibeRaisingCompany
from startup_updates.mcp import oauth
from startup_updates.mcp.config import availability, client_catalog, mcp_url, public_base
from .views import ChatStartupAccess


class AgentConnectionView(ChatStartupAccess, APIView):
    """Expose public install configuration; never embed account credentials."""
    def get(self, request):
        available, reason = availability()
        grants = oauth.grants_for(request.user, self.company.pk) if available else []
        return Response({"enabled": available, "available": available, "reason": reason,
            "mcpUrl": mcp_url(), "authorizationUrl": public_base() + "/mcp/oauth/authorize" if available else None,
            "connection": {"connected": bool(grants), "grants": grants}, "clients": client_catalog()})

    def delete(self, request):
        oauth.disconnect_company(request.user, self.company.pk)
        return Response({"disconnected": True, "connection": {"connected": False, "grants": []}})


class AgentAuthorizationView(ChatStartupAccess, APIView):
    """Resume a validated OAuth intent after the existing Chat sign-in flow."""
    requires_company = False

    def get(self, request, request_id):
        available, reason = availability()
        if not available:
            raise NotFound(reason)
        intent = oauth.intent_for(request_id)
        companies = VibeRaisingCompany.objects.filter(profile__user=request.user, organization__isnull=False).order_by("name")
        return Response({"requestId": intent["requestId"], "clientName": intent["clientName"],
            "scopes": intent["scopes"], "redirectOrigin": intent["redirectOrigin"], "expiresAt": intent["expiresAt"],
            "companies": [{"id": str(company.pk), "name": company.name} for company in companies]})

    def post(self, request, request_id):
        available, reason = availability()
        if not available:
            raise NotFound(reason)
        if not hasattr(request.data, "get") or request.data.get("requestId") != request_id or not isinstance(request.data.get("approve"), bool):
            raise ValidationError("Confirm this agent connection request.")
        from vibe_raising.views import _company_id_from_request
        company_id = _company_id_from_request(request)
        if request.data["approve"] and not company_id:
            raise ValidationError({"companyId": "Choose a startup."})
        try:
            url = oauth.approve_intent(request_id, user=request.user, session=request.auth,
                company_id=company_id, approve=request.data["approve"])
        except oauth.OAuthError as exc:
            raise ValidationError(exc.description) from exc
        return Response({"redirectUrl": url})
