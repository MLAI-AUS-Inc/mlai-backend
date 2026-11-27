"""Account-owned Basic security review workspaces, independent of startup membership."""

from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from community_chat.account_sessions import (
    InvalidAccountSession,
    _valid_session,
    _validate_device_owner,
)
from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.models import CommunityChatAccountSession
from community_chat.onboarding import require_community_access

from .cybertest_entry import EntryAuthority, EntryError
from .cybertest_entry_views import (
    EntryErrors,
    ExchangeView,
    InspectView,
    local_origin,
)
from .cybertest_paths import base_path
from .cybertest_personal_scope import workspace_id
from .cybertest_views import PrivateView


def personal_identity(session_id, subject):
    local_origin()
    if not getattr(settings, "CYBERTEST_PHASED_SCANS_ENABLED", False):
        raise EntryError("ENTRY_UNAVAILABLE", "Basic reviews are unavailable.", 503)
    session = (
        CommunityChatAccountSession.objects.select_related("user").filter(pk=session_id).first()
    )
    if not _valid_session(session, timezone.now()):
        raise EntryError("ACCESS_REVOKED", "Sign in again to open your review.", 401)
    try:
        _validate_device_owner(session)
    except InvalidAccountSession as exc:
        raise EntryError("ACCESS_REVOKED", "Sign in again to open your review.", 401) from exc
    require_community_access(session.user)
    workspace = workspace_id(session.user.community_chat_profile_id)
    if subject != workspace:
        raise EntryError("ACCESS_REVOKED", "This workspace is unavailable.", 403)
    # The subject field is shared with the existing checkout protocol. There is
    # no startup record and the personal grant has a separate cache namespace.
    return dict(
        user_id=str(session.user_id),
        company_id=workspace,
        workspace_id=workspace,
        company_name="My security reviews",
        workspace_provisioning=True,
    )


def personal_authority():
    return EntryAuthority(cache, personal_identity, namespace="personal-basic-v1")


class AuthorizePersonal(EntryErrors, PrivateView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        if not isinstance(request.data, dict) or set(request.data) != {"state", "code_challenge"}:
            raise EntryError("INVALID_ENTRY", "Restart personal review sign-in.")
        state = request.data["state"]
        code = personal_authority().issue(
            str(request.community_chat_account_session.pk),
            str(request.user.pk),
            workspace_id(request.user.community_chat_profile_id),
            state,
            request.data["code_challenge"],
        )
        callback = local_origin() + base_path(settings.CYBERTEST_BASE_PATH) + "/auth/mlai/callback"
        return Response(
            {"callback_url": callback + "?" + urlencode({"code": code, "state": state})}
        )


class PersonalExchange(ExchangeView):
    authority_factory = staticmethod(personal_authority)


class PersonalInspect(InspectView):
    authority_factory = staticmethod(personal_authority)
