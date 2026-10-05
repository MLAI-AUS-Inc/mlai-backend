"""Single-use company-bound browser handoff into existing provider OAuth."""
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
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


def github_settings_return(value, company_id):
    """Keep GitHub consent in its editor, with an owned website setup return."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    if parsed.path != "/my-startup/connections/github" or query.get("company_id") != [str(company_id)]:
        return None
    target = {"company_id": str(company_id)}
    try:
        previous = urlsplit(query.get("returnTo", [""])[0])
    except ValueError:
        previous = urlsplit("")
    previous_query = parse_qs(previous.query)
    step = previous_query.get("step", [""])[0]
    if (not previous.scheme and not previous.netloc and previous.path == "/my-startup/onboarding"
            and previous_query.get("company_id") == [str(company_id)] and step in {"repository", "articles"}):
        target["returnTo"] = "/my-startup/onboarding?" + urlencode({"step": step, "company_id": str(company_id)})
    return "/my-startup/connections/github?" + urlencode(target)


class ConnectView(ChatStartupAccess, APIView):
    def post(self, request, provider):
        provider = PROVIDER_ALIASES.get(provider, provider)
        if provider in {"luma", "humanitix"}:
            response = (LumaConnectView if provider == "luma" else HumanitixConnectView)().post(request)
            if response.status_code == 200 and isinstance(response.data, dict):
                response.data["connected"] = any(
                    (row.get("provider") or row.get("key")) == provider and row.get("status") == "connected"
                    for row in response.data.get("sources", []) if isinstance(row, dict))
            return response
        if provider not in PROVIDERS:
            raise ValidationError("This source does not support browser connection.")
        ticket = signing.dumps({"uid": request.user.pk, "company": str(self.company.pk),
            "session": str(request.auth.pk), "provider": provider,
            "return_to": getattr(request, "data", {}).get("returnTo") if getattr(request, "data", {}).get("returnTo") in {"mobile", "desktop-dev"} else None,
            "github_settings_return": github_settings_return(getattr(request, "data", {}).get("returnUrl"), self.company.pk) if provider == "github" else None,
            "nonce": secrets.token_urlsafe(24)}, salt=SALT)
        url = request.build_absolute_uri(reverse("chat_startups_connect_browser"))
        return Response({"authorizationUrl": f"{url}?{urlencode({'ticket': ticket})}"})


class DisconnectView(ChatStartupAccess, APIView):
    """Manage defaults and disconnect only the chosen startup's own credentials."""
    def post(self, request, provider):
        provider = PROVIDER_ALIASES.get(provider, provider)
        return Response(set_source_preference(self.company, provider, request.data.get("enabled")))

    def delete(self, request, provider):
        provider = PROVIDER_ALIASES.get(provider, provider)
        if provider not in UPDATE_PROVIDERS:
            raise ValidationError("Choose a supported connection.")
        if self.company.organization is None:
            return Response({"status": "not_connected"})
        if provider == "gmail":
            return Response(disconnect_gmail_for_user(request.user, organization=self.company.organization, delete_derived_data=False))
        ids = list(ExternalServiceConnection.objects.filter(user=request.user,
            organization=self.company.organization, provider=provider).exclude(status="disconnected").values_list("pk", flat=True))
        for connection_id in ids:
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
    request.chat_oauth_context = {"chat_session_id": str(session.pk), "chat_company_id": str(company.pk), "user_id": user.pk}
    if getattr(company, "organization_id", None) is not None:
        request.chat_oauth_context["organization_id"] = company.organization_id
    query = QueryDict(mutable=True)
    frontend = settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/")
    return_url = f"{frontend}/my-startup/connections?" + urlencode({"company_id": str(company.pk), "connected": provider})
    editor_return = github_settings_return(payload.get("github_settings_return"), company.pk)
    if provider == "github" and editor_return:
        return_url = f"{frontend}{editor_return}"
    if payload.get("return_to") in {"mobile", "desktop-dev"}:
        scheme = "mlaichat-dev" if payload["return_to"] == "desktop-dev" else "mlaichat"
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
            chat_context={**request.chat_oauth_context, "organization_id": organization.pk})
        response = redirect(build_github_installation_url(state.raw))
    else:
        response = connector_connect(request, "google" if provider == "google_search_console" else provider)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
