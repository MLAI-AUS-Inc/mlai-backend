"""Who may use the contacts API, and which blast channels are switched on.

Access needs an MLAI admin or committee account (a linked ``roo.PointsAdmin``
row with role ``admin`` or ``committee``, or a superuser) on its own verified
device installation, and ``COMMUNITY_CHAT_CRM_ENABLED``. Chat owner and admin
roles don't count. Email needs ``CRM_SENDING_ENABLED`` and a listmonk API
user; SMS and WhatsApp need their flags and Twilio credentials.
"""

from __future__ import annotations

from django.conf import settings

from community_chat.admin_access import is_mlai_admin_request

EMAIL_CREDENTIALS = ("LISTMONK_BASE_URL", "LISTMONK_API_USER", "LISTMONK_API_TOKEN")
TWILIO_CREDENTIALS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
SMS_CREDENTIALS = (*TWILIO_CREDENTIALS, "TWILIO_MESSAGING_SERVICE_SID")
WHATSAPP_CREDENTIALS = (*TWILIO_CREDENTIALS, "TWILIO_WHATSAPP_FROM")


def crm_enabled() -> bool:
    return bool(getattr(settings, "COMMUNITY_CHAT_CRM_ENABLED", False))


def is_crm_admin(request) -> bool:
    """Only an MLAI admin's session on that account's own verified installation."""

    return is_mlai_admin_request(request)


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
