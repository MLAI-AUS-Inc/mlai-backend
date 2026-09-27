"""Revalidate a native/browser Chat handoff at the OAuth callback boundary."""
from django.utils import timezone
from django.core.exceptions import ValidationError

from community_chat.account_sessions import _valid_session
from community_chat.models import CommunityChatAccountSession
from founder_tools.models import VibeRaisingCompany


def valid_chat_oauth_context(payload, user_id):
    """A revoked account session or transferred startup invalidates pending consent."""
    session_id = payload.get("chat_session_id")
    company_id = payload.get("chat_company_id")
    if not session_id and not company_id:
        return True  # Existing non-Chat integrations retain their own authorization.
    if not session_id or not company_id:
        return False
    try:
        session = CommunityChatAccountSession.objects.select_related("user").filter(
            pk=session_id, user_id=user_id,
        ).first()
        company_filter = {"pk": company_id, "profile__user_id": user_id}
        if payload.get("organization_id") is not None:
            company_filter["organization_id"] = payload["organization_id"]
        return bool(_valid_session(session, timezone.now()) and VibeRaisingCompany.objects.filter(**company_filter).exists())
    except (ValueError, TypeError, ValidationError):
        return False
