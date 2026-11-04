"""Reserved founder-fixture email scope; ordinary runtime profiles stay unchanged."""
from dataclasses import dataclass, field
from datetime import timedelta
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
import requests

PROFILE = "mlai.settings_staging_founder"
FRONTEND = "https://founder-staging.mlai.au"
API = "https://api-founder-staging.mlai.au"
PROVIDER = "http://founder-ci-inbox:8990/deliver"
CLIENT = "mlai-chat-web"
LOCAL = re.compile(r"founder-acceptance-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


@dataclass(frozen=True)
class FixtureScope:
    """A fixed origin/client namespace and private provider credential."""
    domain: str
    writer_token: str = field(repr=False)
    origin: str = FRONTEND
    client: str = CLIENT

    def validate_email(self, email):
        """Refuse every address outside the reserved fresh UUID namespace."""
        if not isinstance(email, str) or len(email) > 320:
            raise ValueError("Invalid founder fixture recipient")
        local, separator, domain = email.partition("@")
        if not separator or domain != self.domain or not LOCAL.fullmatch(local):
            raise ValueError("Only reserved founder fixture recipients are permitted")


def fixture_scope():
    """Return the dedicated namespace, or None for every existing profile."""
    if settings.SETTINGS_MODULE != PROFILE:
        return None
    domain = getattr(settings, "FOUNDER_STAGING_EMAIL_DOMAIN", "")
    writer = getattr(settings, "FOUNDER_STAGING_INBOX_WRITE_TOKEN", "")
    capabilities = getattr(settings, "STAGING_CAPABILITIES", {})
    if (settings.APP_ENV != "chat-staging" or getattr(settings, "FOUNDER_STAGING_ENABLED", False) is not True
            or getattr(settings, "FOUNDER_STAGING_INBOX_PROVIDER_ENABLED", False) is not True
            or settings.COMMUNITY_CHAT_FRONTEND_URL != FRONTEND or settings.COMMUNITY_CHAT_API_AUDIENCE != API
            or settings.COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED or settings.COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED
            or not settings.COMMUNITY_CHAT_SIGNUP_ENABLED or not settings.COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED
            or not isinstance(capabilities, dict)
            or capabilities.get("email") != "local" or capabilities.get("account_creation") != "local"
            or getattr(settings, "FOUNDER_STAGING_INBOX_PROVIDER_URL", "") != PROVIDER
            or not isinstance(domain, str) or len(domain) > 253 or not DOMAIN.fullmatch(domain)
            or "staging" not in domain.split(".") or not isinstance(writer, str) or not TOKEN.fullmatch(writer)):
        raise ImproperlyConfigured("The separate founder staging email profile is not fully configured")
    if any(getattr(settings, name, False) for name in (
            "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", "ARTICLES_GOAL_NOTIFICATIONS_ENABLED",
            "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", "MY_STARTUP_TURN_REMINDERS_ENABLED")):
        raise ImproperlyConfigured("Founder staging notification flags must remain off")
    return FixtureScope(domain=domain, writer_token=writer)


def send_fixture_code(scope, *, delivery_id, challenge, recipient, code):
    """Deliver a normal leased outbox row using its immutable source window."""
    if fixture_scope() != scope or scope is None or challenge.origin != scope.origin or challenge.client_id != scope.client:
        raise ValueError("Email delivery belongs to another profile")
    scope.validate_email(recipient.email)
    now = timezone.now()
    issued, expires = challenge.created_at, challenge.expires_at
    if (not timezone.is_aware(issued) or not timezone.is_aware(expires) or not issued <= now < expires
            or now >= issued + timedelta(seconds=300) or expires - issued > timedelta(seconds=600)):
        raise ValueError("Fixture challenge is outside its original delivery window")
    payload = {"deliveryId": str(delivery_id), "email": recipient.email, "code": code,
               "issuedAt": issued.isoformat(), "expiresAt": expires.isoformat()}
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(PROVIDER, json=payload, headers={"Authorization": "Bearer " + scope.writer_token},
                                timeout=(2, 5), allow_redirects=False)
        if response.status_code != 202:
            raise RuntimeError("Founder fixture provider did not accept the delivery")
        receipt = response.json()
    if not isinstance(receipt, dict) or receipt.get("accepted") is not True or receipt.get("deliveryId") != str(delivery_id):
        raise ValueError("Founder fixture provider receipt identity differs")
    return {"delivery_id": str(delivery_id)}
