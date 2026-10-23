"""Who may use the contacts API, and which blast channels are switched on.

Access needs the Chat owner or admin role on a verified device installation
and ``COMMUNITY_CHAT_CRM_ENABLED``. Channel settings are read with defaults
because the sending phases add them later.
"""

from __future__ import annotations

from django.conf import settings

from community_chat.permissions import account_chat_role, is_chat_admin

EMAIL_CREDENTIALS = ("LISTMONK_BASE_URL",)
TWILIO_CREDENTIALS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
SMS_CREDENTIALS = (*TWILIO_CREDENTIALS, "TWILIO_MESSAGING_SERVICE_SID")
WHATSAPP_CREDENTIALS = (*TWILIO_CREDENTIALS, "TWILIO_WHATSAPP_FROM")


def crm_enabled() -> bool:
    return bool(getattr(settings, "COMMUNITY_CHAT_CRM_ENABLED", False))


def is_crm_admin(request) -> bool:
    """Only an owner or admin session on its own verified installation."""

    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return is_chat_admin(
        account_chat_role(
            user,
            getattr(request, "community_chat_public_key", None),
            getattr(request, "community_chat_installation_id", None),
        )
    )


def crm_access(request) -> dict:
    """``enabled``, ``can_view`` and ``can_send`` for the signed-in account."""

    enabled = crm_enabled()
    allowed = enabled and is_crm_admin(request)
    # Sending has no separate role yet; the blast phases gate each channel.
    return {"enabled": enabled, "can_view": allowed, "can_send": allowed}


def channel_availability() -> dict:
    """Each channel's ``available`` flag and ``reason`` (``disabled`` or ``not_configured``)."""

    return {
        "email": _channel("CRM_SENDING_ENABLED", EMAIL_CREDENTIALS),
        "sms": _channel("CRM_SMS_ENABLED", SMS_CREDENTIALS),
        "whatsapp": _channel("CRM_WHATSAPP_ENABLED", WHATSAPP_CREDENTIALS),
    }


def _channel(flag_name: str, credential_names) -> dict:
    if not getattr(settings, flag_name, False):
        return {"available": False, "reason": "disabled"}
    if not all(str(getattr(settings, name, "") or "").strip() for name in credential_names):
        return {"available": False, "reason": "not_configured"}
    return {"available": True, "reason": None}
