"""Single-use company-bound browser handoff into existing provider OAuth."""
import hashlib
import secrets
from urllib.parse import urlencode
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponseBadRequest, QueryDict
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
PROVIDERS = OAUTH_PROVIDERS | {"google"}  # Website/Search Console consent is separate from Gmail input.


def consume_ticket(ticket):
    """Consume once before entering provider OAuth; reject tampering and replay."""
    payload = signing.loads(ticket, salt=SALT, max_age=300)
    key = "startup-connect-used:" + hashlib.sha256(ticket.encode()).hexdigest()
    if not cache.add(key, True, timeout=301):
        raise signing.BadSignature("Connection link already used.")
    return payload


class ConnectView(ChatStartupAccess, APIView):
    def post(self, request, provider):
        if provider in {"luma", "humanitix"}:
            view = LumaConnectView if provider == "luma" else HumanitixConnectView
            return view().post(request)
        if provider not in PROVIDERS:
            raise ValidationError("This source does not support browser connection.")
        ticket = signing.dumps({"uid": request.user.pk, "company": str(self.company.pk),
            "session": str(request.auth.pk), "provider": provider, "nonce": secrets.token_urlsafe(24)}, salt=SALT)
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
    query = QueryDict(mutable=True)
    frontend = settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/")
    return_query = urlencode({"company_id": str(company.pk), "connected": provider})
    query.update({"company_id": str(company.pk), "next": f"{frontend}/my-startup/connections?{return_query}"})
    if provider == "gmail":
        query["scope"] = "gmail"
    elif provider == "google":
        query["scope"] = "website_baseline"
    request.GET = query
    response = connector_connect(request, provider)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
