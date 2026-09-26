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
from integrations.views import connector_connect
from .views import ChatStartupAccess, enabled

SALT = "chat-startup-source-v1"
PROVIDERS = {"gmail", "notion", "slack", "linear", "google_analytics", "xero", "stripe"}


def consume_ticket(ticket):
    """Consume once before entering provider OAuth; reject tampering and replay."""
    payload = signing.loads(ticket, salt=SALT, max_age=300)
    key = "startup-connect-used:" + hashlib.sha256(ticket.encode()).hexdigest()
    if not cache.add(key, True, timeout=301):
        raise signing.BadSignature("Connection link already used.")
    return payload


class ConnectView(ChatStartupAccess, APIView):
    def post(self, request, provider):
        if provider not in PROVIDERS:
            raise ValidationError("This source does not support browser connection.")
        ticket = signing.dumps({"uid": request.user.pk, "company": str(self.company.pk),
            "session": str(request.auth.pk), "provider": provider, "nonce": secrets.token_urlsafe(24)}, salt=SALT)
        url = request.build_absolute_uri(reverse("chat_startups_connect_browser"))
        return Response({"authorizationUrl": f"{url}?{urlencode({'ticket': ticket})}"})


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
    query.update({"company_id": str(company.pk), "next": f"{frontend}/pulse?startup={company.pk}&connected=1"})
    if provider == "gmail":
        query["scope"] = "gmail"
    request.GET = query
    response = connector_connect(request, provider)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
