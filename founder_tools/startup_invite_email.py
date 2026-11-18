"""Context for the co-branded transactional Startup Team invitation."""
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings

from core.firebase_utils import FIREBASE_STORAGE_BUCKET

from .profile_fields import company_avatar_url

MESSAGE_KEY = "startup_team_invite"
MELBOURNE = ZoneInfo("Australia/Melbourne")


def frontend_origin():
    """Return a configured HTTPS origin, with no extra path or credentials."""
    try:
        url = urlsplit(settings.COMMUNITY_CHAT_FRONTEND_URL)
        if (url.scheme == "https" and url.hostname and not url.username and not url.password
                and url.port in (None, 443) and url.path in ("", "/") and not url.query and not url.fragment):
            return f"https://{url.netloc}"
    except (ValueError, AttributeError):
        pass
    return ""


def logo_url(company):
    """Use the company's own uploaded PNG from the configured branding bucket."""
    value = company_avatar_url(company)
    prefix = f"/v0/b/{FIREBASE_STORAGE_BUCKET}/o/company-avatars/{company.pk}/"
    try:
        url = urlsplit(value)
        path = unquote(url.path)
        key = path.removeprefix(prefix)
        if (url.scheme == "https" and url.netloc == "firebasestorage.googleapis.com"
                and not url.fragment and path.startswith(prefix)
                and "/" not in key and key.endswith(".png")):
            return value
    except ValueError:
        pass
    return ""


def template_context(invitation):
    """Freeze the branding and recipient copy when a send is queued."""
    company = invitation.company
    return {
        "startup_name": company.name,
        "startup_logo": logo_url(company),
        "inviter_name": invitation.inviter.full_name or "Your teammate",
        "invite_email": invitation.email,
        "expires_on": invitation.expires_at.astimezone(MELBOURNE).strftime("%-d %B %Y, %-I:%M %p %Z"),
        "accept_url": f"{frontend_origin()}/my-startup/join?invitation={invitation.public_id}",
        "asset_base": str(getattr(settings, "CRM_EMAIL_ASSET_BASE_URL", "")
                          or "https://api.mlai.au/static/crm/email").rstrip("/"),
        "shared_access": company.membership_policy == company.SHARED_ACCESS,
    }
