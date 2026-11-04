"""Narrow browser-only admission for the separate founder acceptance runtime.

Normal handlers retain authentication, membership, ownership, throttling and
locking. This middleware never authenticates a user or grants membership.
"""
import json
import re
from uuid import UUID

from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin
from rest_framework.exceptions import AuthenticationFailed

from community_chat.founder_staging_email import CLIENT, FRONTEND, PROFILE, fixture_scope

KEY = re.compile(r"[0-9a-f]{64}")
# Class AND module are checked: a name collision on another route cannot open it.
CHAT = "community_chat.views"
ALLOWED = {
    (CHAT, "EmailCodeRequestView"): {"POST"},
    (CHAT, "EmailCodeVerifyView"): {"POST"},
    (CHAT, "AccountView"): {"GET", "PATCH"},
    (CHAT, "AccountSessionRefreshView"): {"POST"},
    (CHAT, "AccountSessionLogoutView"): {"POST"},
    (CHAT, "SessionView"): {"GET"},
    (CHAT, "ChallengeView"): {"POST"},
    (CHAT, "InviteView"): {"POST"},
    (CHAT, "ConfirmView"): {"POST"},
    ("community_chat.onboarding_views", "MemberOnboardingView"): {"GET", "PUT"},
}
BOOTSTRAP = {(CHAT, name) for name in ("SessionView", "ChallengeView", "InviteView", "ConfirmView")}
ANONYMOUS = {(CHAT, name) for name in ("EmailCodeRequestView", "EmailCodeVerifyView")}
ALLOWED[("founder_tools.my_startup.api", "MyStartupFounderDraftCopiedView")] = {"POST"}
ALLOWED[("founder_tools.my_startup.api", "MyStartupFounderDraftDownloadView")] = {"GET"}
for _name, _methods in {
        "AccountView": {"GET"}, "BalanceView": {"GET"}, "FounderBootstrapView": {"GET"},
        "ProfileView": {"GET", "POST"}, "CompaniesView": {"GET", "POST"},
        "ActiveCompanyView": {"POST"}, "BootstrapView": {"GET"}, "SettingsView": {"GET", "POST"},
        "ResearchView": {"POST"}, "RunView": {"GET"}, "ArticleView": {"POST"},
        "ArticleReviewView": {"GET"}, "ArticlePreviewView": {"POST"},
}.items():
    ALLOWED[("community_chat.my_startup_views", _name)] = _methods
for _name, _methods in {"FounderJourneyView": {"GET"}, "FirstArticleCardView": {"GET"},
                         "FirstArticleStartView": {"POST"}}.items():
    ALLOWED[("community_chat.my_startup_journey_views", _name)] = _methods


def active():
    """Existing production, local and password-pilot profiles stay unchanged."""
    return getattr(settings, "SETTINGS_MODULE", "") == PROFILE


def _refuse():
    return JsonResponse({"error": "founder_fixture_request_refused"}, status=401)


def _json_body(request):
    if request.content_type != "application/json" or len(request.body) > 16384:
        raise ValueError("Expected bounded JSON")
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def require_browser_credential(request, credential):
    """Check the already authenticated namespace without an eligibility shortcut.

    Pending onboarding still uses its normal user/session locks and consent
    service. Adding the pilot's eligible-only unsafe guard here would prevent
    a fresh member from completing onboarding.
    """
    if not active():
        return
    from community_chat.founder_staging_identity import require_session, InvalidFixtureIdentity
    try:
        require_session(credential)
    except InvalidFixtureIdentity:
        raise AuthenticationFailed("Founder fixture browser credential is invalid.") from None
    if (settings.APP_ENV != "chat-staging" or credential is None
            or getattr(credential, "client_id", None) != CLIENT
            or getattr(credential, "origin", None) != FRONTEND
            or getattr(credential, "platform", None) != "web"
            or request.headers.get("Origin") != FRONTEND):
        raise AuthenticationFailed("Founder fixture browser credential is invalid.")


class FounderAdmissionMiddleware(MiddlewareMixin):
    """Refuse native, legacy and unreviewed routes before handler side effects."""

    def process_view(self, request, view, args, kwargs):
        if not active():
            return None
        cls = getattr(view, "view_class", None) or getattr(view, "cls", None)
        identity = (getattr(cls, "__module__", getattr(view, "__module__", "")),
                    getattr(cls, "__name__", getattr(view, "__name__", "")))
        method = request.method
        if identity == ("mlai.views", "health_live") and method in {"GET", "HEAD"}:
            return None
        # Navigation and iframe subresources carry no Origin/X-MLAI header. The
        # normal proxy must still verify its signed, expiring run-bound grant.
        if identity == ("content_factory.article_preview_lease", "ArticlePreviewLeaseProxyView"):
            return None if method in {"GET", "HEAD"} else _refuse()
        allowed = ALLOWED.get(identity, set())
        if method == "HEAD" and "GET" in allowed:
            method = "GET"
        if method not in allowed:
            return JsonResponse({"error": "founder_fixture_route_unavailable"}, status=404)
        if settings.APP_ENV != "chat-staging" or request.headers.get("Origin") != FRONTEND:
            return _refuse()
        authorization = request.headers.get("Authorization", "")
        if authorization:
            # Signed enrollment uses the normal short-lived bootstrap token;
            # account access/refresh Bearer tokens and ordinary JWTs are native.
            if identity not in BOOTSTRAP or not authorization.startswith("Bearer mlai_chat_"):
                return _refuse()
        elif identity not in ANONYMOUS and not KEY.fullmatch(request.headers.get("X-MLAI-Public-Key", "")):
            return _refuse()
        try:
            if identity == (CHAT, "EmailCodeRequestView"):
                data = _json_body(request)
                scope = fixture_scope()
                scope.validate_email(data.get("email"))
                device = data.get("device")
                if (data.get("client_id") != CLIENT or not isinstance(device, dict)
                        or device.get("platform") != "web" or not _uuid(device.get("installation_id"))
                        or not KEY.fullmatch(str(device.get("public_key", "")))
                        or (request.headers.get("X-MLAI-Public-Key") is not None
                            and request.headers["X-MLAI-Public-Key"] != device["public_key"])):
                    return _refuse()
            elif identity == (CHAT, "EmailCodeVerifyView"):
                data = _json_body(request)
                if (data.get("client_id") != CLIENT or not _uuid(data.get("installation_id"))
                        or not _uuid(data.get("challenge_id")) or data.get("device_request_id")):
                    return _refuse()
            elif identity in {(CHAT, "AccountSessionRefreshView"), (CHAT, "AccountSessionLogoutView")}:
                if _json_body(request):
                    return _refuse()
        except (ValueError, TypeError, UnicodeError):
            return _refuse()
        return None
