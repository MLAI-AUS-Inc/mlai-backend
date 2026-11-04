"""Chat-authenticated MCP setup, startup selection, consent and revocation."""
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from founder_tools.models import VibeRaisingCompany
from startup_updates.mcp import oauth
from startup_updates.mcp.config import DOT_SCOPES, availability, capability_catalog, client_catalog, mcp_url, mlai_url, public_base
from .views import ChatStartupAccess
from django.conf import settings


class AgentConnectionView(ChatStartupAccess, APIView):
    """Expose public install configuration; never embed account credentials."""
    requires_company = False

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from vibe_raising.views import _company_id_from_request
        company_id = _company_id_from_request(request)
        if company_id:
            self.company = oauth.company_for(request.user, company_id)

    def get(self, request):
        available, reason = availability(mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url())
        grants = oauth.grants_for(request.user, self.company.pk if self.company else None) if available else []
        return Response({"enabled": available, "available": available, "reason": reason,
            "mcpUrl": mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url(), "authorizationUrl": public_base() + "/mcp/oauth/authorize" if available else None,
            "connection": {"connected": bool(grants), "grants": grants}, "clients": client_catalog()})

    def delete(self, request):
        data = getattr(request, "data", {})
        grant_id = data.get("grantId") if hasattr(data, "get") else None
        if grant_id:
            grant = oauth.cache.get(oauth.key("grant", grant_id))
            if not grant or grant["user_id"] != request.user.pk or (self.company and str(grant.get("company_id")) != str(self.company.pk)):
                raise NotFound("This agent connection is unavailable.")
            oauth.revoke_grant(grant_id)
            grants = oauth.grants_for(request.user, self.company.pk if self.company else None)
            return Response({"disconnected": True, "connection": {"connected": bool(grants), "grants": grants}})
        oauth.disconnect_company(request.user, self.company.pk if self.company else None)
        return Response({"disconnected": True, "connection": {"connected": False, "grants": []}})


class AgentAuthorizationView(ChatStartupAccess, APIView):
    """Resume a validated OAuth intent after the existing Chat sign-in flow."""
    requires_company = False

    def get(self, request, request_id):
        available, reason = availability(mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url())
        if not available:
            raise NotFound(reason)
        intent = oauth.intent_for(request_id)
        companies = VibeRaisingCompany.objects.filter(profile__user=request.user, organization__isnull=False).order_by("name")
        channels = []
        community_reason = None
        native_installations = []
        native_reason = None
        if intent["resource"] == mlai_url() and set(intent["scopes"]) & DOT_SCOPES:
            from startup_updates.mcp.native_agents import consent_installations
            try:
                native_installations = consent_installations(request.user, request.auth)
                if not native_installations:
                    native_reason = "Enroll and verify a personal Dot in Apps & agents before connecting its chat permissions."
            except (PermissionDenied, ValidationError) as exc:
                native_reason = str(exc.detail)
        if set(intent["scopes"]) & {"community:read", "community:write"}:
            from startup_updates.mcp.community import consent_channels
            try:
                channels = [{"id": item["id"], "name": item["name"]} for item in consent_channels(request.user, request.auth)]
            except (PermissionDenied, ValidationError) as exc:
                # Other requested capabilities remain available when a member
                # has no verified native device, the separate gate is off, or
                # the native relay cannot currently return channel metadata.
                community_reason = str(exc.detail)
        return Response({"resource": intent["resource"], "capabilities": capability_catalog(intent["scopes"]),
            "nativeInstallations": native_installations, "nativeInstallationReason": native_reason,
            "communityChannels": channels, "communityReason": community_reason, "requestId": intent["requestId"], "clientName": intent["clientName"],
            "scopes": intent["scopes"], "redirectOrigin": intent["redirectOrigin"], "expiresAt": intent["expiresAt"],
            "companies": [{"id": str(company.pk), "name": company.name} for company in companies]})

    def post(self, request, request_id):
        available, reason = availability(mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url())
        if not available:
            raise NotFound(reason)
        if not hasattr(request.data, "get") or request.data.get("requestId") != request_id or not isinstance(request.data.get("approve"), bool):
            raise ValidationError("Confirm this agent connection request.")
        from vibe_raising.views import _company_id_from_request
        company_id = _company_id_from_request(request)
        try:
            url = oauth.approve_intent(request_id, user=request.user, session=request.auth,
                company_id=company_id, approve=request.data["approve"], scopes=request.data.get("scopes"),
                community_channel_ids=request.data.get("communityChannelIds"),
                native_installation_id=request.data.get("nativeInstallationId"))
        except oauth.OAuthError as exc:
            raise ValidationError(exc.description) from exc
        return Response({"redirectUrl": url})
