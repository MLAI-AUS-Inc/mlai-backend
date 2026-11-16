"""Chat-authenticated MCP setup, startup selection, consent and revocation."""
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.authentication import CommunityChatOnboardingAuthentication
from community_chat.onboarding import has_community_access
from founder_tools.models import VibeRaisingCompany
from startup_updates.mcp import draft_task, oauth
from startup_updates.mcp.config import DOT_SCOPES, availability, capability_catalog, client_catalog, mcp_url, mlai_url, public_base
from .views import ChatStartupAccess
from django.conf import settings

MEMBERSHIP_REASON = "Your MLAI community membership isn't approved yet, so an AI can't be connected. Finish joining MLAI, then start the connection again from your AI app."
NO_STARTUP_REASON = "Add your startup in MLAI before connecting an AI."
STARTUPS_BLOCKED_REASON = "None of your startups can be connected yet. See the note beside each one."
NOT_SET_UP_REASON = "This startup isn't fully set up yet. Finish setting it up in MLAI, then connect your AI."
TEAM_MEMBER_REASON = "Only the person who owns this startup in MLAI can connect an AI to it. Ask them to connect it."


def unavailable_companies(user, available_ids):
    """Startups the founder would expect to pick, each with why it cannot be chosen."""
    rows = [{"id": str(company.pk), "name": company.name, "reason": NOT_SET_UP_REASON}
        for company in VibeRaisingCompany.objects.filter(profile__user=user, organization__isnull=True).order_by("name")]
    if getattr(settings, "MY_STARTUP_TEAM_ENABLED", False):
        try:
            from founder_tools.startup_team import member_companies
            members = member_companies(user)
        except Exception:
            members = []  # An explanatory list must never stop the consent screen loading.
        taken = set(available_ids) | {row["id"] for row in rows}
        rows.extend({"id": str(item["id"]), "name": item.get("name", ""), "reason": TEAM_MEMBER_REASON}
            for item in members if str(item["id"]) not in taken)
    return rows


def consent_eligibility(member, intent, companies, unavailable):
    """Say before approval, in plain words, when this account cannot receive a token."""
    if not member:
        return {"ok": False, "reason": MEMBERSHIP_REASON, "code": "community_membership_required"}
    startup_only = intent["resource"] != mlai_url() or all(scope.startswith(("startup:", "article:")) for scope in intent["scopes"])
    if startup_only and not companies:
        return {"ok": False, "reason": STARTUPS_BLOCKED_REASON if unavailable else NO_STARTUP_REASON, "code": "no_startup"}
    return {"ok": True, "reason": None, "code": None}


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
        return Response({"enabled": available, "available": available, "reason": reason, "serviceName": draft_task.service_name(),
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
    # A member still awaiting approval reaches this screen to be told why the
    # connection cannot be made, instead of a load error. Approving stays refused.
    authentication_classes = (CommunityChatOnboardingAuthentication,)

    def get(self, request, request_id):
        available, reason = availability(mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url())
        if not available:
            raise NotFound(reason)
        intent = oauth.intent_for(request_id)
        companies = [{"id": str(company.pk), "name": company.name} for company in
            VibeRaisingCompany.objects.filter(profile__user=request.user, organization__isnull=False).order_by("name")]
        unavailable = unavailable_companies(request.user, {company["id"] for company in companies})
        member = has_community_access(request.user)
        channels = []
        community_reason = None
        native_installations = []
        native_reason = None
        if not member:
            community_reason = native_reason = MEMBERSHIP_REASON
        elif intent["resource"] == mlai_url() and set(intent["scopes"]) & DOT_SCOPES:
            from startup_updates.mcp.native_agents import consent_installations
            try:
                native_installations = consent_installations(request.user, request.auth)
                if not native_installations:
                    native_reason = "Enroll and verify a personal Dot in Apps & agents before connecting its chat permissions."
            except (PermissionDenied, ValidationError) as exc:
                native_reason = str(exc.detail)
        if member and set(intent["scopes"]) & {"community:read", "community:write"}:
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
            "companies": companies, "unavailableCompanies": unavailable,
            "eligibility": consent_eligibility(member, intent, companies, unavailable)})

    def post(self, request, request_id):
        available, reason = availability(mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url())
        if not available:
            raise NotFound(reason)
        if not hasattr(request.data, "get") or request.data.get("requestId") != request_id or not isinstance(request.data.get("approve"), bool):
            raise ValidationError("Confirm this agent connection request.")
        if request.data["approve"] and not has_community_access(request.user):
            # No token could be issued; refuse here so the AI app is not left
            # with an approval that never becomes a working connection.
            raise ValidationError(MEMBERSHIP_REASON)
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


class AgentDraftTaskView(ChatStartupAccess, APIView):
    """Write the request the founder gives their own AI for one month's update."""

    def post(self, request):
        from django.utils import timezone
        from startup_updates.models import MonthlyUpdateDraft, StartupProfile
        from startup_updates.mcp.tools import month_date
        data = request.data if hasattr(request.data, "get") else {}
        organization = self.company.organization
        if organization is None:
            raise ValidationError({"companyId": NOT_SET_UP_REASON})
        month = month_date(data.get("month"))
        profile = StartupProfile.objects.filter(organization=organization).first()
        reporting = getattr(profile, "reporting_timezone", "") or "UTC"
        update = None
        update_id = data.get("updateId")
        if update_id is not None:
            if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 1:
                raise ValidationError({"updateId": "Use a positive updateId."})
            update = MonthlyUpdateDraft.objects.filter(pk=update_id, organization=organization).first()
            if update is None:
                raise NotFound("This update is unavailable to this startup.")
            if update.month != month:
                raise ValidationError({"updateId": "This update belongs to another reporting month."})
        # The saved revision is the truth: a client's own expectedRevision may
        # be stale and would only make the AI's first save fail with a conflict.
        return Response(draft_task.build_task(company=self.company, month=data.get("month"),
            zone=draft_task.zone_for(data.get("timezone"), reporting), reporting_zone=draft_task.zone_for(reporting),
            now=timezone.now(), update=update, expected_revision=update.current_revision_id if update else None))
