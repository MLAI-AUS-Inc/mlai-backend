"""Single-use company-bound browser handoff into existing provider OAuth."""
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
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
    if parsed.path == "/my-startup/website":
        # P1.14: the new-journey website page (accepted only while the
        # GitHub callback redirects are on; see github_callback_reasons).
        from integrations.services.github_callback_reasons import website_settings_return
        return website_settings_return(value, company_id)
    if parsed.path != "/my-startup/connections/github" or query.get("company_id") != [str(company_id)]:
        return None
    target = {"company_id": str(company_id)}
    try:
        previous = urlsplit(query.get("return_to", query.get("returnTo", [""]))[0])
    except ValueError:
        previous = urlsplit("")
    previous_query = parse_qs(previous.query)
    step = previous_query.get("step", [""])[0]
    if (not previous.scheme and not previous.netloc and previous.path == "/my-startup/onboarding"
            and previous_query.get("company_id") == [str(company_id)] and step in {"repository", "articles"}):
        target["return_to"] = "/my-startup/onboarding?" + urlencode({"step": step, "company_id": str(company_id)})
    return "/my-startup/connections/github?" + urlencode(target)


def _new_journey_organization(company):
    """The company's existing organisation when it is in the new journey, else None."""
    if getattr(company, "organization_id", None) is None:
        return None
    from integrations.services.github_callback_reasons import is_new_journey_organization
    organization = company.organization
    return organization if is_new_journey_organization(organization) else None


def _new_journey_github_refusal(company):
    """IF-P1-08: a new-journey startup without a website cannot start a GitHub connection."""
    organization = _new_journey_organization(company)
    if organization is None:
        return None
    from content_factory.founder_journey_cohort import organization_has_website
    if organization_has_website(organization):
        return None
    from integrations.services.github_callback_reasons import NO_WEBSITE, NO_WEBSITE_DETAIL, log_return
    log_return(organization, NO_WEBSITE)
    return Response({"detail": NO_WEBSITE_DETAIL, "reason": NO_WEBSITE}, status=409)


def _peek_github_ticket(ticket):
    """A valid, unexpired GitHub ticket's owned company, read without consuming it."""
    try:
        payload = signing.loads(ticket, salt=SALT, max_age=300)
        if payload.get("provider") != "github":
            return None, None
        session = CommunityChatAccountSession.objects.filter(pk=payload["session"], user_id=payload["uid"]).first()
        if not _valid_session(session, timezone.now()):
            return None, None
        company = VibeRaisingCompany.objects.select_related("organization").get(
            pk=payload["company"], profile__user_id=payload["uid"])
    except (signing.BadSignature, KeyError, ValueError, TypeError, AttributeError, VibeRaisingCompany.DoesNotExist):
        return None, None
    return payload, company


def _bad_ticket_is_new_journey(ticket):
    """Whether a bad GitHub ticket goes to the fixed route rather than today's 400.

    Only a ticket whose signature still checks out (expired, replayed, or for
    a revoked Chat session) and whose company's organisation is new-journey.
    Its age is deliberately not checked: it is read only to find the cohort,
    and the fixed route carries nothing from it. A tampered or unsigned
    ticket, and every legacy one, keeps today's 400.
    """
    try:
        payload = signing.loads(ticket, salt=SALT)
        if not isinstance(payload, dict) or payload.get("provider") != "github":
            return False
        company = VibeRaisingCompany.objects.select_related("organization").filter(
            pk=payload["company"], profile__user_id=payload["uid"]).first()
    except (signing.BadSignature, KeyError, ValueError, TypeError, DjangoValidationError):
        return False
    return company is not None and _new_journey_organization(company) is not None


def _new_journey_return(payload, company):
    """The new-journey return: the website page on the web, today's native link on the desktop."""
    from integrations.services.github_callback_reasons import chat_return_url
    return chat_return_url(company_id=company.pk, return_to=payload.get("return_to"),
                           editor_return=github_settings_return(payload.get("github_settings_return"), company.pk))


def _new_journey_redirect(reason, payload, company, organization):
    from integrations.services.github_callback_reasons import redirect_with_reason
    return redirect_with_reason(reason, organization=organization, company_id=company.pk,
                                return_url=_new_journey_return(payload, company))


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
        if provider == "github":
            refusal = _new_journey_github_refusal(self.company)
            if refusal is not None:
                return refusal
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
    from integrations.services import github_callback_reasons as reasons
    if not enabled():
        if reasons.redirects_enabled():
            peeked, peeked_company = _peek_github_ticket(request.GET.get("ticket", ""))
            organization = _new_journey_organization(peeked_company) if peeked_company is not None else None
            if organization is not None:
                return _new_journey_redirect(reasons.UNAVAILABLE, peeked, peeked_company, organization)
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
        bad_ticket = request.GET.get("ticket", "")
        if (reasons.redirects_enabled() and reasons.is_github_chat_ticket(bad_ticket)
                and _bad_ticket_is_new_journey(bad_ticket)):
            # Nothing in a bad ticket is used: the fixed route only.
            fixed = reasons.state_expired_redirect()
            if fixed is not None:
                return fixed
        return HttpResponseBadRequest("This connection link expired or was already used. Start again in MLAI Chat.")
    new_journey_org = (_new_journey_organization(company)
                       if provider == "github" and reasons.redirects_enabled() else None)
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
    if new_journey_org is not None:
        return_url = _new_journey_return(payload, company)
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
        from content_factory.founder_journey_cohort import organization_has_website
        refuse_no_website = _new_journey_organization(company) is not None and not organization_has_website(company.organization)
        organization = None if refuse_no_website else ensure_company_organization(company)
        if organization is None:
            # IF-P1-08: a new-journey startup without a website mints no GitHub state.
            if refuse_no_website and new_journey_org is not None:
                return _new_journey_redirect(reasons.NO_WEBSITE, payload, company, new_journey_org)
            return HttpResponseBadRequest("Add your startup website in Startup details first.")
        if not (getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "") and getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "")):
            if new_journey_org is not None:
                return _new_journey_redirect(reasons.UNAVAILABLE, payload, company, new_journey_org)
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
