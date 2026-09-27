"""Single-use company-bound browser handoff into existing provider OAuth."""
import hashlib
import secrets
from urllib.parse import urlencode
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponseBadRequest, QueryDict
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from community_chat.account_sessions import _valid_session
from community_chat.models import CommunityChatAccountSession
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from founder_tools.models import VibeRaisingCompany
from integrations.api_views_connectors import HumanitixConnectView, LumaConnectView
from integrations.models import ExternalServiceConnection
from integrations.services.external_connectors import disconnect_external_connection
from integrations.views import connector_connect
from startup_updates.data_deletion import disconnect_gmail_for_user
from .lifecycle import OAUTH_PROVIDERS, UPDATE_PROVIDERS
from .source_preferences import set_source_preference
from .views import ChatStartupAccess, enabled

SALT = "chat-startup-source-v1"
PROVIDERS = OAUTH_PROVIDERS | {"google", "google_search_console", "github"}
PROVIDER_ALIASES = {"google-drive": "google_drive", "google-analytics": "google_analytics",
    "google-search-console": "google_search_console", "bank-feed": "bank_feed"}


def consume_ticket(ticket):
    """Consume once before entering provider OAuth; reject tampering and replay."""
    payload = signing.loads(ticket, salt=SALT, max_age=300)
    key = "startup-connect-used:" + hashlib.sha256(ticket.encode()).hexdigest()
    if not cache.add(key, True, timeout=301):
        raise signing.BadSignature("Connection link already used.")
    return payload


class ConnectView(ChatStartupAccess, APIView):
    def post(self, request, provider):
        provider = PROVIDER_ALIASES.get(provider, provider)
        if provider in {"luma", "humanitix"}:
            view = LumaConnectView if provider == "luma" else HumanitixConnectView
            response = view().post(request)
            if response.status_code == 200 and isinstance(response.data, dict):
                response.data["connected"] = any(
                    (source.get("provider") or source.get("key")) == provider
                    and source.get("status") in {"connected", "syncing"}
                    for source in response.data.get("sources", [])
                )
            return response
        if provider not in PROVIDERS:
            raise ValidationError("This source does not support browser connection.")
        ticket = signing.dumps({"uid": request.user.pk, "company": str(self.company.pk),
            "session": str(request.auth.pk), "provider": provider,
            "return_to": getattr(request, "data", {}).get("returnTo") if getattr(request, "data", {}).get("returnTo") in ("mobile", "desktop-dev") else None,
            "nonce": secrets.token_urlsafe(24)}, salt=SALT)
        url = request.build_absolute_uri(reverse("chat_startups_connect_browser"))
        return Response({"authorizationUrl": f"{url}?{urlencode({'ticket': ticket})}"})


class DisconnectView(ChatStartupAccess, APIView):
    """Disconnect only the explicitly selected startup's own provider account."""
    def post(self, request, provider):
        return Response(set_source_preference(self.company, provider, request.data.get("enabled")))

    def delete(self, request, provider):
        if provider not in UPDATE_PROVIDERS:
            raise ValidationError("Choose a supported connection.")
        if self.company.organization is None:
            return Response({"status": "not_connected"})
        if provider == "gmail":
            return Response(disconnect_gmail_for_user(request.user,
                organization=self.company.organization, delete_derived_data=False))
        connection_ids = list(ExternalServiceConnection.objects.filter(
            user=request.user, organization=self.company.organization, provider=provider,
        ).exclude(status="disconnected").values_list("pk", flat=True))
        for connection_id in connection_ids:
            disconnect_external_connection(request.user, connection_id)
        return Response({"status": "disconnected"})


def connect_browser(request):
    """Only signed server-selected company/provider data reaches the OAuth view."""
    if not enabled():
        return HttpResponseBadRequest("Startup connections are unavailable.")
    try:
        payload = consume_ticket(request.GET.get("ticket", ""))
        session = CommunityChatAccountSession.objects.select_related("user").filter(
            pk=payload["session"], user_id=payload["uid"],
        ).first()
        if not _valid_session(session, timezone.now()):
            raise ValueError("Chat session is no longer valid")
        user = session.user
        company = VibeRaisingCompany.objects.get(pk=payload["company"], profile__user=user)
        provider = payload["provider"]
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
    except (signing.BadSignature, KeyError, ValueError, get_user_model().DoesNotExist, VibeRaisingCompany.DoesNotExist):
        return HttpResponseBadRequest("This connection link expired or was already used. Start again in MLAI Chat.")
    request.user = user
    request.chat_oauth_context = {"chat_session_id": str(session.pk), "chat_company_id": str(company.pk)}
    query = QueryDict(mutable=True)
    frontend = settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/")
    return_query = urlencode({"company_id": str(company.pk), "connected": provider})
    return_url = f"{frontend}/my-startup/connections?{return_query}"
    if payload.get("return_to"):
        scheme = "mlaichat-dev" if payload.get("return_to") == "desktop-dev" else "mlaichat"
        return_url = f"{scheme}://connections?" + urlencode({"company_id": str(company.pk), "provider": provider})
    query.update({"company_id": str(company.pk), "next": return_url})
    if provider == "gmail":
        query["scope"] = "gmail"
    elif provider in {"google", "google_search_console"}:
        query["scope"] = "website_baseline"
    request.GET = query
    if provider == "github":
        from founder_tools.services import ensure_company_organization, founder_actor_id_for_user
        from integrations.services.github_connections import build_github_oauth_state, build_github_installation_url
        organization = ensure_company_organization(company)
        if organization is None:
            return HttpResponseBadRequest("Add your startup website in Startup details first.")
        if not (getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "") and getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "")):
            return HttpResponseBadRequest("GitHub is not available yet. Please try again later.")
        state = build_github_oauth_state(domain=organization.domain,
            slack_user_id=founder_actor_id_for_user(user), return_url=return_url,
            chat_context={**request.chat_oauth_context, "user_id": user.pk})
        response = redirect(build_github_installation_url(state.raw))
    else:
        response = connector_connect(request, "google" if provider == "google_search_console" else provider)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
