from __future__ import annotations

import logging
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone

from organizations.models import Organization
from integrations import http_client as requests
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalFinancialRecord,
    ExternalServiceProvider,
    FinancialAccount,
    GoogleConnection,
)
from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailMessageArtifact,
    GmailSyncCursor,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupMetricObservation,
)
from integrations.services.gmail import build_gmail_service, get_message_metadata, list_message_page
from startup_updates.services import bind_user_to_startup, get_default_gmail_binding, resolve_or_create_profile
from integrations.services.xero_scopes import (
    XERO_REPORT_SCOPE_WARNING,
    XERO_REPORT_SCOPE_CONFIGURATION_WARNING,
    XERO_REQUIRED_OPERATIONAL_SCOPES,
    XERO_REQUIRED_REPORT_SCOPES,
    xero_can_request_report_scopes,
    xero_has_report_scope,
    xero_needs_report_reconnect,
)
from integrations.utils import normalize_domain

logger = logging.getLogger(__name__)

CONNECTOR_OAUTH_STATE_SESSION_KEY = "connector_oauth_state"
CONNECTOR_OAUTH_STATE_SIGNING_SALT = "mlai.integrations.connector_oauth_state.v1"
CONNECTOR_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
DEFAULT_CONNECTOR_NEXT_PATH = "/vibe-raising/connect-data"
ALLOWED_CONNECTOR_NEXT_PREFIXES = (
    "/vibe-raising/connect-data",
    "/vibe-raising/create-update",
)


@dataclass(frozen=True)
class ConnectorDefinition:
    provider: str
    slug: str
    label: str
    capabilities: tuple[str, ...]
    financial: bool = False


CONNECTOR_DEFINITIONS: dict[str, ConnectorDefinition] = {
    "gmail": ConnectorDefinition("gmail", "gmail", "Gmail", ("context",)),
    ExternalServiceProvider.STRIPE: ConnectorDefinition(
        ExternalServiceProvider.STRIPE,
        "stripe",
        "Stripe",
        ("metrics",),
        financial=True,
    ),
    ExternalServiceProvider.XERO: ConnectorDefinition(
        ExternalServiceProvider.XERO,
        "xero",
        "Xero",
        ("metrics",),
        financial=True,
    ),
    ExternalServiceProvider.BANK_FEED: ConnectorDefinition(
        ExternalServiceProvider.BANK_FEED,
        "bank-feed",
        "Bank Feed",
        ("cash_validation",),
        financial=True,
    ),
    ExternalServiceProvider.NOTION: ConnectorDefinition(
        ExternalServiceProvider.NOTION,
        "notion",
        "Notion",
        ("docs", "context"),
    ),
    ExternalServiceProvider.GOOGLE_DRIVE: ConnectorDefinition(
        ExternalServiceProvider.GOOGLE_DRIVE,
        "google-drive",
        "Google Drive",
        ("docs", "context"),
    ),
    ExternalServiceProvider.SLACK: ConnectorDefinition(
        ExternalServiceProvider.SLACK,
        "slack",
        "Slack",
        ("context",),
    ),
    ExternalServiceProvider.LINEAR: ConnectorDefinition(
        ExternalServiceProvider.LINEAR,
        "linear",
        "Linear",
        ("context",),
    ),
}

PROVIDER_ALIASES = {
    "gmail": "gmail",
    "google": "gmail",
    "google_gmail": "gmail",
    "stripe": ExternalServiceProvider.STRIPE,
    "xero": ExternalServiceProvider.XERO,
    "bank-feed": ExternalServiceProvider.BANK_FEED,
    "bank_feed": ExternalServiceProvider.BANK_FEED,
    "basiq": ExternalServiceProvider.BANK_FEED,
    "notion": ExternalServiceProvider.NOTION,
    "google-drive": ExternalServiceProvider.GOOGLE_DRIVE,
    "google_drive": ExternalServiceProvider.GOOGLE_DRIVE,
    "drive": ExternalServiceProvider.GOOGLE_DRIVE,
    "slack": ExternalServiceProvider.SLACK,
    "linear": ExternalServiceProvider.LINEAR,
}

EXTERNAL_PROVIDER_ORDER = (
    ExternalServiceProvider.STRIPE,
    ExternalServiceProvider.XERO,
    ExternalServiceProvider.BANK_FEED,
    ExternalServiceProvider.NOTION,
    ExternalServiceProvider.GOOGLE_DRIVE,
    ExternalServiceProvider.SLACK,
    ExternalServiceProvider.LINEAR,
)


class ConnectorConfigurationError(Exception):
    pass


class ConnectorOAuthError(Exception):
    pass


class ConnectorRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(int(retry_after_seconds or 1), 1)
        super().__init__(f"Connector rate limit exceeded; retry after {self.retry_after_seconds}s.")


def normalize_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower().replace(" ", "_")
    resolved = PROVIDER_ALIASES.get(normalized)
    if not resolved:
        raise ConnectorConfigurationError("Unknown connector provider.")
    return resolved


def connector_slug(provider: str) -> str:
    return CONNECTOR_DEFINITIONS[provider].slug


def _origin_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(str(url).strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _frontend_base_url() -> str:
    for setting_name in ("VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value.rstrip("/")
    return "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"


def _known_frontend_origins() -> set[str]:
    origins = {
        origin
        for origin in (
            _origin_from_url(getattr(settings, "VIBE_RAISING_URL", None)),
            _origin_from_url(getattr(settings, "DEFAULT_FRONTEND_URL", None)),
            _origin_from_url(getattr(settings, "MEDHACK_URL", None)),
            _origin_from_url(getattr(settings, "ESAFETY_URL", None)),
        )
        if origin
    }
    origin = _origin_from_url(_frontend_base_url())
    if origin:
        origins.add(origin)
    return origins


def normalize_connector_next(next_url: Optional[str]) -> str:
    frontend_base = _frontend_base_url()
    default_next = f"{frontend_base}{DEFAULT_CONNECTOR_NEXT_PATH}"
    raw_next = str(next_url or "").strip()
    if not raw_next or raw_next.startswith("//"):
        return default_next

    parsed = urllib.parse.urlparse(raw_next)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return default_next
        if _origin_from_url(raw_next) not in _known_frontend_origins():
            return default_next
        candidate = urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
    else:
        candidate = raw_next if raw_next.startswith("/") else f"/{raw_next}"

    if not any(candidate.startswith(prefix) for prefix in ALLOWED_CONNECTOR_NEXT_PREFIXES):
        return default_next

    return f"{frontend_base}{candidate}"


def _as_scope_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _stripe_oauth_scope() -> str:
    return (_as_scope_list(getattr(settings, "STRIPE_OAUTH_SCOPES", [])) or ["read_only"])[0]


def _xero_oauth_scope_list() -> list[str]:
    configured_scopes = _as_scope_list(getattr(settings, "XERO_OAUTH_SCOPES", []))
    expanded_scopes: list[str] = []
    for scope in configured_scopes:
        if scope == "accounting.transactions.read":
            expanded_scopes.extend(["accounting.invoices.read", "accounting.payments.read"])
        elif scope == "accounting.transactions":
            expanded_scopes.extend(["accounting.invoices", "accounting.payments"])
        else:
            expanded_scopes.append(scope)

    deduped_scopes: list[str] = []
    seen: set[str] = set()
    for scope in expanded_scopes:
        if scope in seen:
            continue
        seen.add(scope)
        deduped_scopes.append(scope)
    return deduped_scopes


def _xero_can_request_report_scopes() -> bool:
    return xero_can_request_report_scopes(_xero_oauth_scope_list())


def _slack_oauth_user_scope_list() -> list[str]:
    configured = _as_scope_list(
        getattr(settings, "SLACK_OAUTH_USER_SCOPES", None)
        or getattr(settings, "SLACK_OAUTH_SCOPES", [])
    )
    return _uniq_scopes(configured)


def _slack_oauth_bot_scope_list() -> list[str]:
    return _uniq_scopes(_as_scope_list(getattr(settings, "SLACK_OAUTH_BOT_SCOPES", [])))


def _linear_oauth_scope_list() -> list[str]:
    return _uniq_scopes(_as_scope_list(getattr(settings, "LINEAR_OAUTH_SCOPES", ["read"])))


def _uniq_scopes(scopes: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for scope in scopes:
        scope_text = str(scope or "").strip()
        if not scope_text or scope_text in seen:
            continue
        seen.add(scope_text)
        deduped.append(scope_text)
    return deduped


def _looks_like_placeholder_secret(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    placeholder_values = {
        "api-key",
        "basiq-api-key",
        "placeholder",
        "your-api-key",
        "your_basiq_api_key",
    }
    return not normalized or normalized in placeholder_values or "..." in normalized or normalized.endswith("_")


def _provider_configuration_error(provider: str) -> Optional[str]:
    provider = normalize_provider(provider)
    definition = CONNECTOR_DEFINITIONS[provider]

    if provider == ExternalServiceProvider.STRIPE:
        client_id = str(getattr(settings, "STRIPE_CONNECT_CLIENT_ID", "") or "").strip()
        secret_key = str(getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
        missing = []
        if not client_id:
            missing.append("STRIPE_CONNECT_CLIENT_ID")
        if not secret_key:
            missing.append("STRIPE_SECRET_KEY")
        if missing:
            return (
                f"{definition.label} OAuth is not configured. "
                f"Set {', '.join(missing)} for Stripe Connect OAuth."
            )
        if not client_id.startswith("ca_") or _looks_like_placeholder_secret(client_id):
            return (
                "Stripe OAuth is not configured with a valid Connect client ID. "
                "Set STRIPE_CONNECT_CLIENT_ID to the real ca_ value from Stripe Connect OAuth settings."
            )
        if not secret_key.startswith("sk_") or _looks_like_placeholder_secret(secret_key):
            return (
                "Stripe OAuth is not configured with a valid secret API key. "
                "Set STRIPE_SECRET_KEY to the real sk_test_ or sk_live_ value for the same Stripe mode."
            )
        if _stripe_oauth_scope() != "read_only":
            return "Stripe OAuth must be configured with read_only scope."
        return None

    if provider == ExternalServiceProvider.XERO:
        client_id = str(getattr(settings, "XERO_CLIENT_ID", "") or "").strip()
        client_secret = str(getattr(settings, "XERO_CLIENT_SECRET", "") or "").strip()
        redirect_uri = str(getattr(settings, "XERO_OAUTH_REDIRECT_URI", "") or "").strip()
        scopes = set(_xero_oauth_scope_list())
        required_scopes = set(XERO_REQUIRED_OPERATIONAL_SCOPES)
        missing = []
        if not client_id:
            missing.append("XERO_CLIENT_ID")
        if not client_secret:
            missing.append("XERO_CLIENT_SECRET")
        if not redirect_uri:
            missing.append("XERO_OAUTH_REDIRECT_URI")
        if missing:
            return (
                f"{definition.label} OAuth is not configured. "
                f"Set {', '.join(missing)} for Xero OAuth."
            )
        if client_id.lower() in {"xero-client-id", "client-id", "your-xero-client-id"} or _looks_like_placeholder_secret(client_id):
            return "Xero OAuth is not configured with a valid client ID."
        if client_secret.lower() in {"xero-client-secret", "client-secret", "your-xero-client-secret"} or _looks_like_placeholder_secret(client_secret):
            return "Xero OAuth is not configured with a valid client secret."
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc:
            return "Xero OAuth redirect URI must be an absolute http or https URL."
        missing_scopes = sorted(required_scopes - scopes)
        if missing_scopes:
            return f"Xero OAuth scopes are missing: {', '.join(missing_scopes)}."
        return None

    if provider == ExternalServiceProvider.BANK_FEED:
        api_key = str(getattr(settings, "BASIQ_API_KEY", "") or "").strip()
        base_url = str(getattr(settings, "BASIQ_API_BASE_URL", "") or "").strip()
        consent_ui_url = str(getattr(settings, "BASIQ_CONSENT_UI_URL", "") or "").strip()
        missing = []
        if not api_key:
            missing.append("BASIQ_API_KEY")
        if not base_url:
            missing.append("BASIQ_API_BASE_URL")
        if not consent_ui_url:
            missing.append("BASIQ_CONSENT_UI_URL")
        if missing:
            return (
                f"{definition.label} OAuth is not configured. "
                f"Set {', '.join(missing)} for Basiq."
            )
        if _looks_like_placeholder_secret(api_key):
            return "Bank Feed OAuth is not configured with a valid Basiq API key."
        if not base_url.startswith("https://") and not getattr(settings, "DEBUG", False):
            return "Bank Feed OAuth must use an HTTPS BASIQ_API_BASE_URL outside local development."
        if not consent_ui_url.startswith("https://") and not getattr(settings, "DEBUG", False):
            return "Bank Feed OAuth must use an HTTPS BASIQ_CONSENT_UI_URL outside local development."
        return None

    if provider == ExternalServiceProvider.SLACK:
        client_id = str(getattr(settings, "SLACK_CLIENT_ID", "") or "").strip()
        client_secret = str(getattr(settings, "SLACK_CLIENT_SECRET", "") or "").strip()
        redirect_uri = str(getattr(settings, "SLACK_OAUTH_REDIRECT_URI", "") or "").strip()
        user_scopes = set(_slack_oauth_user_scope_list())
        required_scopes = {
            "channels:read",
            "channels:history",
            "groups:read",
            "groups:history",
            "team:read",
            "users:read",
        }
        disallowed_dm_scopes = {"im:read", "im:history", "mpim:read", "mpim:history"}
        missing = []
        if not client_id:
            missing.append("SLACK_CLIENT_ID")
        if not client_secret:
            missing.append("SLACK_CLIENT_SECRET")
        if not redirect_uri:
            missing.append("SLACK_OAUTH_REDIRECT_URI")
        if missing:
            return (
                f"{definition.label} OAuth is not configured. "
                f"Set {', '.join(missing)} for Slack OAuth."
            )
        if _looks_like_placeholder_secret(client_id) or _looks_like_placeholder_secret(client_secret):
            return "Slack OAuth is not configured with valid app credentials."
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc:
            return "Slack OAuth redirect URI must be an absolute http or https URL."
        missing_scopes = sorted(required_scopes - user_scopes)
        if missing_scopes:
            return f"Slack OAuth user scopes are missing: {', '.join(missing_scopes)}."
        configured_dm_scopes = sorted(disallowed_dm_scopes & user_scopes)
        if configured_dm_scopes:
            return f"Slack OAuth v1 excludes DMs and MPIMs. Remove: {', '.join(configured_dm_scopes)}."
        return None

    if provider == ExternalServiceProvider.LINEAR:
        client_id = str(getattr(settings, "LINEAR_CLIENT_ID", "") or "").strip()
        client_secret = str(getattr(settings, "LINEAR_CLIENT_SECRET", "") or "").strip()
        redirect_uri = str(getattr(settings, "LINEAR_OAUTH_REDIRECT_URI", "") or "").strip()
        scopes = set(_linear_oauth_scope_list())
        missing = []
        if not client_id:
            missing.append("LINEAR_CLIENT_ID")
        if not client_secret:
            missing.append("LINEAR_CLIENT_SECRET")
        if not redirect_uri:
            missing.append("LINEAR_OAUTH_REDIRECT_URI")
        if missing:
            return (
                f"{definition.label} OAuth is not configured. "
                f"Set {', '.join(missing)} for Linear OAuth."
            )
        if _looks_like_placeholder_secret(client_id) or _looks_like_placeholder_secret(client_secret):
            return "Linear OAuth is not configured with valid app credentials."
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc:
            return "Linear OAuth redirect URI must be an absolute http or https URL."
        if "read" not in scopes:
            return "Linear OAuth scopes must include read."
        return None

    if not is_provider_configured(provider):
        return f"{definition.label} OAuth is not configured."
    return None


def is_provider_configured(provider: str) -> bool:
    provider = normalize_provider(provider)
    if provider == "gmail":
        return bool(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""))
    if provider == ExternalServiceProvider.STRIPE:
        return bool(
            getattr(settings, "STRIPE_CONNECT_CLIENT_ID", "")
            and getattr(settings, "STRIPE_SECRET_KEY", "")
            and _stripe_oauth_scope() == "read_only"
        )
    if provider == ExternalServiceProvider.XERO:
        return _provider_configuration_error(ExternalServiceProvider.XERO) is None
    if provider == ExternalServiceProvider.BANK_FEED:
        api_key = str(getattr(settings, "BASIQ_API_KEY", "") or "").strip()
        return bool(api_key and not _looks_like_placeholder_secret(api_key))
    if provider == ExternalServiceProvider.NOTION:
        return bool(getattr(settings, "NOTION_CLIENT_ID", "") and getattr(settings, "NOTION_CLIENT_SECRET", ""))
    if provider == ExternalServiceProvider.GOOGLE_DRIVE:
        return bool(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""))
    if provider == ExternalServiceProvider.SLACK:
        return bool(getattr(settings, "SLACK_CLIENT_ID", "") and getattr(settings, "SLACK_CLIENT_SECRET", ""))
    if provider == ExternalServiceProvider.LINEAR:
        return bool(getattr(settings, "LINEAR_CLIENT_ID", "") and getattr(settings, "LINEAR_CLIENT_SECRET", ""))
    return False


def _get_active_vibe_raising_company(user):
    try:
        from vibe_raising.models import VibeRaisingProfile
    except Exception:
        return None

    try:
        profile = (
            VibeRaisingProfile.objects.select_related("active_company")
            .prefetch_related("companies")
            .get(user=user)
        )
    except VibeRaisingProfile.DoesNotExist:
        return None

    if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
        return None
    return profile.active_company or profile.companies.first()


def resolve_connector_organization(user) -> Optional[Organization]:
    company = _get_active_vibe_raising_company(user)
    domain = normalize_domain(getattr(company, "domain", "") or "")
    if domain:
        organization, _startup_profile = resolve_or_create_profile(domain=domain)
        bind_user_to_startup(
            user=user,
            organization=organization,
            google_connection=getattr(user, "google_connection", None),
            role="founder",
            is_default_for_gmail=True,
        )
        return organization

    binding = (
        user.startup_bindings.select_related("organization")
        .order_by("-is_default_for_gmail", "-updated_at", "-id")
        .first()
    )
    return binding.organization if binding else None


def _state_store(request) -> dict[str, dict[str, Any]]:
    store = request.session.get(CONNECTOR_OAUTH_STATE_SESSION_KEY)
    return store if isinstance(store, dict) else {}


def _save_state(request, provider: str, next_url: str, extra: Optional[dict[str, Any]] = None) -> str:
    state_payload = {
        "provider": provider,
        "user_id": request.user.id,
        "nonce": secrets.token_urlsafe(24),
        "next": next_url,
        **(extra or {}),
    }
    state = signing.dumps(
        state_payload,
        salt=CONNECTOR_OAUTH_STATE_SIGNING_SALT,
        compress=True,
    )
    store = _state_store(request)
    store[provider] = {
        "state": state,
        "next": next_url,
        **(extra or {}),
    }
    request.session[CONNECTOR_OAUTH_STATE_SESSION_KEY] = store
    request.session.modified = True
    return state


def _consume_state(request, provider: str, state: str) -> dict[str, Any]:
    store = _state_store(request)
    payload = store.get(provider)
    if payload and state and secrets.compare_digest(str(payload.get("state") or ""), state):
        store.pop(provider, None)
        if store:
            request.session[CONNECTOR_OAUTH_STATE_SESSION_KEY] = store
        else:
            request.session.pop(CONNECTOR_OAUTH_STATE_SESSION_KEY, None)
        request.session.modified = True
        return payload

    if not state:
        raise ConnectorOAuthError("Invalid connector OAuth state.")

    try:
        signed_payload = signing.loads(
            state,
            salt=CONNECTOR_OAUTH_STATE_SIGNING_SALT,
            max_age=CONNECTOR_OAUTH_STATE_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired as exc:
        raise ConnectorOAuthError("Expired connector OAuth state. Please try connecting again.") from exc
    except signing.BadSignature as exc:
        raise ConnectorOAuthError("Invalid connector OAuth state.") from exc

    if not isinstance(signed_payload, dict):
        raise ConnectorOAuthError("Invalid connector OAuth state.")
    if signed_payload.get("provider") != provider:
        raise ConnectorOAuthError("Invalid connector OAuth state.")
    if str(signed_payload.get("user_id") or "") != str(request.user.id):
        raise ConnectorOAuthError("Invalid connector OAuth state.")

    return signed_payload


def _expires_at(token_data: dict[str, Any]) -> Optional[Any]:
    raw = token_data.get("expires_in")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return timezone.now() + timedelta(seconds=seconds)


def build_authorization_url(request, provider: str) -> str:
    provider = normalize_provider(provider)
    if provider == "gmail":
        raise ConnectorConfigurationError("Use the Gmail connector endpoint.")
    configuration_error = _provider_configuration_error(provider)
    if configuration_error:
        raise ConnectorConfigurationError(configuration_error)

    next_url = normalize_connector_next(request.GET.get("next"))
    organization = resolve_connector_organization(request.user)

    if provider == ExternalServiceProvider.BANK_FEED:
        return _build_basiq_consent_url(request, next_url=next_url, organization=organization)

    state = _save_state(
        request,
        provider,
        next_url,
        {"organization_id": organization.id if organization else None},
    )

    if provider == ExternalServiceProvider.STRIPE:
        stripe_scope = _stripe_oauth_scope()
        params = {
            "client_id": settings.STRIPE_CONNECT_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.STRIPE_OAUTH_REDIRECT_URI,
            "scope": stripe_scope,
            "state": state,
        }
        return "https://connect.stripe.com/oauth/authorize?" + urllib.parse.urlencode(params)

    if provider == ExternalServiceProvider.XERO:
        params = {
            "client_id": settings.XERO_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.XERO_OAUTH_REDIRECT_URI,
            "scope": " ".join(_xero_oauth_scope_list()),
            "state": state,
        }
        return "https://login.xero.com/identity/connect/authorize?" + urllib.parse.urlencode(params)

    if provider == ExternalServiceProvider.NOTION:
        params = {
            "client_id": settings.NOTION_CLIENT_ID,
            "response_type": "code",
            "owner": "user",
            "redirect_uri": settings.NOTION_OAUTH_REDIRECT_URI,
            "state": state,
        }
        return "https://api.notion.com/v1/oauth/authorize?" + urllib.parse.urlencode(params)

    if provider == ExternalServiceProvider.GOOGLE_DRIVE:
        params = {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_DRIVE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(_as_scope_list(getattr(settings, "GOOGLE_DRIVE_OAUTH_SCOPES", []))),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    if provider == ExternalServiceProvider.SLACK:
        user_scopes = _slack_oauth_user_scope_list()
        bot_scopes = _slack_oauth_bot_scope_list()
        params = {
            "client_id": settings.SLACK_CLIENT_ID,
            "redirect_uri": settings.SLACK_OAUTH_REDIRECT_URI,
            "state": state,
        }
        if bot_scopes:
            params["scope"] = ",".join(bot_scopes)
        if user_scopes:
            params["user_scope"] = ",".join(user_scopes)
        return "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)

    if provider == ExternalServiceProvider.LINEAR:
        params = {
            "client_id": settings.LINEAR_CLIENT_ID,
            "redirect_uri": settings.LINEAR_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(_linear_oauth_scope_list()),
            "state": state,
        }
        return "https://linear.app/oauth/authorize?" + urllib.parse.urlencode(params)

    raise ConnectorConfigurationError("Unsupported connector provider.")


def _basiq_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {"basiq-version": getattr(settings, "BASIQ_API_VERSION", "3.0")}
    headers.update(extra or {})
    return headers


def _basiq_token(*, scope: str, user_id: str = "") -> dict[str, Any]:
    data = {"scope": scope}
    if user_id:
        data["userId"] = user_id
    response = requests.post(
        f"{settings.BASIQ_API_BASE_URL.rstrip('/')}/token",
        headers=_basiq_headers(
            {
                "Authorization": f"Basic {settings.BASIQ_API_KEY}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        ),
        data=data,
        timeout=(3, 20),
    )
    response.raise_for_status()
    return response.json()


def _build_basiq_consent_url(request, *, next_url: str, organization: Optional[Organization]) -> str:
    existing_connection = (
        ExternalServiceConnection.objects.filter(
            user=request.user,
            provider=ExternalServiceProvider.BANK_FEED,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .exclude(external_account_id="")
        .order_by("-updated_at", "-id")
        .first()
    )
    basiq_user = {}
    basiq_user_id = str(existing_connection.external_account_id).strip() if existing_connection else ""

    server_token = _basiq_token(scope="SERVER_ACCESS").get("access_token")
    if not server_token:
        raise ConnectorOAuthError("Basiq did not return a server access token.")

    if not basiq_user_id:
        response = requests.post(
            f"{settings.BASIQ_API_BASE_URL.rstrip('/')}/users",
            headers=_basiq_headers(
                {
                    "Authorization": f"Bearer {server_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            ),
            json={"email": request.user.email},
            timeout=(3, 20),
        )
        response.raise_for_status()
        basiq_user = response.json()
        basiq_user_id = str(basiq_user.get("id") or "").strip()
        if not basiq_user_id:
            raise ConnectorOAuthError("Basiq did not return a user id.")
    elif existing_connection:
        basiq_user = dict((existing_connection.provider_metadata or {}).get("basiq_user") or {})
        if not basiq_user:
            basiq_user = {"id": basiq_user_id}

    client_token = _basiq_token(scope="CLIENT_ACCESS", user_id=basiq_user_id).get("access_token")
    if not client_token:
        raise ConnectorOAuthError("Basiq did not return a client access token.")

    state = _save_state(
        request,
        ExternalServiceProvider.BANK_FEED,
        next_url,
        {"organization_id": organization.id if organization else None, "basiq_user_id": basiq_user_id},
    )
    provider_metadata = dict(existing_connection.provider_metadata or {}) if existing_connection else {}
    provider_metadata.update(
        {
            "basiq_user": basiq_user,
            "consent_state": state,
            "consent_action": "connect" if existing_connection else "create",
        }
    )
    _upsert_connection(
        user=request.user,
        provider=ExternalServiceProvider.BANK_FEED,
        organization=organization,
        access_token="",
        refresh_token="",
        token_type="",
        token_expires_at=None,
        scopes=["CLIENT_ACCESS"],
        external_account_id=basiq_user_id,
        account_label="Basiq bank feed",
        status=ExternalServiceConnectionStatus.SYNCING,
        provider_metadata=provider_metadata,
    )

    params = {"token": client_token, "state": state}
    if existing_connection:
        params["action"] = "connect"
    return f"{settings.BASIQ_CONSENT_UI_URL}?{urllib.parse.urlencode(params)}"


def _exchange_stripe_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://connect.stripe.com/oauth/token",
        auth=(settings.STRIPE_SECRET_KEY, ""),
        data={"code": code, "grant_type": "authorization_code"},
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    return token_data


def _exchange_xero_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://identity.xero.com/connect/token",
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.XERO_OAUTH_REDIRECT_URI,
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    return token_data


def _exchange_notion_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://api.notion.com/v1/oauth/token",
        auth=(settings.NOTION_CLIENT_ID, settings.NOTION_CLIENT_SECRET),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.NOTION_OAUTH_REDIRECT_URI,
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    return token_data


def _exchange_google_drive_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_DRIVE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    return token_data


def _exchange_slack_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": settings.SLACK_CLIENT_ID,
            "client_secret": settings.SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.SLACK_OAUTH_REDIRECT_URI,
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if not token_data.get("ok", False):
        raise ConnectorOAuthError(token_data.get("error") or "Slack OAuth failed.")
    return token_data


def _exchange_linear_code(code: str) -> dict[str, Any]:
    response = requests.post(
        "https://api.linear.app/oauth/token",
        data={
            "client_id": settings.LINEAR_CLIENT_ID,
            "client_secret": settings.LINEAR_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.LINEAR_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    return token_data


def _refresh_linear_token(connection: ExternalServiceConnection) -> dict[str, Any]:
    if not connection.refresh_token:
        raise ConnectorOAuthError("Linear connection needs to be reauthorised.")
    response = requests.post(
        "https://api.linear.app/oauth/token",
        data={
            "client_id": settings.LINEAR_CLIENT_ID,
            "client_secret": settings.LINEAR_CLIENT_SECRET,
            "refresh_token": connection.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        raise ConnectorOAuthError(token_data.get("error_description") or token_data.get("error"))
    connection.access_token = str(token_data.get("access_token") or connection.access_token or "")
    connection.refresh_token = str(token_data.get("refresh_token") or connection.refresh_token or "")
    connection.token_type = str(token_data.get("token_type") or connection.token_type or "Bearer")
    connection.token_expires_at = _expires_at(token_data)
    scopes = _as_scope_list(token_data.get("scope"))
    if scopes:
        connection.scopes = scopes
    connection.status = ExternalServiceConnectionStatus.CONNECTED
    connection.last_error = ""
    connection.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "token_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_error",
            "updated_at",
        ]
    )
    return token_data


def _fetch_xero_connections(access_token: str) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.xero.com/connections",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=(3, 20),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _fetch_google_userinfo(access_token: str) -> dict[str, Any]:
    response = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=(3, 20),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _upsert_connection(
    *,
    user,
    provider: str,
    organization: Optional[Organization],
    access_token: str,
    refresh_token: str,
    token_type: str,
    token_expires_at,
    scopes: list[str],
    external_account_id: str,
    account_label: str,
    status: str = ExternalServiceConnectionStatus.CONNECTED,
    provider_metadata: Optional[dict[str, Any]] = None,
) -> ExternalServiceConnection:
    queryset = ExternalServiceConnection.objects.filter(user=user, provider=provider)
    connection = None
    if external_account_id:
        connection = queryset.filter(external_account_id=external_account_id).first()
    if connection is None:
        connection = queryset.first()
    if connection is None:
        connection = ExternalServiceConnection(user=user, provider=provider)

    connection.organization = organization
    connection.access_token = access_token or connection.access_token or ""
    connection.refresh_token = refresh_token or connection.refresh_token or ""
    connection.token_type = token_type or connection.token_type or ""
    connection.token_expires_at = token_expires_at
    connection.scopes = scopes
    connection.external_account_id = external_account_id or connection.external_account_id or ""
    connection.account_label = account_label or connection.account_label or ""
    connection.status = status
    connection.provider_metadata = provider_metadata or {}
    connection.last_error = ""
    connection.save()
    return connection


def _store_stripe_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    stripe_account_id = str(token_data.get("stripe_user_id") or "").strip()
    scopes = _as_scope_list(token_data.get("scope")) or _as_scope_list(getattr(settings, "STRIPE_OAUTH_SCOPES", []))
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.STRIPE,
        organization=organization,
        access_token=str(token_data.get("access_token") or ""),
        refresh_token=str(token_data.get("refresh_token") or ""),
        token_type=str(token_data.get("token_type") or "bearer"),
        token_expires_at=None,
        scopes=scopes,
        external_account_id=stripe_account_id,
        account_label=stripe_account_id or "Stripe account",
        provider_metadata={"livemode": token_data.get("livemode")},
    )


def _store_xero_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    access_token = str(token_data.get("access_token") or "")
    tenants = _fetch_xero_connections(access_token) if access_token else []
    primary_tenant = tenants[0] if tenants else {}
    tenant_id = str(primary_tenant.get("tenantId") or primary_tenant.get("tenant_id") or "").strip()
    tenant_name = str(primary_tenant.get("tenantName") or primary_tenant.get("tenant_name") or "").strip()
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.XERO,
        organization=organization,
        access_token=access_token,
        refresh_token=str(token_data.get("refresh_token") or ""),
        token_type=str(token_data.get("token_type") or "Bearer"),
        token_expires_at=_expires_at(token_data),
        scopes=_as_scope_list(token_data.get("scope")) or _xero_oauth_scope_list(),
        external_account_id=tenant_id,
        account_label=tenant_name or tenant_id or "Xero tenant",
        provider_metadata={"tenants": tenants},
    )


def _store_notion_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    workspace_id = str(token_data.get("workspace_id") or "").strip()
    workspace_name = str(token_data.get("workspace_name") or "").strip()
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.NOTION,
        organization=organization,
        access_token=str(token_data.get("access_token") or ""),
        refresh_token=str(token_data.get("refresh_token") or ""),
        token_type=str(token_data.get("token_type") or "bearer"),
        token_expires_at=None,
        scopes=[],
        external_account_id=workspace_id or str(token_data.get("bot_id") or ""),
        account_label=workspace_name or workspace_id or "Notion workspace",
        provider_metadata={key: value for key, value in token_data.items() if key != "access_token"},
    )


def _store_google_drive_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    access_token = str(token_data.get("access_token") or "")
    userinfo = _fetch_google_userinfo(access_token) if access_token else {}
    email = str(userinfo.get("email") or "").strip()
    subject = str(userinfo.get("sub") or "").strip()
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.GOOGLE_DRIVE,
        organization=organization,
        access_token=access_token,
        refresh_token=str(token_data.get("refresh_token") or ""),
        token_type=str(token_data.get("token_type") or "Bearer"),
        token_expires_at=_expires_at(token_data),
        scopes=_as_scope_list(token_data.get("scope")) or _as_scope_list(getattr(settings, "GOOGLE_DRIVE_OAUTH_SCOPES", [])),
        external_account_id=email or subject,
        account_label=email or "Google Drive",
        provider_metadata={"userinfo": userinfo},
    )


def _store_slack_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    team = token_data.get("team") if isinstance(token_data.get("team"), dict) else {}
    authed_user = token_data.get("authed_user") if isinstance(token_data.get("authed_user"), dict) else {}
    team_id = str(team.get("id") or "").strip()
    team_name = str(team.get("name") or "").strip()
    user_access_token = str(authed_user.get("access_token") or "").strip()
    bot_access_token = str(token_data.get("access_token") or "").strip()
    access_token = user_access_token or bot_access_token
    refresh_token = str(authed_user.get("refresh_token") or token_data.get("refresh_token") or "")
    scopes = _as_scope_list(authed_user.get("scope") or token_data.get("scope")) or _slack_oauth_user_scope_list()
    provider_metadata = {key: value for key, value in token_data.items() if key not in {"access_token", "refresh_token"}}
    provider_metadata["token_source"] = "authed_user" if user_access_token else "bot"
    provider_metadata["bot_token_present"] = bool(bot_access_token)
    provider_metadata["team"] = team
    provider_metadata["authed_user"] = {
        key: value
        for key, value in authed_user.items()
        if key not in {"access_token", "refresh_token"}
    }
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.SLACK,
        organization=organization,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=str(token_data.get("token_type") or authed_user.get("token_type") or "Bearer"),
        token_expires_at=_expires_at(token_data) or _expires_at(authed_user),
        scopes=scopes or _as_scope_list(getattr(settings, "SLACK_OAUTH_SCOPES", [])),
        external_account_id=team_id,
        account_label=team_name or team_id or "Slack workspace",
        provider_metadata=provider_metadata,
    )


def _store_linear_connection(user, organization: Optional[Organization], token_data: dict[str, Any]) -> ExternalServiceConnection:
    access_token = str(token_data.get("access_token") or "")
    refresh_token = str(token_data.get("refresh_token") or "")
    scopes = _as_scope_list(token_data.get("scope")) or _linear_oauth_scope_list()
    temporary_connection = type(
        "LinearTemporaryConnection",
        (),
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expires_at": None,
            "status": ExternalServiceConnectionStatus.CONNECTED,
            "save": lambda *args, **kwargs: None,
        },
    )()
    identity = {}
    if access_token:
        try:
            identity_payload = _linear_graphql_request(
                temporary_connection,
                """
                query LinearViewer {
                  viewer {
                    id
                    name
                    email
                    organization {
                      id
                      name
                      urlKey
                    }
                  }
                }
                """,
                {},
                allow_refresh=False,
            )
            identity = identity_payload.get("viewer") if isinstance(identity_payload.get("viewer"), dict) else {}
        except Exception:
            logger.exception("Unable to load Linear viewer during OAuth storage", extra={"user_id": user.id})
            identity = {}

    linear_org = identity.get("organization") if isinstance(identity.get("organization"), dict) else {}
    external_account_id = str(linear_org.get("id") or identity.get("id") or "").strip()
    account_label = (
        str(linear_org.get("name") or "").strip()
        or str(identity.get("email") or "").strip()
        or str(identity.get("name") or "").strip()
        or "Linear workspace"
    )
    return _upsert_connection(
        user=user,
        provider=ExternalServiceProvider.LINEAR,
        organization=organization,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=str(token_data.get("token_type") or "Bearer"),
        token_expires_at=_expires_at(token_data),
        scopes=scopes,
        external_account_id=external_account_id,
        account_label=account_label,
        provider_metadata={
            "viewer": identity,
            "organization": linear_org,
            "token_type": token_data.get("token_type"),
        },
    )


def complete_oauth_callback(request, provider: str) -> str:
    provider = normalize_provider(provider)
    if provider == "gmail":
        raise ConnectorOAuthError("Use the Gmail callback endpoint.")

    if request.GET.get("error"):
        raise ConnectorOAuthError(str(request.GET.get("error_description") or request.GET.get("error")))

    state_payload = _consume_state(request, provider, str(request.GET.get("state") or ""))
    next_url = normalize_connector_next(state_payload.get("next"))
    organization_id = state_payload.get("organization_id")
    organization = Organization.objects.filter(id=organization_id).first() if organization_id else resolve_connector_organization(request.user)

    if provider == ExternalServiceProvider.BANK_FEED:
        job_ids = [
            item.strip()
            for item in str(request.GET.get("jobIds") or request.GET.get("job_ids") or "").split(",")
            if item.strip()
        ]
        basiq_user_id = str(state_payload.get("basiq_user_id") or "").strip()
        connection = ExternalServiceConnection.objects.filter(
            user=request.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id=basiq_user_id,
        ).first()
        if connection:
            metadata = dict(connection.provider_metadata or {})
            metadata["job_ids"] = job_ids
            connection.provider_metadata = metadata
            connection.status = ExternalServiceConnectionStatus.SYNCING if job_ids else ExternalServiceConnectionStatus.CONNECTED
            connection.last_error = ""
            connection.save(update_fields=["provider_metadata", "status", "last_error", "updated_at"])
        return next_url

    code = str(request.GET.get("code") or "").strip()
    if not code:
        raise ConnectorOAuthError("Missing OAuth code.")

    try:
        if provider == ExternalServiceProvider.STRIPE:
            connection = _store_stripe_connection(request.user, organization, _exchange_stripe_code(code))
        elif provider == ExternalServiceProvider.XERO:
            connection = _store_xero_connection(request.user, organization, _exchange_xero_code(code))
        elif provider == ExternalServiceProvider.NOTION:
            connection = _store_notion_connection(request.user, organization, _exchange_notion_code(code))
        elif provider == ExternalServiceProvider.GOOGLE_DRIVE:
            connection = _store_google_drive_connection(request.user, organization, _exchange_google_drive_code(code))
        elif provider == ExternalServiceProvider.SLACK:
            connection = _store_slack_connection(request.user, organization, _exchange_slack_code(code))
        elif provider == ExternalServiceProvider.LINEAR:
            connection = _store_linear_connection(request.user, organization, _exchange_linear_code(code))
        else:
            raise ConnectorOAuthError("Unsupported connector provider.")
    except requests.RequestException as exc:
        logger.exception("Connector OAuth callback failed", extra={"provider": provider, "user_id": request.user.id})
        raise ConnectorOAuthError(f"Failed to connect {CONNECTOR_DEFINITIONS[provider].label}.") from exc

    logger.info(
        "Stored external service connection",
        extra={"provider": provider, "connection_id": connection.id, "user_id": request.user.id},
    )
    return next_url


def _basiq_api_url(path_or_url: str) -> str:
    value = str(path_or_url or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{settings.BASIQ_API_BASE_URL.rstrip('/')}/{value.lstrip('/')}"


def _basiq_get_json(path_or_url: str, server_token: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = requests.get(
        _basiq_api_url(path_or_url),
        headers=_basiq_headers({"Authorization": f"Bearer {server_token}", "Accept": "application/json"}),
        params=params,
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _basiq_collection_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _basiq_next_link(payload: dict[str, Any]) -> str:
    links = payload.get("links")
    if isinstance(links, dict):
        next_link = links.get("next")
        if isinstance(next_link, dict):
            return str(next_link.get("href") or next_link.get("url") or "").strip()
        return str(next_link or "").strip()
    if isinstance(payload.get("next"), str):
        return str(payload["next"]).strip()
    return ""


def _basiq_get_collection(path: str, server_token: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_path = path
    next_params = params
    page_count = 0
    while next_path and page_count < 25:
        payload = _basiq_get_json(next_path, server_token, next_params)
        items.extend(_basiq_collection_items(payload))
        next_path = _basiq_next_link(payload)
        next_params = None
        page_count += 1
    return items


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value") or value.get("current") or value.get("available")
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _datetime_or_none(value: Any):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value
    parsed = parse_datetime(str(value))
    if parsed and timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def _date_or_none(value: Any):
    if not value:
        return None
    parsed_datetime = parse_datetime(str(value))
    if parsed_datetime:
        return parsed_datetime.date()
    return parse_date(str(value))


def _xero_datetime_or_none(value: Any):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value
    raw = str(value).strip()
    match = re.match(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$", raw)
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=dt_timezone.utc)
    return _datetime_or_none(raw)


def _xero_date_or_none(value: Any):
    parsed_datetime = _xero_datetime_or_none(value)
    if parsed_datetime:
        return parsed_datetime.date()
    return _date_or_none(value)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value: Any = payload
        for part in key.split("."):
            value = _as_dict(value).get(part)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _basiq_job_has_failure(job: dict[str, Any]) -> bool:
    status_value = str(job.get("status") or job.get("state") or "").strip().lower()
    if status_value in {"failed", "failure", "error", "errored"}:
        return True
    steps = job.get("steps") or job.get("data") or []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_status = str(step.get("status") or step.get("state") or "").strip().lower()
            if step_status in {"failed", "failure", "error", "errored"}:
                return True
    return False


def _basiq_job_is_complete(job: dict[str, Any]) -> bool:
    status_value = str(job.get("status") or job.get("state") or "").strip().lower()
    if status_value in {"success", "successful", "complete", "completed"}:
        return True
    steps = job.get("steps") or job.get("data") or []
    if isinstance(steps, list) and steps:
        return all(
            str(_as_dict(step).get("status") or _as_dict(step).get("state") or "").strip().lower()
            in {"success", "successful", "complete", "completed"}
            for step in steps
        )
    return not status_value


def _basiq_job_error_message(job: dict[str, Any]) -> str:
    for key in ("error", "message", "description", "title"):
        value = job.get(key)
        if isinstance(value, dict):
            nested = _nested_text(value, "message", "description", "title", "code")
            if nested:
                return nested
        if value is not None and str(value).strip():
            return str(value).strip()
    steps = job.get("steps") or job.get("data") or []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            nested = _basiq_job_error_message(step)
            if nested:
                return nested
    return "Basiq could not retrieve bank data for this connection."


def _account_balance(account: dict[str, Any], *keys: str) -> Optional[Decimal]:
    balance = _as_dict(account.get("balance"))
    for key in keys:
        value = account.get(key)
        if value is None:
            value = balance.get(key)
        parsed = _decimal_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _transaction_account_id(payload: dict[str, Any]) -> str:
    account = payload.get("account")
    if isinstance(account, dict):
        return str(account.get("id") or account.get("accountId") or "").strip()
    if account is not None and str(account).strip():
        return str(account).strip()
    return _nested_text(payload, "accountId", "account_id")


def _poll_basiq_jobs(connection: ExternalServiceConnection, server_token: str) -> tuple[bool, str]:
    metadata = dict(connection.provider_metadata or {})
    job_ids = metadata.get("job_ids") or metadata.get("jobIds") or []
    if isinstance(job_ids, str):
        job_ids = [item.strip() for item in job_ids.split(",") if item.strip()]
    if not isinstance(job_ids, list) or not job_ids:
        return True, ""

    poll_attempts = max(1, int(getattr(settings, "BASIQ_JOB_POLL_ATTEMPTS", 3) or 3))
    poll_delay = float(getattr(settings, "BASIQ_JOB_POLL_DELAY_SECONDS", 0) or 0)
    latest_jobs: list[dict[str, Any]] = []
    for attempt in range(poll_attempts):
        latest_jobs = []
        failed_message = ""
        all_complete = True
        for job_id in job_ids:
            job = _basiq_get_json(f"/jobs/{job_id}", server_token)
            latest_jobs.append(job)
            if _basiq_job_has_failure(job):
                failed_message = _basiq_job_error_message(job)
                all_complete = False
                break
            if not _basiq_job_is_complete(job):
                all_complete = False
        if failed_message:
            metadata["jobs"] = latest_jobs
            connection.provider_metadata = metadata
            connection.status = ExternalServiceConnectionStatus.ERROR
            connection.last_error = failed_message
            connection.save(update_fields=["provider_metadata", "status", "last_error", "updated_at"])
            return False, failed_message
        if all_complete:
            metadata["jobs"] = latest_jobs
            connection.provider_metadata = metadata
            connection.save(update_fields=["provider_metadata", "updated_at"])
            return True, ""
        if poll_delay and attempt < poll_attempts - 1:
            import time

            time.sleep(poll_delay)

    metadata["jobs"] = latest_jobs
    connection.provider_metadata = metadata
    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = "Bank data is still syncing. Try again shortly."
    connection.save(update_fields=["provider_metadata", "status", "last_error", "updated_at"])
    return False, connection.last_error


def _upsert_basiq_accounts(connection: ExternalServiceConnection, accounts: list[dict[str, Any]], synced_at) -> dict[str, FinancialAccount]:
    account_by_external_id: dict[str, FinancialAccount] = {}
    for account in accounts:
        external_account_id = str(account.get("id") or account.get("accountId") or "").strip()
        if not external_account_id:
            continue
        institution = _as_dict(account.get("institution"))
        defaults = {
            "connection": connection,
            "user": connection.user,
            "organization": connection.organization,
            "account_label": _nested_text(account, "name", "accountName", "displayName") or external_account_id,
            "institution_id": str(institution.get("id") or account.get("institutionId") or "").strip(),
            "institution_name": str(institution.get("name") or account.get("institutionName") or "").strip(),
            "account_type": _nested_text(account, "type", "class"),
            "status": _nested_text(account, "status"),
            "currency": _nested_text(account, "currency"),
            "balance": _account_balance(account, "current", "balance", "amount"),
            "available_funds": _account_balance(account, "available", "availableFunds", "available_funds"),
            "raw_payload": account,
            "last_synced_at": synced_at,
        }
        financial_account, _created = FinancialAccount.objects.update_or_create(
            provider=ExternalServiceProvider.BANK_FEED,
            connection=connection,
            external_account_id=external_account_id,
            defaults=defaults,
        )
        account_by_external_id[external_account_id] = financial_account
    return account_by_external_id


def _upsert_basiq_transactions(
    connection: ExternalServiceConnection,
    transactions: list[dict[str, Any]],
    account_by_external_id: dict[str, FinancialAccount],
) -> int:
    upserted = 0
    for item in transactions:
        status_value = _nested_text(item, "status")
        if status_value and status_value.lower() != "posted":
            continue
        external_record_id = str(item.get("id") or item.get("transactionId") or "").strip()
        external_account_id = _transaction_account_id(item)
        if not external_record_id or not external_account_id:
            continue
        amount = _decimal_or_none(item.get("amount"))
        direction = _nested_text(item, "direction")
        if not direction and amount is not None:
            direction = "debit" if amount < 0 else "credit"
        merchant = _as_dict(item.get("merchant"))
        category = item.get("category")
        if isinstance(category, dict):
            category_text = _nested_text(category, "title", "name", "code")
        else:
            category_text = str(category or "").strip()
        defaults = {
            "record_type": ExternalFinancialRecord.RECORD_BANK_TRANSACTION,
            "connection": connection,
            "financial_account": account_by_external_id.get(external_account_id),
            "user": connection.user,
            "organization": connection.organization,
            "currency": _nested_text(item, "currency"),
            "amount": amount,
            "direction": direction.lower()[:16],
            "status": status_value or "posted",
            "posted_at": _datetime_or_none(item.get("postDate") or item.get("postedAt") or item.get("posted_at")),
            "transaction_date": _date_or_none(item.get("transactionDate") or item.get("transaction_date") or item.get("date")),
            "description": _nested_text(item, "description", "text", "reference"),
            "merchant_name": _nested_text(merchant, "businessName", "name", "merchantName"),
            "category": category_text,
            "class_name": _nested_text(item, "class", "transactionClass"),
            "raw_payload": item,
        }
        ExternalFinancialRecord.objects.update_or_create(
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id=external_account_id,
            external_record_id=external_record_id,
            defaults=defaults,
        )
        upserted += 1
    return upserted


def sync_basiq_connection(connection: ExternalServiceConnection) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.BANK_FEED:
        raise ConnectorConfigurationError("Connection is not a Bank Feed connection.")
    if not connection.external_account_id:
        raise ConnectorOAuthError("Bank Feed connection is missing its Basiq user id.")

    server_token = _basiq_token(scope="SERVER_ACCESS").get("access_token")
    if not server_token:
        raise ConnectorOAuthError("Basiq did not return a server access token.")

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    jobs_complete, job_message = _poll_basiq_jobs(connection, server_token)
    if not jobs_complete:
        return {
            "connectionId": connection.id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "status": connection.status,
            "error": job_message,
        }

    basiq_user_id = urllib.parse.quote(connection.external_account_id, safe="")
    accounts = _basiq_get_collection(f"/users/{basiq_user_id}/accounts", server_token)
    transactions_payload = _basiq_get_collection(
        f"/users/{basiq_user_id}/transactions",
        server_token,
        {"filter": "status.eq('posted')"},
    )

    synced_at = timezone.now()
    with transaction.atomic():
        account_by_external_id = _upsert_basiq_accounts(connection, accounts, synced_at)
        transactions_synced = _upsert_basiq_transactions(connection, transactions_payload, account_by_external_id)
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.sync_cursor = {
            "last_synced_at": synced_at.isoformat(),
            "accounts_synced": len(account_by_external_id),
            "transactions_synced": transactions_synced,
        }
        connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])

    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
        "accountsSynced": len(account_by_external_id),
        "accounts_synced": len(account_by_external_id),
        "transactionsSynced": transactions_synced,
        "transactions_synced": transactions_synced,
    }


def _money_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def serialize_bank_feed_accounts(user) -> dict[str, Any]:
    queryset = (
        FinancialAccount.objects.filter(
            user=user,
            provider=ExternalServiceProvider.BANK_FEED,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .select_related("connection")
        .order_by("institution_name", "account_label", "external_account_id")
    )
    accounts = [
        {
            "id": account.id,
            "connectionId": account.connection_id,
            "connection_id": account.connection_id,
            "externalAccountId": account.external_account_id,
            "external_account_id": account.external_account_id,
            "institutionName": account.institution_name,
            "institution_name": account.institution_name,
            "accountLabel": account.account_label,
            "account_label": account.account_label,
            "accountType": account.account_type,
            "account_type": account.account_type,
            "status": account.status,
            "currency": account.currency,
            "balance": _money_string(account.balance),
            "availableFunds": _money_string(account.available_funds),
            "available_funds": _money_string(account.available_funds),
            "lastSyncedAt": account.last_synced_at.isoformat() if account.last_synced_at else None,
            "last_synced_at": account.last_synced_at.isoformat() if account.last_synced_at else None,
        }
        for account in queryset
    ]
    return {"accounts": accounts}


def serialize_bank_feed_transactions(
    user,
    *,
    account_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    queryset = (
        ExternalFinancialRecord.objects.filter(
            user=user,
            provider=ExternalServiceProvider.BANK_FEED,
            record_type=ExternalFinancialRecord.RECORD_BANK_TRANSACTION,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .select_related("financial_account", "connection")
    )
    if account_id:
        queryset = queryset.filter(
            models_q_financial_account_id_or_external_account_id(account_id)
        )
    parsed_start_date = parse_date(str(start_date)) if start_date else None
    parsed_end_date = parse_date(str(end_date)) if end_date else None
    if parsed_start_date:
        queryset = queryset.filter(transaction_date__gte=parsed_start_date)
    if parsed_end_date:
        queryset = queryset.filter(transaction_date__lte=parsed_end_date)

    limit = min(max(int(limit or 50), 1), 100)
    records = queryset.order_by("-posted_at", "-transaction_date", "-id")[:limit]
    transactions = [
        {
            "id": record.id,
            "connectionId": record.connection_id,
            "connection_id": record.connection_id,
            "accountId": record.financial_account_id,
            "account_id": record.financial_account_id,
            "externalAccountId": record.external_account_id,
            "external_account_id": record.external_account_id,
            "externalTransactionId": record.external_record_id,
            "external_transaction_id": record.external_record_id,
            "amount": _money_string(record.amount),
            "currency": record.currency,
            "direction": record.direction,
            "status": record.status,
            "postedAt": record.posted_at.isoformat() if record.posted_at else None,
            "posted_at": record.posted_at.isoformat() if record.posted_at else None,
            "transactionDate": record.transaction_date.isoformat() if record.transaction_date else None,
            "transaction_date": record.transaction_date.isoformat() if record.transaction_date else None,
            "description": record.description,
            "merchantName": record.merchant_name,
            "merchant_name": record.merchant_name,
            "category": record.category,
            "className": record.class_name,
            "class_name": record.class_name,
            "accountLabel": record.financial_account.account_label if record.financial_account else None,
            "account_label": record.financial_account.account_label if record.financial_account else None,
        }
        for record in records
    ]
    return {"transactions": transactions}


def models_q_financial_account_id_or_external_account_id(account_id: str):
    from django.db.models import Q

    account_id = str(account_id or "").strip()
    if account_id.isdigit():
        return Q(financial_account_id=int(account_id)) | Q(external_account_id=account_id)
    return Q(external_account_id=account_id)


def _xero_required_token(connection: ExternalServiceConnection) -> str:
    expires_at = connection.token_expires_at
    if connection.access_token and (expires_at is None or expires_at > timezone.now() + timedelta(minutes=2)):
        return connection.access_token
    return _refresh_xero_connection(connection)


def _refresh_xero_connection(connection: ExternalServiceConnection) -> str:
    if not connection.refresh_token:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = "Xero connection needs to be reauthorised."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise ConnectorOAuthError(connection.last_error)

    response = requests.post(
        "https://identity.xero.com/connect/token",
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        data={
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    token_data = response.json()
    if token_data.get("error"):
        message = token_data.get("error_description") or token_data.get("error")
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = str(message or "Xero token refresh failed.")
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise ConnectorOAuthError(connection.last_error)

    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = "Xero token refresh did not return complete credentials."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise ConnectorOAuthError(connection.last_error)

    connection.access_token = access_token
    connection.refresh_token = refresh_token
    connection.token_type = str(token_data.get("token_type") or "Bearer")
    connection.token_expires_at = _expires_at(token_data)
    connection.scopes = _as_scope_list(token_data.get("scope")) or connection.scopes
    connection.status = ExternalServiceConnectionStatus.CONNECTED
    connection.last_error = ""
    connection.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "token_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_error",
            "updated_at",
        ]
    )
    return access_token


def _xero_get_json(
    connection: ExternalServiceConnection,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    if_modified_since: Optional[str] = None,
) -> dict[str, Any]:
    access_token = _xero_required_token(connection)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Xero-Tenant-Id": connection.external_account_id,
    }
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    response = requests.get(
        f"https://api.xero.com/api.xro/2.0/{path.lstrip('/')}",
        headers=headers,
        params=params,
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _xero_collection(
    connection: ExternalServiceConnection,
    path: str,
    key: str,
    *,
    params: Optional[dict[str, Any]] = None,
    if_modified_since: Optional[str] = None,
    paginated: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    page_size = int((params or {}).get("pageSize") or 100)
    while True:
        request_params = dict(params or {})
        if paginated:
            request_params["page"] = page
            request_params.setdefault("pageSize", page_size)
        payload = _xero_get_json(
            connection,
            path,
            params=request_params or None,
            if_modified_since=if_modified_since,
        )
        page_items = payload.get(key)
        if not isinstance(page_items, list):
            page_items = []
        items.extend(item for item in page_items if isinstance(item, dict))
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        page_count = int(pagination.get("pageCount") or pagination.get("page_count") or 0)
        if not paginated:
            break
        if page_count and page >= page_count:
            break
        if not page_count and len(page_items) < page_size:
            break
        page += 1
        if page > 100:
            break
    return items


def fetch_xero_accounting_report(
    connection: ExternalServiceConnection,
    report_name: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _xero_get_json(
        connection,
        f"/Reports/{report_name.strip('/')}",
        params=params,
    )


def _xero_contact_name(payload: dict[str, Any]) -> str:
    contact = _as_dict(payload.get("Contact") or payload.get("contact"))
    return _nested_text(contact, "Name", "name", "ContactID", "contact_id")


def _xero_currency(payload: dict[str, Any]) -> str:
    return _nested_text(payload, "CurrencyCode", "currency_code", "Currency", "currency")


def _xero_amount(payload: dict[str, Any], *keys: str) -> Optional[Decimal]:
    for key in keys or ("SubTotal", "sub_total", "Total", "total", "Amount", "amount"):
        if key in payload:
            parsed = _decimal_or_none(payload.get(key))
            if parsed is not None:
                return parsed
    return None


def _xero_invoice_description(payload: dict[str, Any]) -> str:
    parts = [
        _nested_text(payload, "InvoiceNumber", "invoice_number"),
        _nested_text(payload, "Reference", "reference"),
        _xero_contact_name(payload),
    ]
    return " · ".join(part for part in parts if part)


def _xero_record_external_id(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _nested_text(payload, key)
        if value:
            return value
    return ""


def _upsert_xero_repeating_invoices(connection: ExternalServiceConnection, invoices: list[dict[str, Any]]) -> int:
    upserted = 0
    for invoice in invoices:
        if _nested_text(invoice, "Type", "type").upper() and _nested_text(invoice, "Type", "type").upper() != "ACCREC":
            continue
        status_value = _nested_text(invoice, "Status", "status").upper()
        if status_value in {"DRAFT", "DELETED", "VOIDED"}:
            continue
        external_record_id = _xero_record_external_id(invoice, "RepeatingInvoiceID", "repeating_invoice_id")
        if not external_record_id:
            continue
        schedule = _as_dict(invoice.get("Schedule") or invoice.get("schedule"))
        defaults = {
            "record_type": ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
            "connection": connection,
            "financial_account": None,
            "user": connection.user,
            "organization": connection.organization,
            "currency": _xero_currency(invoice),
            "amount": _xero_amount(invoice, "SubTotal", "sub_total", "Total", "total"),
            "direction": "credit",
            "status": status_value or "AUTHORISED",
            "posted_at": _xero_datetime_or_none(invoice.get("UpdatedDateUTC") or invoice.get("updated_date_utc")),
            "transaction_date": _xero_date_or_none(
                schedule.get("NextScheduledDate")
                or schedule.get("next_scheduled_date")
                or schedule.get("StartDate")
                or schedule.get("start_date")
            ),
            "description": _xero_invoice_description(invoice) or "Xero repeating invoice",
            "merchant_name": _xero_contact_name(invoice),
            "category": "repeating_invoice",
            "class_name": _nested_text(invoice, "Type", "type"),
            "raw_payload": invoice,
        }
        ExternalFinancialRecord.objects.update_or_create(
            provider=ExternalServiceProvider.XERO,
            external_account_id=connection.external_account_id,
            external_record_id=external_record_id,
            defaults=defaults,
        )
        upserted += 1
    return upserted


def _upsert_xero_invoices(connection: ExternalServiceConnection, invoices: list[dict[str, Any]]) -> int:
    upserted = 0
    for invoice in invoices:
        if _nested_text(invoice, "Type", "type").upper() != "ACCREC":
            continue
        status_value = _nested_text(invoice, "Status", "status").upper()
        if status_value not in {"AUTHORISED", "PAID"}:
            continue
        external_record_id = _xero_record_external_id(invoice, "InvoiceID", "invoice_id")
        if not external_record_id:
            continue
        defaults = {
            "record_type": ExternalFinancialRecord.RECORD_XERO_INVOICE,
            "connection": connection,
            "financial_account": None,
            "user": connection.user,
            "organization": connection.organization,
            "currency": _xero_currency(invoice),
            "amount": _xero_amount(invoice, "SubTotal", "sub_total", "Total", "total"),
            "direction": "credit",
            "status": status_value,
            "posted_at": _xero_datetime_or_none(invoice.get("UpdatedDateUTC") or invoice.get("updated_date_utc")),
            "transaction_date": _xero_date_or_none(invoice.get("DateString") or invoice.get("Date") or invoice.get("date")),
            "description": _xero_invoice_description(invoice) or "Xero sales invoice",
            "merchant_name": _xero_contact_name(invoice),
            "category": "sales_invoice",
            "class_name": _nested_text(invoice, "Type", "type"),
            "raw_payload": invoice,
        }
        ExternalFinancialRecord.objects.update_or_create(
            provider=ExternalServiceProvider.XERO,
            external_account_id=connection.external_account_id,
            external_record_id=external_record_id,
            defaults=defaults,
        )
        upserted += 1
    return upserted


def _upsert_xero_payments(connection: ExternalServiceConnection, payments: list[dict[str, Any]]) -> int:
    upserted = 0
    for payment in payments:
        invoice = _as_dict(payment.get("Invoice") or payment.get("invoice"))
        invoice_type = _nested_text(invoice, "Type", "type").upper()
        if invoice_type and invoice_type != "ACCREC":
            continue
        status_value = _nested_text(payment, "Status", "status").upper()
        if status_value in {"DELETED", "VOIDED"}:
            continue
        external_record_id = _xero_record_external_id(payment, "PaymentID", "payment_id")
        if not external_record_id:
            continue
        defaults = {
            "record_type": ExternalFinancialRecord.RECORD_XERO_PAYMENT,
            "connection": connection,
            "financial_account": None,
            "user": connection.user,
            "organization": connection.organization,
            "currency": _xero_currency(payment) or _xero_currency(invoice),
            "amount": _xero_amount(payment, "Amount", "amount"),
            "direction": "credit",
            "status": status_value or "AUTHORISED",
            "posted_at": _xero_datetime_or_none(payment.get("UpdatedDateUTC") or payment.get("updated_date_utc")),
            "transaction_date": _xero_date_or_none(payment.get("Date") or payment.get("date")),
            "description": _xero_invoice_description(invoice) or "Xero invoice payment",
            "merchant_name": _xero_contact_name(invoice),
            "category": "payment",
            "class_name": invoice_type or "ACCREC",
            "raw_payload": payment,
        }
        ExternalFinancialRecord.objects.update_or_create(
            provider=ExternalServiceProvider.XERO,
            external_account_id=connection.external_account_id,
            external_record_id=external_record_id,
            defaults=defaults,
        )
        upserted += 1
    return upserted


def _previous_month_start(month: date) -> date:
    if month.month == 1:
        return date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def _xero_report_metric_window(today: date) -> tuple[date, date]:
    current_month = date(today.year, today.month, 1)
    previous_month = _previous_month_start(current_month)
    oldest_month = _previous_month_start(previous_month)
    return oldest_month, today


def _publish_xero_report_metrics_for_sync(connection: ExternalServiceConnection, today: date) -> dict[str, Any]:
    has_report_scope = xero_has_report_scope(connection.scopes)
    can_request_report_scopes = _xero_can_request_report_scopes()
    needs_report_scope_configuration = not has_report_scope and not can_request_report_scopes
    needs_report_reconnect = not has_report_scope and can_request_report_scopes and xero_needs_report_reconnect(connection.scopes)
    metric_warnings: list[str] = []
    metrics_published_count = 0

    if not has_report_scope:
        logger.warning(
            "Xero report metric sync skipped because reports scope is missing",
            extra={"connection_id": connection.id, "user_id": connection.user_id},
        )
        metric_warnings.append(
            XERO_REPORT_SCOPE_CONFIGURATION_WARNING
            if needs_report_scope_configuration
            else XERO_REPORT_SCOPE_WARNING
        )
    elif not connection.organization_id:
        logger.warning(
            "Xero report metric sync skipped because connection is not linked to an organization",
            extra={"connection_id": connection.id, "user_id": connection.user_id},
        )
        metric_warnings.append("Xero report metrics could not be published because the connection is not linked to a company.")
    else:
        try:
            from startup_updates.services import publish_xero_metric_observations

            start_date, end_date = _xero_report_metric_window(today)
            summary = publish_xero_metric_observations(
                organization=connection.organization,
                run=None,
                start_date=start_date,
                end_date=end_date,
            )
            metrics_published_count = int(summary.get("published_metric_count") or 0)
            metric_warnings = [
                str(warning or "").strip()
                for warning in summary.get("warnings", []) or []
                if str(warning or "").strip()
                and "deterministic accounting context" not in str(warning or "")
            ]
        except Exception as exc:
            logger.exception(
                "Xero report metric sync failed",
                extra={"connection_id": connection.id, "user_id": connection.user_id},
            )
            metric_warnings.append(str(exc) or "Xero report metrics could not be published after sync.")

    return {
        "hasReportScope": has_report_scope,
        "has_report_scope": has_report_scope,
        "needsReportReconnect": needs_report_reconnect,
        "needs_report_reconnect": needs_report_reconnect,
        "canRequestReportScopes": can_request_report_scopes,
        "can_request_report_scopes": can_request_report_scopes,
        "needsReportScopeConfiguration": needs_report_scope_configuration,
        "needs_report_scope_configuration": needs_report_scope_configuration,
        "metricsPublishedCount": metrics_published_count,
        "metrics_published_count": metrics_published_count,
        "metricWarnings": metric_warnings,
        "metric_warnings": metric_warnings,
    }


def sync_xero_connection(connection: ExternalServiceConnection) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.XERO:
        raise ConnectorConfigurationError("Connection is not a Xero connection.")
    if not connection.external_account_id:
        raise ConnectorOAuthError("Xero connection is missing its tenant id.")

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    now = timezone.now()
    cursor = dict(connection.sync_cursor or {})
    if_modified_since = str(cursor.get("if_modified_since") or (now - timedelta(days=395)).isoformat())

    repeating_invoices = _xero_collection(
        connection,
        "/RepeatingInvoices",
        "RepeatingInvoices",
        if_modified_since=if_modified_since,
    )
    invoices = _xero_collection(
        connection,
        "/Invoices",
        "Invoices",
        params={"where": 'Type=="ACCREC"', "Statuses": "AUTHORISED,PAID", "pageSize": 100},
        if_modified_since=if_modified_since,
        paginated=True,
    )
    payments = _xero_collection(
        connection,
        "/Payments",
        "Payments",
        params={"pageSize": 100},
        if_modified_since=if_modified_since,
        paginated=True,
    )

    with transaction.atomic():
        repeating_synced = _upsert_xero_repeating_invoices(connection, repeating_invoices)
        invoices_synced = _upsert_xero_invoices(connection, invoices)
        payments_synced = _upsert_xero_payments(connection, payments)
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = now
        connection.sync_cursor = {
            **cursor,
            "if_modified_since": now.isoformat(),
            "repeating_invoices_synced": repeating_synced,
            "invoices_synced": invoices_synced,
            "payments_synced": payments_synced,
        }
        connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])

    report_metric_summary = _publish_xero_report_metrics_for_sync(connection, now.date())

    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "lastSyncedAt": now.isoformat(),
        "last_synced_at": now.isoformat(),
        "repeatingInvoicesSynced": repeating_synced,
        "repeating_invoices_synced": repeating_synced,
        "invoicesSynced": invoices_synced,
        "invoices_synced": invoices_synced,
        "paymentsSynced": payments_synced,
        "payments_synced": payments_synced,
        **report_metric_summary,
    }


def _latest_xero_connection(user) -> Optional[ExternalServiceConnection]:
    return (
        ExternalServiceConnection.objects.filter(user=user, provider=ExternalServiceProvider.XERO)
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )


def _xero_record_source_id(record: ExternalFinancialRecord) -> str:
    return str(record.external_record_id or "")


def _serialize_xero_record(record: ExternalFinancialRecord) -> dict[str, Any]:
    raw_payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
    return {
        "id": record.id,
        "connectionId": record.connection_id,
        "connection_id": record.connection_id,
        "recordType": record.record_type,
        "record_type": record.record_type,
        "externalRecordId": _xero_record_source_id(record),
        "external_record_id": _xero_record_source_id(record),
        "externalTenantId": record.external_account_id,
        "external_tenant_id": record.external_account_id,
        "invoiceNumber": _nested_text(raw_payload, "InvoiceNumber", "invoice_number"),
        "invoice_number": _nested_text(raw_payload, "InvoiceNumber", "invoice_number"),
        "amount": _money_string(record.amount),
        "currency": record.currency,
        "direction": record.direction,
        "status": record.status,
        "postedAt": record.posted_at.isoformat() if record.posted_at else None,
        "posted_at": record.posted_at.isoformat() if record.posted_at else None,
        "transactionDate": record.transaction_date.isoformat() if record.transaction_date else None,
        "transaction_date": record.transaction_date.isoformat() if record.transaction_date else None,
        "description": record.description,
        "contactName": record.merchant_name,
        "contact_name": record.merchant_name,
        "category": record.category,
        "className": record.class_name,
        "class_name": record.class_name,
    }


def _xero_monthly_normalized_amount(record: ExternalFinancialRecord) -> Optional[Decimal]:
    amount = record.amount
    if amount is None:
        return None
    payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
    schedule = _as_dict(payload.get("Schedule") or payload.get("schedule"))
    unit = _nested_text(schedule, "Unit", "unit").upper()
    period = _decimal_or_none(schedule.get("Period") or schedule.get("period")) or Decimal("1")
    if period <= 0:
        period = Decimal("1")
    if unit in {"MONTHLY", "MONTH"}:
        return amount / period
    if unit in {"YEARLY", "YEAR"}:
        return amount / (Decimal("12") * period)
    if unit in {"WEEKLY", "WEEK"}:
        return amount * Decimal("52") / Decimal("12") / period
    if unit in {"DAILY", "DAY"}:
        return amount * Decimal("365") / Decimal("12") / period
    return amount


def _xero_preview_month(end_date: Optional[date]) -> date:
    resolved = end_date or timezone.now().date()
    return date(resolved.year, resolved.month, 1)


def _xero_metric_value_lookup(connection: ExternalServiceConnection, month: date) -> dict[str, str]:
    if connection.organization_id is None:
        return {}
    metrics = StartupMetricObservation.objects.filter(
        organization=connection.organization,
        source_provider=ExternalServiceProvider.XERO,
        period_month=month,
    ).order_by("metric_key", "-observed_at", "-updated_at", "-id")
    values: dict[str, str] = {}
    for metric in metrics:
        values.setdefault(metric.metric_key, metric.value_text)
    return values


def serialize_xero_preview(
    user,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    connection = _latest_xero_connection(user)
    if not connection:
        return {
            "tenantLabel": None,
            "tenant_label": None,
            "lastSyncedAt": None,
            "last_synced_at": None,
            "recurringInvoices": [],
            "recurring_invoices": [],
            "recentInvoices": [],
            "recent_invoices": [],
            "revenue": None,
            "burnRate": None,
            "burn_rate": None,
            "runway": None,
            "monthlyCosts": None,
            "monthly_costs": None,
            "operatingExpenses": None,
            "operating_expenses": None,
            "costOfSales": None,
            "cost_of_sales": None,
            "revenueGrowthRate": None,
            "revenue_growth_rate": None,
            "invoiceRevenue": "0",
            "invoice_revenue": "0",
            "invoiceCount": "0",
            "invoice_count": "0",
            "customerCount": "0",
            "customer_count": "0",
            "recurringInvoiceCount": "0",
            "recurring_invoice_count": "0",
            "cashCollected": "0",
            "cash_collected": "0",
            "currencies": [],
            "warnings": ["Xero is not connected."],
            "hasReportScope": False,
            "has_report_scope": False,
            "needsReportReconnect": False,
            "needs_report_reconnect": False,
            "canRequestReportScopes": _xero_can_request_report_scopes(),
            "can_request_report_scopes": _xero_can_request_report_scopes(),
            "needsReportScopeConfiguration": False,
            "needs_report_scope_configuration": False,
            "requiredReportScopes": list(XERO_REQUIRED_REPORT_SCOPES),
            "required_report_scopes": list(XERO_REQUIRED_REPORT_SCOPES),
        }

    parsed_start_date = parse_date(str(start_date)) if start_date else None
    parsed_end_date = parse_date(str(end_date)) if end_date else None
    base_queryset = (
        ExternalFinancialRecord.objects.filter(
            user=user,
            provider=ExternalServiceProvider.XERO,
            connection=connection,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
    )
    date_filtered = base_queryset
    if parsed_start_date:
        date_filtered = date_filtered.filter(transaction_date__gte=parsed_start_date)
    if parsed_end_date:
        date_filtered = date_filtered.filter(transaction_date__lte=parsed_end_date)

    recurring_records = list(
        base_queryset.filter(record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE)
        .order_by("-updated_at", "-transaction_date", "-id")[:10]
    )
    invoice_records = list(
        date_filtered.filter(record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE)
        .order_by("-transaction_date", "-posted_at", "-id")[:10]
    )
    all_invoice_records = list(date_filtered.filter(record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE))
    payment_records = list(date_filtered.filter(record_type=ExternalFinancialRecord.RECORD_XERO_PAYMENT))
    invoice_revenue = sum((record.amount or Decimal("0") for record in all_invoice_records), Decimal("0"))
    cash_collected = sum((abs(record.amount or Decimal("0")) for record in payment_records), Decimal("0"))
    currencies = sorted({record.currency for record in list(recurring_records) + invoice_records + payment_records if record.currency})
    monthly_recurring_revenue = sum(
        (value for value in (_xero_monthly_normalized_amount(record) for record in recurring_records) if value is not None),
        Decimal("0"),
    )
    customer_count = len({
        record.merchant_name
        for record in all_invoice_records + payment_records
        if str(record.merchant_name or "").strip()
    })
    metric_values = _xero_metric_value_lookup(connection, _xero_preview_month(parsed_end_date))
    warnings = []
    if len(currencies) > 1:
        warnings.append("Xero records include multiple currencies; do not combine them into one MRR value.")
    has_report_scope = xero_has_report_scope(connection.scopes)
    can_request_report_scopes = _xero_can_request_report_scopes()
    needs_report_scope_configuration = not has_report_scope and not can_request_report_scopes
    needs_report_reconnect = not has_report_scope and can_request_report_scopes and xero_needs_report_reconnect(connection.scopes)
    if needs_report_reconnect:
        warnings.append(XERO_REPORT_SCOPE_WARNING)
    elif needs_report_scope_configuration:
        warnings.append(XERO_REPORT_SCOPE_CONFIGURATION_WARNING)
    if connection.status == ExternalServiceConnectionStatus.ERROR and connection.last_error:
        warnings.append(connection.last_error)

    return {
        "tenantLabel": connection.account_label or connection.external_account_id,
        "tenant_label": connection.account_label or connection.external_account_id,
        "tenantId": connection.external_account_id,
        "tenant_id": connection.external_account_id,
        "lastSyncedAt": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "last_synced_at": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "monthlyRecurringRevenue": metric_values.get("mrr") or _money_string(monthly_recurring_revenue),
        "monthly_recurring_revenue": metric_values.get("mrr") or _money_string(monthly_recurring_revenue),
        "revenue": metric_values.get("revenue"),
        "burnRate": metric_values.get("burnRate"),
        "burn_rate": metric_values.get("burnRate"),
        "runway": metric_values.get("runway"),
        "monthlyCosts": metric_values.get("monthlyCosts"),
        "monthly_costs": metric_values.get("monthlyCosts"),
        "operatingExpenses": metric_values.get("operatingExpenses"),
        "operating_expenses": metric_values.get("operatingExpenses"),
        "costOfSales": metric_values.get("costOfSales"),
        "cost_of_sales": metric_values.get("costOfSales"),
        "revenueGrowthRate": metric_values.get("revenueGrowthRate"),
        "revenue_growth_rate": metric_values.get("revenueGrowthRate"),
        "invoiceRevenue": metric_values.get("invoiceRevenue") or _money_string(invoice_revenue),
        "invoice_revenue": metric_values.get("invoiceRevenue") or _money_string(invoice_revenue),
        "cashCollected": metric_values.get("cashCollected") or _money_string(cash_collected),
        "cash_collected": metric_values.get("cashCollected") or _money_string(cash_collected),
        "invoiceCount": metric_values.get("invoiceCount") or str(len(all_invoice_records)),
        "invoice_count": metric_values.get("invoiceCount") or str(len(all_invoice_records)),
        "customerCount": metric_values.get("customerCount") or str(customer_count),
        "customer_count": metric_values.get("customerCount") or str(customer_count),
        "recurringInvoiceCount": metric_values.get("recurringInvoiceCount") or str(len(recurring_records)),
        "recurring_invoice_count": metric_values.get("recurringInvoiceCount") or str(len(recurring_records)),
        "currencies": currencies,
        "warnings": warnings,
        "hasReportScope": has_report_scope,
        "has_report_scope": has_report_scope,
        "needsReportReconnect": needs_report_reconnect,
        "needs_report_reconnect": needs_report_reconnect,
        "canRequestReportScopes": can_request_report_scopes,
        "can_request_report_scopes": can_request_report_scopes,
        "needsReportScopeConfiguration": needs_report_scope_configuration,
        "needs_report_scope_configuration": needs_report_scope_configuration,
        "requiredReportScopes": list(XERO_REQUIRED_REPORT_SCOPES),
        "required_report_scopes": list(XERO_REQUIRED_REPORT_SCOPES),
        "recurringInvoices": [_serialize_xero_record(record) for record in recurring_records],
        "recurring_invoices": [_serialize_xero_record(record) for record in recurring_records],
        "recentInvoices": [_serialize_xero_record(record) for record in invoice_records],
        "recent_invoices": [_serialize_xero_record(record) for record in invoice_records],
    }


def serialize_xero_invoices(
    user,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    queryset = (
        ExternalFinancialRecord.objects.filter(
            user=user,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .select_related("connection")
    )
    parsed_start_date = parse_date(str(start_date)) if start_date else None
    parsed_end_date = parse_date(str(end_date)) if end_date else None
    if parsed_start_date:
        queryset = queryset.filter(transaction_date__gte=parsed_start_date)
    if parsed_end_date:
        queryset = queryset.filter(transaction_date__lte=parsed_end_date)
    limit = min(max(int(limit or 50), 1), 100)
    records = queryset.order_by("-transaction_date", "-posted_at", "-id")[:limit]
    return {"invoices": [_serialize_xero_record(record) for record in records]}


def _serialize_gmail_artifact_preview(artifact: GmailMessageArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "gmailMessageId": artifact.gmail_message_id,
        "gmail_message_id": artifact.gmail_message_id,
        "gmailThreadId": artifact.gmail_thread_id,
        "gmail_thread_id": artifact.gmail_thread_id,
        "subject": artifact.subject or "(No subject)",
        "fromAddress": artifact.from_address,
        "from_address": artifact.from_address,
        "date": artifact.internal_date.isoformat() if artifact.internal_date else None,
        "internalDate": artifact.internal_date.isoformat() if artifact.internal_date else None,
        "internal_date": artifact.internal_date.isoformat() if artifact.internal_date else None,
        "snippet": artifact.snippet or artifact.body_preview,
        "relevanceLabel": artifact.relevance_label,
        "relevance_label": artifact.relevance_label,
        "hasAttachments": bool(artifact.has_attachments),
        "has_attachments": bool(artifact.has_attachments),
    }


def _gmail_header_map(headers: Iterable[dict]) -> dict[str, str]:
    values: dict[str, str] = {}
    for header in headers or []:
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name:
            values[name] = value
    return values


def _parse_gmail_internal_date(value: Any) -> Optional[datetime]:
    if value in ("", None):
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return parse_datetime(str(value))
    return datetime.fromtimestamp(milliseconds / 1000, tz=dt_timezone.utc)


def _gmail_metadata_has_attachments(metadata: dict[str, Any]) -> bool:
    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    parts = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    for part in parts:
        body = part.get("body") if isinstance(part, dict) and isinstance(part.get("body"), dict) else {}
        filename = str(part.get("filename") or "").strip() if isinstance(part, dict) else ""
        if filename or body.get("attachmentId"):
            return True
    return False


def _serialize_gmail_metadata_preview(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    headers = _gmail_header_map(payload.get("headers") if isinstance(payload.get("headers"), list) else [])
    internal_date = _parse_gmail_internal_date(metadata.get("internalDate"))
    message_id = str(metadata.get("id") or "").strip()
    thread_id = str(metadata.get("threadId") or "").strip()
    return {
        "id": message_id,
        "gmailMessageId": message_id,
        "gmail_message_id": message_id,
        "gmailThreadId": thread_id,
        "gmail_thread_id": thread_id,
        "subject": headers.get("subject") or "(No subject)",
        "fromAddress": headers.get("from") or "",
        "from_address": headers.get("from") or "",
        "date": internal_date.isoformat() if internal_date else headers.get("date"),
        "internalDate": internal_date.isoformat() if internal_date else None,
        "internal_date": internal_date.isoformat() if internal_date else None,
        "snippet": str(metadata.get("snippet") or "").strip(),
        "relevanceLabel": "metadata",
        "relevance_label": "metadata",
        "hasAttachments": _gmail_metadata_has_attachments(metadata),
        "has_attachments": _gmail_metadata_has_attachments(metadata),
    }


def _gmail_preview_binding(user):
    binding = get_default_gmail_binding(user=user)
    if binding and binding.google_connection:
        return binding
    connection = GoogleConnection.objects.filter(user=user).first()
    if not connection:
        return None
    return type("GmailPreviewBinding", (), {"organization": None, "google_connection": connection})()


def serialize_gmail_preview(user, *, limit: int = 5) -> dict[str, Any]:
    limit = min(max(int(limit or 5), 1), 10)
    binding = _gmail_preview_binding(user)
    if not binding or not binding.google_connection:
        return {
            "accountLabel": None,
            "account_label": None,
            "lastSyncedAt": None,
            "last_synced_at": None,
            "totalCachedMessages": 0,
            "total_cached_messages": 0,
            "messages": [],
            "warnings": ["Gmail is not connected."],
        }

    organization = binding.organization
    connection = binding.google_connection
    base_queryset = GmailMessageArtifact.objects.none()
    cursor = None
    if organization:
        base_queryset = GmailMessageArtifact.objects.filter(
            organization=organization,
            google_connection=connection,
        )
        cursor = GmailSyncCursor.objects.filter(
            organization=organization,
            google_connection=connection,
        ).first()
    total_cached = base_queryset.count()
    last_synced_at = None
    if cursor:
        last_synced_at = (
            cursor.last_message_internal_date
            or cursor.last_synced_internal_date
            or cursor.updated_at
        )

    artifacts = list(
        base_queryset
        .exclude(subject="")
        .order_by("-internal_date", "-id")[:limit]
    )
    warnings: list[str] = []
    messages = [_serialize_gmail_artifact_preview(artifact) for artifact in artifacts]

    if not messages:
        try:
            service = build_gmail_service(connection, cache_discovery=False)
            page = list_message_page(
                connection,
                query="newer_than:30d -in:spam -in:trash -category:promotions -category:social -category:forums",
                max_results=limit,
                service=service,
            )
            metadata_rows = [
                get_message_metadata(connection, str(item.get("id") or ""), service=service)
                for item in page.get("messages", [])
                if item.get("id")
            ]
            messages = [_serialize_gmail_metadata_preview(metadata) for metadata in metadata_rows[:limit]]
        except Exception as exc:  # pragma: no cover - exercised with mocks in tests and avoids preview hard-fail.
            logger.warning("Unable to fetch Gmail metadata preview for user %s: %s", user.id, exc)
            warnings.append("Gmail is connected, but recent message previews could not be loaded right now.")

    return {
        "accountLabel": connection.google_email,
        "account_label": connection.google_email,
        "lastSyncedAt": last_synced_at.isoformat() if last_synced_at else None,
        "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
        "totalCachedMessages": total_cached,
        "total_cached_messages": total_cached,
        "messages": messages,
        "warnings": warnings,
    }


LINEAR_PROJECT_LIST_QUERY = """
query LinearProjects($first: Int!, $after: String) {
  projects(first: $first, after: $after) {
    nodes {
      id
      name
      description
      createdAt
      updatedAt
      startDate
      targetDate
      startedAt
      completedAt
      canceledAt
      priority
      health
      progress
      scope
      url
      status { name type }
      lead { id name email }
      teams(first: 10) { nodes { id key name } }
      lastUpdate { id body health createdAt updatedAt url user { id name email } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


LINEAR_PROJECT_DETAIL_QUERY = """
query LinearProjectDetail($id: String!, $issueFirst: Int!, $issueAfter: String, $updateFirst: Int!, $updateAfter: String) {
  project(id: $id) {
    id
    name
    description
    createdAt
    updatedAt
    startDate
    targetDate
    startedAt
    completedAt
    canceledAt
    priority
    health
    progress
    scope
    url
    status { name type }
    lead { id name email }
    teams(first: 10) { nodes { id key name } }
    projectUpdates(first: $updateFirst, after: $updateAfter) {
      nodes {
        id
        body
        health
        createdAt
        updatedAt
        url
        user { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
    issues(first: $issueFirst, after: $issueAfter) {
      nodes {
        id
        identifier
        title
        description
        priority
        priorityLabel
        estimate
        dueDate
        createdAt
        updatedAt
        startedAt
        completedAt
        canceledAt
        url
        state { id name type }
        team { id key name }
        assignee { id name email }
        labels(first: 10) { nodes { id name } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _latest_linear_connection(user) -> Optional[ExternalServiceConnection]:
    return (
        ExternalServiceConnection.objects.filter(user=user, provider=ExternalServiceProvider.LINEAR)
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )


def _linear_retry_after_seconds(response) -> int:
    raw_retry_after = str(response.headers.get("Retry-After") or "").strip()
    if raw_retry_after:
        try:
            return max(int(float(raw_retry_after)), 1)
        except (TypeError, ValueError):
            pass
    raw_reset = str(response.headers.get("X-RateLimit-Requests-Reset") or "").strip()
    if raw_reset:
        try:
            reset_value = float(raw_reset)
            if reset_value > 10_000_000_000:
                reset_value = reset_value / 1000
            return max(int(reset_value - timezone.now().timestamp()), 1)
        except (TypeError, ValueError):
            pass
    return 1


def _linear_token_expired(connection: ExternalServiceConnection) -> bool:
    expires_at = getattr(connection, "token_expires_at", None)
    if expires_at is None:
        return False
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at)
    return expires_at <= timezone.now() + timedelta(seconds=60)


def _linear_required_token(connection: ExternalServiceConnection, *, allow_refresh: bool = True) -> str:
    if not getattr(connection, "access_token", ""):
        raise ConnectorOAuthError("Linear connection needs to be reauthorised.")
    if allow_refresh and _linear_token_expired(connection):
        _refresh_linear_token(connection)
    return str(connection.access_token or "")


def _linear_graphql_request(
    connection: ExternalServiceConnection,
    query: str,
    variables: Optional[dict[str, Any]] = None,
    *,
    allow_refresh: bool = True,
) -> dict[str, Any]:
    token = _linear_required_token(connection, allow_refresh=allow_refresh)
    connect_timeout = float(getattr(settings, "LINEAR_API_CONNECT_TIMEOUT_SECONDS", 3) or 3)
    read_timeout = float(getattr(settings, "LINEAR_API_READ_TIMEOUT_SECONDS", 20) or 20)
    response = requests.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=(connect_timeout, read_timeout),
    )
    if response.status_code == 429:
        raise ConnectorRateLimitError(_linear_retry_after_seconds(response))
    if response.status_code in {401, 403} and allow_refresh and getattr(connection, "refresh_token", ""):
        _refresh_linear_token(connection)
        return _linear_graphql_request(connection, query, variables, allow_refresh=False)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ConnectorOAuthError("Linear GraphQL returned an invalid response.")
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        first_error = errors[0] if isinstance(errors[0], dict) else {}
        message = str(first_error.get("message") or "Linear GraphQL request failed.")
        extension = first_error.get("extensions") if isinstance(first_error.get("extensions"), dict) else {}
        http_extension = extension.get("http") if isinstance(extension.get("http"), dict) else {}
        status_value = extension.get("status") or http_extension.get("status")
        code = str(extension.get("code") or "").lower()
        if status_value == 429 or "rate" in code or "rate limit" in message.lower():
            raise ConnectorRateLimitError(1)
        if code in {"unauthenticated", "authentication_error"} and allow_refresh and getattr(connection, "refresh_token", ""):
            _refresh_linear_token(connection)
            return _linear_graphql_request(connection, query, variables, allow_refresh=False)
        raise ConnectorOAuthError(message)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _linear_source_project_id(project_id: str) -> str:
    return f"linear:project:{project_id}"


def _linear_source_issue_id(identifier_or_id: str) -> str:
    return f"linear:issue:{identifier_or_id}"


def _linear_source_update_id(update_id: str) -> str:
    return f"linear:update:{update_id}"


def _linear_float_or_none(value: Any) -> Optional[float]:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _linear_project_is_active(project: dict[str, Any]) -> bool:
    if project.get("canceledAt"):
        return False
    status = project.get("status") if isinstance(project.get("status"), dict) else {}
    status_type = str(status.get("type") or "").lower()
    if status_type in {"canceled", "cancelled"}:
        return False
    return True


def _linear_project_defaults(project: dict[str, Any]) -> dict[str, Any]:
    status_payload = project.get("status") if isinstance(project.get("status"), dict) else {}
    lead = project.get("lead") if isinstance(project.get("lead"), dict) else {}
    teams_payload = project.get("teams") if isinstance(project.get("teams"), dict) else {}
    team_nodes = teams_payload.get("nodes") if isinstance(teams_payload.get("nodes"), list) else []
    team_names = [
        str(team.get("name") or team.get("key") or "").strip()
        for team in team_nodes
        if isinstance(team, dict) and str(team.get("name") or team.get("key") or "").strip()
    ]
    project_id = str(project.get("id") or "").strip()
    source_record_ids = [_linear_source_project_id(project_id)] if project_id else []
    last_update = project.get("lastUpdate") if isinstance(project.get("lastUpdate"), dict) else {}
    if last_update.get("id"):
        source_record_ids.append(_linear_source_update_id(str(last_update["id"])))
    return {
        "name": str(project.get("name") or project_id or "Linear project").strip(),
        "description": str(project.get("description") or ""),
        "status_name": str(status_payload.get("name") or ""),
        "status_type": str(status_payload.get("type") or ""),
        "health": str(project.get("health") or ""),
        "progress": _linear_float_or_none(project.get("progress")),
        "scope": _linear_float_or_none(project.get("scope")),
        "priority": int(_linear_float_or_none(project.get("priority")) or 0),
        "lead_name": str(lead.get("name") or ""),
        "lead_email": str(lead.get("email") or ""),
        "team_names": team_names,
        "start_date": _date_or_none(project.get("startDate")),
        "target_date": _date_or_none(project.get("targetDate")),
        "started_at": _datetime_or_none(project.get("startedAt")),
        "completed_at": _datetime_or_none(project.get("completedAt")),
        "canceled_at": _datetime_or_none(project.get("canceledAt")),
        "url": str(project.get("url") or ""),
        "source_record_ids": source_record_ids,
        "raw_payload": project,
    }


def _serialize_linear_project_selection(selection: LinearProjectSelection) -> dict[str, Any]:
    return {
        "id": selection.id,
        "projectId": selection.linear_project_id,
        "project_id": selection.linear_project_id,
        "linearProjectId": selection.linear_project_id,
        "linear_project_id": selection.linear_project_id,
        "name": selection.project_name or selection.linear_project_id,
        "projectName": selection.project_name,
        "project_name": selection.project_name,
        "status": selection.project_status,
        "health": selection.project_health,
        "selected": bool(selection.selected),
        "lastSyncedAt": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
        "last_synced_at": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
    }


def serialize_linear_projects(user, *, cursor: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    connection = _latest_linear_connection(user)
    if not connection:
        return {
            "accountLabel": None,
            "account_label": None,
            "workspaceId": None,
            "workspace_id": None,
            "projects": [],
            "nextCursor": None,
            "next_cursor": None,
            "warnings": ["Linear is not connected."],
        }

    limit = min(max(int(limit or 100), 1), 250)
    payload = _linear_graphql_request(
        connection,
        LINEAR_PROJECT_LIST_QUERY,
        {"first": limit, "after": cursor or None},
    )
    project_connection = payload.get("projects") if isinstance(payload.get("projects"), dict) else {}
    nodes = [item for item in project_connection.get("nodes") or [] if isinstance(item, dict)]
    project_rows = []
    with transaction.atomic():
        for project in nodes:
            if not _linear_project_is_active(project):
                continue
            project_id = str(project.get("id") or "").strip()
            if not project_id:
                continue
            status_payload = project.get("status") if isinstance(project.get("status"), dict) else {}
            defaults = {
                "user": connection.user,
                "organization": connection.organization,
                "project_name": str(project.get("name") or project_id).strip(),
                "project_status": str(status_payload.get("name") or status_payload.get("type") or ""),
                "project_health": str(project.get("health") or ""),
                "raw_payload": project,
            }
            selection, _created = LinearProjectSelection.objects.update_or_create(
                connection=connection,
                linear_project_id=project_id,
                defaults=defaults,
            )
            project_rows.append(selection)

    page_info = project_connection.get("pageInfo") if isinstance(project_connection.get("pageInfo"), dict) else {}
    next_cursor = str(page_info.get("endCursor") or "").strip() if page_info.get("hasNextPage") else ""
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "workspaceId": connection.external_account_id,
        "workspace_id": connection.external_account_id,
        "projects": [_serialize_linear_project_selection(selection) for selection in project_rows],
        "nextCursor": next_cursor or None,
        "next_cursor": next_cursor or None,
        "warnings": [],
    }


def _selected_linear_projects(connection: ExternalServiceConnection):
    return LinearProjectSelection.objects.filter(
        connection=connection,
        selected=True,
    ).order_by("project_name", "linear_project_id")


def update_linear_project_selections(user, project_ids: Iterable[str]) -> dict[str, Any]:
    connection = _latest_linear_connection(user)
    if not connection:
        raise ConnectorConfigurationError("Linear is not connected.")
    selected_ids = {
        str(project_id or "").strip()
        for project_id in project_ids or []
        if str(project_id or "").strip()
    }
    with transaction.atomic():
        LinearProjectSelection.objects.filter(connection=connection).update(selected=False)
        for project_id in sorted(selected_ids):
            selection, created = LinearProjectSelection.objects.get_or_create(
                connection=connection,
                linear_project_id=project_id,
                defaults={
                    "user": connection.user,
                    "organization": connection.organization,
                    "project_name": project_id,
                    "selected": True,
                },
            )
            if not created:
                selection.selected = True
                selection.user = connection.user
                selection.organization = connection.organization
                selection.save(update_fields=["selected", "user", "organization", "updated_at"])
        if selected_ids:
            LinearProjectSelection.objects.filter(
                connection=connection,
                linear_project_id__in=selected_ids,
            ).update(selected=True)
    selected = list(_selected_linear_projects(connection))
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "workspaceId": connection.external_account_id,
        "workspace_id": connection.external_account_id,
        "selectedProjects": [_serialize_linear_project_selection(selection) for selection in selected],
        "selected_projects": [_serialize_linear_project_selection(selection) for selection in selected],
        "selectedProjectCount": len(selected),
        "selected_project_count": len(selected),
    }


def _linear_selection_needs_work(selection: LinearProjectSelection, *, run_id: str) -> bool:
    cursor_payload = dict(selection.sync_cursor or {})
    if cursor_payload.get("startup_update_run_id") != run_id:
        return True
    return not bool(cursor_payload.get("run_backfill_complete"))


def _reset_linear_run_cursor(cursor_payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    cursor = dict(cursor_payload or {})
    if cursor.get("startup_update_run_id") == run_id:
        return cursor
    cursor.pop("issues_cursor", None)
    cursor.pop("updates_cursor", None)
    cursor.pop("run_backfill_complete", None)
    cursor.pop("run_issues_synced", None)
    cursor.pop("run_updates_synced", None)
    cursor["startup_update_run_id"] = run_id
    return cursor


def _upsert_linear_project_artifact(
    *,
    connection: ExternalServiceConnection,
    project: dict[str, Any],
) -> Optional[LinearProjectArtifact]:
    if not connection.organization:
        return None
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        return None
    existing = LinearProjectArtifact.objects.filter(
        organization=connection.organization,
        connection=connection,
        linear_project_id=project_id,
    ).first()
    defaults = _linear_project_defaults(project)
    content_changed = bool(existing and existing.raw_payload != project)
    if content_changed:
        defaults.update(
            {
                "relevance_label": GmailRelevanceLabel.PENDING,
                "relevance_score": 0.0,
                "relevance_reason": "",
                "needs_extraction": False,
                "extraction_hints": {},
                "classified_at": None,
                "extraction_status": ArtifactProcessingStatus.HYDRATED,
            }
        )
    elif existing and existing.extraction_status == ArtifactProcessingStatus.PROCESSED:
        defaults["extraction_status"] = ArtifactProcessingStatus.PROCESSED
    else:
        defaults["extraction_status"] = ArtifactProcessingStatus.HYDRATED
    artifact, _created = LinearProjectArtifact.objects.update_or_create(
        organization=connection.organization,
        connection=connection,
        linear_project_id=project_id,
        defaults=defaults,
    )
    return artifact


def _upsert_linear_issue_artifact(
    *,
    connection: ExternalServiceConnection,
    project_artifact: Optional[LinearProjectArtifact],
    issue: dict[str, Any],
) -> Optional[LinearIssueArtifact]:
    if not connection.organization:
        return None
    issue_id = str(issue.get("id") or "").strip()
    if not issue_id:
        return None
    state = issue.get("state") if isinstance(issue.get("state"), dict) else {}
    assignee = issue.get("assignee") if isinstance(issue.get("assignee"), dict) else {}
    team = issue.get("team") if isinstance(issue.get("team"), dict) else {}
    labels = issue.get("labels") if isinstance(issue.get("labels"), dict) else {}
    label_nodes = labels.get("nodes") if isinstance(labels.get("nodes"), list) else []
    identifier = str(issue.get("identifier") or issue_id).strip()
    source_record_id = _linear_source_issue_id(identifier or issue_id)
    artifact, _created = LinearIssueArtifact.objects.update_or_create(
        organization=connection.organization,
        connection=connection,
        linear_issue_id=issue_id,
        defaults={
            "project": project_artifact,
            "identifier": identifier,
            "title": str(issue.get("title") or ""),
            "description": str(issue.get("description") or ""),
            "state_name": str(state.get("name") or ""),
            "state_type": str(state.get("type") or ""),
            "priority": _linear_float_or_none(issue.get("priority")),
            "priority_label": str(issue.get("priorityLabel") or ""),
            "assignee_name": str(assignee.get("name") or ""),
            "assignee_email": str(assignee.get("email") or ""),
            "team_key": str(team.get("key") or ""),
            "team_name": str(team.get("name") or ""),
            "label_names": [
                str(label.get("name") or "").strip()
                for label in label_nodes
                if isinstance(label, dict) and str(label.get("name") or "").strip()
            ],
            "estimate": _linear_float_or_none(issue.get("estimate")),
            "due_date": _date_or_none(issue.get("dueDate")),
            "created_at_linear": _datetime_or_none(issue.get("createdAt")),
            "updated_at_linear": _datetime_or_none(issue.get("updatedAt")),
            "started_at": _datetime_or_none(issue.get("startedAt")),
            "completed_at": _datetime_or_none(issue.get("completedAt")),
            "canceled_at": _datetime_or_none(issue.get("canceledAt")),
            "url": str(issue.get("url") or ""),
            "source_record_id": source_record_id,
            "raw_payload": issue,
        },
    )
    return artifact


def _upsert_linear_project_update_artifact(
    *,
    connection: ExternalServiceConnection,
    project_artifact: Optional[LinearProjectArtifact],
    update: dict[str, Any],
) -> Optional[LinearProjectUpdateArtifact]:
    if not connection.organization:
        return None
    update_id = str(update.get("id") or "").strip()
    if not update_id:
        return None
    user = update.get("user") if isinstance(update.get("user"), dict) else {}
    artifact, _created = LinearProjectUpdateArtifact.objects.update_or_create(
        organization=connection.organization,
        connection=connection,
        linear_project_update_id=update_id,
        defaults={
            "project": project_artifact,
            "body": str(update.get("body") or ""),
            "health": str(update.get("health") or ""),
            "author_name": str(user.get("name") or ""),
            "author_email": str(user.get("email") or ""),
            "url": str(update.get("url") or ""),
            "created_at_linear": _datetime_or_none(update.get("createdAt")),
            "updated_at_linear": _datetime_or_none(update.get("updatedAt")),
            "source_record_id": _linear_source_update_id(update_id),
            "raw_payload": update,
        },
    )
    return artifact


def sync_linear_connection_page(
    connection: ExternalServiceConnection,
    *,
    run_id: str,
    project_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.LINEAR:
        raise ConnectorConfigurationError("Connection is not a Linear connection.")
    if not connection.organization:
        raise ConnectorConfigurationError("Linear connection is not linked to an organization.")
    if not connection.access_token:
        raise ConnectorOAuthError("Linear connection needs to be reauthorised.")

    selected_qs = _selected_linear_projects(connection)
    if project_ids:
        selected_set = {str(item or "").strip() for item in project_ids if str(item or "").strip()}
        selected_qs = selected_qs.filter(linear_project_id__in=selected_set)
    selections = list(selected_qs)
    if not selections:
        raise ConnectorConfigurationError("Select at least one Linear project before syncing.")

    selected_run_id = str(run_id or "").strip()
    if not selected_run_id:
        raise ConnectorConfigurationError("Linear sync run id is required.")

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    selection = next(
        (item for item in selections if _linear_selection_needs_work(item, run_id=selected_run_id)),
        None,
    )
    synced_at = timezone.now()
    if selection is None:
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])
        return {
            "connectionId": connection.id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "status": "synced",
            "lastSyncedAt": synced_at.isoformat(),
            "last_synced_at": synced_at.isoformat(),
            "projectsSynced": 0,
            "projects_synced": 0,
            "issuesSynced": 0,
            "issues_synced": 0,
            "updatesSynced": 0,
            "updates_synced": 0,
            "projects": [],
            "has_more": False,
        }

    cursor_payload = _reset_linear_run_cursor(dict(selection.sync_cursor or {}), run_id=selected_run_id)
    issue_limit = min(max(int(getattr(settings, "LINEAR_SYNC_ISSUE_PAGE_LIMIT", 50) or 50), 1), 250)
    update_limit = min(max(int(getattr(settings, "LINEAR_SYNC_UPDATE_PAGE_LIMIT", 20) or 20), 1), 100)

    try:
        data = _linear_graphql_request(
            connection,
            LINEAR_PROJECT_DETAIL_QUERY,
            {
                "id": selection.linear_project_id,
                "issueFirst": issue_limit,
                "issueAfter": str(cursor_payload.get("issues_cursor") or "") or None,
                "updateFirst": update_limit,
                "updateAfter": str(cursor_payload.get("updates_cursor") or "") or None,
            },
        )
        project = data.get("project") if isinstance(data.get("project"), dict) else None
        if project is None:
            cursor_payload["run_backfill_complete"] = True
            selection.sync_cursor = cursor_payload
            selection.last_synced_at = synced_at
            selection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
            project_result = {
                "projectId": selection.linear_project_id,
                "project_id": selection.linear_project_id,
                "projectName": selection.project_name,
                "project_name": selection.project_name,
                "issuesSynced": 0,
                "issues_synced": 0,
                "updatesSynced": 0,
                "updates_synced": 0,
                "notFound": True,
                "not_found": True,
            }
            issue_count = 0
            update_count = 0
            selection_has_more = False
        else:
            project_artifact = _upsert_linear_project_artifact(connection=connection, project=project)
            issues_payload = project.get("issues") if isinstance(project.get("issues"), dict) else {}
            updates_payload = project.get("projectUpdates") if isinstance(project.get("projectUpdates"), dict) else {}
            issue_nodes = [item for item in issues_payload.get("nodes") or [] if isinstance(item, dict)]
            update_nodes = [item for item in updates_payload.get("nodes") or [] if isinstance(item, dict)]
            issue_count = 0
            update_count = 0
            for issue in issue_nodes:
                if _upsert_linear_issue_artifact(
                    connection=connection,
                    project_artifact=project_artifact,
                    issue=issue,
                ):
                    issue_count += 1
            for update in update_nodes:
                if _upsert_linear_project_update_artifact(
                    connection=connection,
                    project_artifact=project_artifact,
                    update=update,
                ):
                    update_count += 1

            issue_page_info = issues_payload.get("pageInfo") if isinstance(issues_payload.get("pageInfo"), dict) else {}
            update_page_info = updates_payload.get("pageInfo") if isinstance(updates_payload.get("pageInfo"), dict) else {}
            if issue_page_info.get("hasNextPage"):
                cursor_payload["issues_cursor"] = str(issue_page_info.get("endCursor") or "")
            else:
                cursor_payload.pop("issues_cursor", None)
            if update_page_info.get("hasNextPage"):
                cursor_payload["updates_cursor"] = str(update_page_info.get("endCursor") or "")
            else:
                cursor_payload.pop("updates_cursor", None)

            cursor_payload["run_issues_synced"] = int(cursor_payload.get("run_issues_synced") or 0) + issue_count
            cursor_payload["run_updates_synced"] = int(cursor_payload.get("run_updates_synced") or 0) + update_count
            selection_has_more = bool(cursor_payload.get("issues_cursor") or cursor_payload.get("updates_cursor"))
            if not selection_has_more:
                cursor_payload["run_backfill_complete"] = True
                cursor_payload["last_synced_at"] = synced_at.isoformat()
                selection.last_synced_at = synced_at
            if project_artifact:
                selection.project_name = project_artifact.name
                selection.project_status = project_artifact.status_name or project_artifact.status_type
                selection.project_health = project_artifact.health
                selection.raw_payload = project
            project_result = {
                "projectId": selection.linear_project_id,
                "project_id": selection.linear_project_id,
                "projectName": selection.project_name,
                "project_name": selection.project_name,
                "issuesSynced": issue_count,
                "issues_synced": issue_count,
                "updatesSynced": update_count,
                "updates_synced": update_count,
            }

        selection.sync_cursor = cursor_payload
        selection.save(
            update_fields=[
                "sync_cursor",
                "last_synced_at",
                "project_name",
                "project_status",
                "project_health",
                "raw_payload",
                "updated_at",
            ]
        )

        has_more = selection_has_more or any(
            _linear_selection_needs_work(item, run_id=selected_run_id)
            for item in selections
            if item.pk != selection.pk
        )
        connection.status = ExternalServiceConnectionStatus.SYNCING if has_more else ExternalServiceConnectionStatus.CONNECTED
        if not has_more:
            connection.last_synced_at = synced_at
        connection.last_error = ""
        connection.sync_cursor = {
            **dict(connection.sync_cursor or {}),
            "last_synced_at": synced_at.isoformat(),
        }
        connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])
        return {
            "connectionId": connection.id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "status": "syncing" if has_more else "synced",
            "lastSyncedAt": synced_at.isoformat(),
            "last_synced_at": synced_at.isoformat(),
            "projectsSynced": 1,
            "projects_synced": 1,
            "issuesSynced": issue_count,
            "issues_synced": issue_count,
            "updatesSynced": update_count,
            "updates_synced": update_count,
            "projects": [project_result],
            "has_more": has_more,
        }
    except ConnectorRateLimitError:
        raise
    except (ConnectorOAuthError, requests.RequestException) as exc:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = str(exc) or "Linear sync failed."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise


def sync_linear_connection(
    connection: ExternalServiceConnection,
    *,
    project_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    run_id = f"manual-{timezone.now().timestamp()}"
    aggregate = {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "projectsSynced": 0,
        "projects_synced": 0,
        "issuesSynced": 0,
        "issues_synced": 0,
        "updatesSynced": 0,
        "updates_synced": 0,
        "projects": [],
        "has_more": False,
    }
    selected_count = _selected_linear_projects(connection).count()
    max_pages = max(selected_count * 20, 1)
    for _index in range(max_pages):
        page = sync_linear_connection_page(connection, run_id=run_id, project_ids=project_ids)
        aggregate["lastSyncedAt"] = page.get("lastSyncedAt")
        aggregate["last_synced_at"] = page.get("last_synced_at")
        aggregate["projectsSynced"] += int(page.get("projectsSynced") or 0)
        aggregate["projects_synced"] = aggregate["projectsSynced"]
        aggregate["issuesSynced"] += int(page.get("issuesSynced") or 0)
        aggregate["issues_synced"] = aggregate["issuesSynced"]
        aggregate["updatesSynced"] += int(page.get("updatesSynced") or 0)
        aggregate["updates_synced"] = aggregate["updatesSynced"]
        aggregate["projects"].extend(page.get("projects") or [])
        if not page.get("has_more"):
            aggregate["status"] = page.get("status") or "synced"
            aggregate["has_more"] = False
            return aggregate
    aggregate["status"] = "syncing"
    aggregate["has_more"] = True
    return aggregate


def _serialize_linear_project_artifact(project: LinearProjectArtifact) -> dict[str, Any]:
    issue_count = project.issues.count()
    update_count = project.project_updates.count()
    return {
        "id": project.id,
        "projectId": project.linear_project_id,
        "project_id": project.linear_project_id,
        "name": project.name,
        "description": project.description,
        "statusName": project.status_name,
        "status_name": project.status_name,
        "statusType": project.status_type,
        "status_type": project.status_type,
        "health": project.health,
        "progress": project.progress,
        "scope": project.scope,
        "priority": project.priority,
        "leadName": project.lead_name,
        "lead_name": project.lead_name,
        "teamNames": project.team_names or [],
        "team_names": project.team_names or [],
        "targetDate": project.target_date.isoformat() if project.target_date else None,
        "target_date": project.target_date.isoformat() if project.target_date else None,
        "url": project.url,
        "issueCount": issue_count,
        "issue_count": issue_count,
        "updateCount": update_count,
        "update_count": update_count,
    }


def serialize_linear_preview(user, *, limit: int = 5) -> dict[str, Any]:
    connection = _latest_linear_connection(user)
    if not connection:
        return {
            "accountLabel": None,
            "account_label": None,
            "workspaceId": None,
            "workspace_id": None,
            "lastSyncedAt": None,
            "last_synced_at": None,
            "selectedProjects": [],
            "selected_projects": [],
            "projects": [],
            "projectUpdates": [],
            "project_updates": [],
            "issues": [],
            "totalCachedProjects": 0,
            "total_cached_projects": 0,
            "totalCachedIssues": 0,
            "total_cached_issues": 0,
            "totalCachedUpdates": 0,
            "total_cached_updates": 0,
            "warnings": ["Linear is not connected."],
        }
    selected = list(_selected_linear_projects(connection))
    warnings: list[str] = []
    if not selected:
        warnings.append("Select at least one Linear project before syncing.")
    if connection.status == ExternalServiceConnectionStatus.ERROR and connection.last_error:
        warnings.append(connection.last_error)

    selected_ids = [selection.linear_project_id for selection in selected]
    project_queryset = LinearProjectArtifact.objects.filter(
        connection=connection,
        organization=connection.organization,
    )
    if selected_ids:
        project_queryset = project_queryset.filter(linear_project_id__in=selected_ids)
    issue_queryset = LinearIssueArtifact.objects.filter(
        connection=connection,
        organization=connection.organization,
    )
    update_queryset = LinearProjectUpdateArtifact.objects.filter(
        connection=connection,
        organization=connection.organization,
    )
    if selected_ids:
        issue_queryset = issue_queryset.filter(project__linear_project_id__in=selected_ids)
        update_queryset = update_queryset.filter(project__linear_project_id__in=selected_ids)

    limit = min(max(int(limit or 5), 1), 20)
    projects = [_serialize_linear_project_artifact(project) for project in project_queryset.order_by("name", "id")[:limit]]
    project_updates = [
        {
            "id": update.linear_project_update_id,
            "projectId": update.project.linear_project_id if update.project else None,
            "project_id": update.project.linear_project_id if update.project else None,
            "projectName": update.project.name if update.project else "",
            "project_name": update.project.name if update.project else "",
            "body": update.body,
            "health": update.health,
            "authorName": update.author_name,
            "author_name": update.author_name,
            "updatedAt": update.updated_at_linear.isoformat() if update.updated_at_linear else None,
            "updated_at": update.updated_at_linear.isoformat() if update.updated_at_linear else None,
            "url": update.url,
        }
        for update in update_queryset.select_related("project").order_by("-updated_at_linear", "-id")[:limit]
    ]
    issues = [
        {
            "id": issue.linear_issue_id,
            "identifier": issue.identifier,
            "projectId": issue.project.linear_project_id if issue.project else None,
            "project_id": issue.project.linear_project_id if issue.project else None,
            "projectName": issue.project.name if issue.project else "",
            "project_name": issue.project.name if issue.project else "",
            "title": issue.title,
            "stateName": issue.state_name,
            "state_name": issue.state_name,
            "stateType": issue.state_type,
            "state_type": issue.state_type,
            "priorityLabel": issue.priority_label,
            "priority_label": issue.priority_label,
            "assigneeName": issue.assignee_name,
            "assignee_name": issue.assignee_name,
            "updatedAt": issue.updated_at_linear.isoformat() if issue.updated_at_linear else None,
            "updated_at": issue.updated_at_linear.isoformat() if issue.updated_at_linear else None,
            "url": issue.url,
        }
        for issue in issue_queryset.select_related("project").order_by("-updated_at_linear", "-id")[:limit]
    ]
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "workspaceId": connection.external_account_id,
        "workspace_id": connection.external_account_id,
        "lastSyncedAt": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "last_synced_at": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "selectedProjects": [_serialize_linear_project_selection(selection) for selection in selected],
        "selected_projects": [_serialize_linear_project_selection(selection) for selection in selected],
        "projects": projects,
        "projectUpdates": project_updates,
        "project_updates": project_updates,
        "issues": issues,
        "totalCachedProjects": project_queryset.count(),
        "total_cached_projects": project_queryset.count(),
        "totalCachedIssues": issue_queryset.count(),
        "total_cached_issues": issue_queryset.count(),
        "totalCachedUpdates": update_queryset.count(),
        "total_cached_updates": update_queryset.count(),
        "warnings": warnings,
    }


def _latest_slack_connection(user) -> Optional[ExternalServiceConnection]:
    return (
        ExternalServiceConnection.objects.filter(user=user, provider=ExternalServiceProvider.SLACK)
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )


def _slack_source_id(channel_id: str, message_ts: str) -> str:
    return f"slack:{channel_id}:{message_ts}"


def _slack_datetime(value: Any) -> Optional[datetime]:
    if value in ("", None):
        return None
    try:
        seconds = float(str(value))
    except (TypeError, ValueError):
        return _datetime_or_none(value)
    return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)


def _slack_api_request(
    connection: ExternalServiceConnection,
    method: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    connect_timeout = float(getattr(settings, "SLACK_API_CONNECT_TIMEOUT_SECONDS", 3) or 3)
    read_timeout = float(getattr(settings, "SLACK_API_READ_TIMEOUT_SECONDS", 8) or 8)
    response = requests.get(
        f"https://slack.com/api/{method}",
        headers={
            "Authorization": f"Bearer {connection.access_token}",
            "Accept": "application/json",
        },
        params=params,
        timeout=(connect_timeout, read_timeout),
    )
    if response.status_code == 429:
        raw_retry_after = str(response.headers.get("Retry-After") or "1").strip()
        try:
            retry_after_seconds = int(float(raw_retry_after))
        except (TypeError, ValueError):
            retry_after_seconds = 1
        raise ConnectorRateLimitError(retry_after_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ConnectorOAuthError(f"Slack {method} returned an invalid response.")
    if not payload.get("ok", False):
        error = str(payload.get("error") or "Slack API request failed.")
        raise ConnectorOAuthError(error)
    return payload


def _clean_slack_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<@([A-Z0-9]+)>", r"@\1", text)
    text = re.sub(r"<#([A-Z0-9]+)\|([^>]+)>", r"#\2", text)
    text = re.sub(r"<(https?://[^>|]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def _slack_author_label(message: dict[str, Any]) -> str:
    profile = message.get("user_profile") if isinstance(message.get("user_profile"), dict) else {}
    bot_profile = message.get("bot_profile") if isinstance(message.get("bot_profile"), dict) else {}
    return (
        str(profile.get("real_name") or profile.get("name") or "").strip()
        or str(bot_profile.get("name") or "").strip()
        or str(message.get("username") or "").strip()
        or str(message.get("user") or message.get("bot_id") or "").strip()
        or "Slack user"
    )


def _slack_message_payload(
    artifact: SlackMessageArtifact,
) -> dict[str, Any]:
    return {
        "message_id": _slack_source_id(artifact.channel_id, artifact.slack_message_ts),
        "channel_id": artifact.channel_id,
        "channel_name": artifact.channel_name,
        "message_ts": artifact.slack_message_ts,
        "thread_ts": artifact.thread_ts,
        "author_id": artifact.author_id,
        "author_name": artifact.author_name,
        "posted_at": artifact.posted_at.isoformat() if artifact.posted_at else None,
        "cleaned_text": artifact.cleaned_text,
    }


def _upsert_slack_message(
    *,
    connection: ExternalServiceConnection,
    channel_id: str,
    channel_name: str,
    message: dict[str, Any],
) -> Optional[SlackMessageArtifact]:
    if not connection.organization:
        return None
    message_ts = str(message.get("ts") or "").strip()
    if not message_ts:
        return None
    posted_at = _slack_datetime(message_ts)
    if posted_at is None:
        return None
    thread_ts = str(message.get("thread_ts") or message_ts).strip()
    parent_ts = "" if thread_ts == message_ts else thread_ts
    text = str(message.get("text") or "")
    cleaned_text = _clean_slack_text(text)
    if not cleaned_text and not text:
        return None
    artifact, _created = SlackMessageArtifact.objects.update_or_create(
        organization=connection.organization,
        connection=connection,
        channel_id=channel_id,
        slack_message_ts=message_ts,
        defaults={
            "channel_name": channel_name,
            "thread_ts": thread_ts,
            "parent_ts": parent_ts,
            "author_id": str(message.get("user") or message.get("bot_id") or "").strip(),
            "author_name": _slack_author_label(message),
            "posted_at": posted_at,
            "text": text,
            "cleaned_text": cleaned_text,
            "raw_payload": message,
        },
    )
    return artifact


def _upsert_slack_thread_artifact(
    *,
    connection: ExternalServiceConnection,
    channel_id: str,
    channel_name: str,
    thread_ts: str,
) -> Optional[SlackThreadArtifact]:
    if not connection.organization or not thread_ts:
        return None
    messages = list(
        SlackMessageArtifact.objects.filter(
            organization=connection.organization,
            connection=connection,
            channel_id=channel_id,
            thread_ts=thread_ts,
        ).order_by("posted_at", "slack_message_ts")
    )
    if not messages:
        return None

    lines = []
    participants: dict[str, int] = {}
    for message in messages:
        author = message.author_name or message.author_id or "Slack user"
        participants[author] = participants.get(author, 0) + 1
        posted = message.posted_at.isoformat() if message.posted_at else message.slack_message_ts
        if message.cleaned_text:
            lines.append(f"[{posted}] {author}: {message.cleaned_text}")

    source_message_ids = [_slack_source_id(message.channel_id, message.slack_message_ts) for message in messages]
    cleaned_text = "\n".join(lines)
    existing = SlackThreadArtifact.objects.filter(
        organization=connection.organization,
        connection=connection,
        channel_id=channel_id,
        thread_ts=thread_ts,
    ).first()
    content_changed = bool(
        existing
        and (
            (existing.source_message_ids or []) != source_message_ids
            or (existing.cleaned_text or "") != cleaned_text
        )
    )
    extraction_status = (
        existing.extraction_status
        if existing and existing.extraction_status == ArtifactProcessingStatus.PROCESSED
        else existing.extraction_status
        if existing and not content_changed and existing.extraction_status == ArtifactProcessingStatus.UNSUPPORTED
        else ArtifactProcessingStatus.HYDRATED
    )
    classification_defaults = {}
    if content_changed:
        classification_defaults = {
            "relevance_label": "pending",
            "relevance_score": 0.0,
            "relevance_reason": "",
            "needs_extraction": False,
            "extraction_hints": {},
            "classified_at": None,
        }
    artifact, _created = SlackThreadArtifact.objects.update_or_create(
        organization=connection.organization,
        connection=connection,
        channel_id=channel_id,
        thread_ts=thread_ts,
        defaults={
            "channel_name": channel_name,
            "source_message_ids": source_message_ids,
            "source_message_count": len(messages),
            "cleaned_text": cleaned_text,
            "participant_summary": {
                "participants": sorted(participants.keys()),
                "message_counts": participants,
            },
            "message_payloads": [_slack_message_payload(message) for message in messages],
            "latest_message_at": max((message.posted_at for message in messages if message.posted_at), default=None),
            "extraction_status": extraction_status,
            "last_error": "",
            **classification_defaults,
        },
    )
    return artifact


def _selected_slack_channels(connection: ExternalServiceConnection):
    return SlackChannelSelection.objects.filter(
        connection=connection,
        selected=True,
    ).order_by("channel_name", "channel_id")


def _serialize_slack_channel_selection(selection: SlackChannelSelection) -> dict[str, Any]:
    return {
        "id": selection.id,
        "channelId": selection.channel_id,
        "channel_id": selection.channel_id,
        "channelName": selection.channel_name,
        "channel_name": selection.channel_name,
        "name": selection.channel_name,
        "isPrivate": bool(selection.is_private),
        "is_private": bool(selection.is_private),
        "selected": bool(selection.selected),
        "lastSyncedAt": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
        "last_synced_at": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
    }


def serialize_slack_channels(user, *, cursor: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
    connection = _latest_slack_connection(user)
    if not connection:
        return {
            "accountLabel": None,
            "account_label": None,
            "teamId": None,
            "team_id": None,
            "channels": [],
            "nextCursor": None,
            "next_cursor": None,
            "warnings": ["Slack is not connected."],
        }

    limit = min(max(int(limit or 200), 1), 1000)
    params = {
        "types": "public_channel,private_channel",
        "exclude_archived": "true",
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor
    payload = _slack_api_request(connection, "conversations.list", params=params)
    channels = [item for item in payload.get("channels", []) if isinstance(item, dict)]
    channel_rows = []
    with transaction.atomic():
        for channel in channels:
            channel_id = str(channel.get("id") or "").strip()
            if not channel_id:
                continue
            defaults = {
                "user": connection.user,
                "organization": connection.organization,
                "channel_name": str(channel.get("name") or channel_id).strip(),
                "is_private": bool(channel.get("is_private")),
                "raw_payload": channel,
            }
            selection, _created = SlackChannelSelection.objects.update_or_create(
                connection=connection,
                channel_id=channel_id,
                defaults=defaults,
            )
            channel_rows.append(selection)

    response_metadata = payload.get("response_metadata") if isinstance(payload.get("response_metadata"), dict) else {}
    next_cursor = str(response_metadata.get("next_cursor") or "").strip() or None
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "teamId": connection.external_account_id,
        "team_id": connection.external_account_id,
        "channels": [_serialize_slack_channel_selection(selection) for selection in channel_rows],
        "nextCursor": next_cursor,
        "next_cursor": next_cursor,
        "warnings": [],
    }


def update_slack_channel_selections(user, channel_ids: Iterable[str]) -> dict[str, Any]:
    connection = _latest_slack_connection(user)
    if not connection:
        raise ConnectorConfigurationError("Slack is not connected.")
    selected_ids = {
        str(channel_id or "").strip()
        for channel_id in channel_ids or []
        if str(channel_id or "").strip()
    }
    with transaction.atomic():
        SlackChannelSelection.objects.filter(connection=connection).update(selected=False)
        for channel_id in sorted(selected_ids):
            selection, created = SlackChannelSelection.objects.get_or_create(
                connection=connection,
                channel_id=channel_id,
                defaults={
                    "user": connection.user,
                    "organization": connection.organization,
                    "channel_name": channel_id,
                    "selected": True,
                },
            )
            if not created:
                selection.selected = True
                selection.user = connection.user
                selection.organization = connection.organization
                selection.save(update_fields=["selected", "user", "organization", "updated_at"])
        if selected_ids:
            SlackChannelSelection.objects.filter(
                connection=connection,
                channel_id__in=selected_ids,
            ).update(selected=True)
    selected = list(_selected_slack_channels(connection))
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "teamId": connection.external_account_id,
        "team_id": connection.external_account_id,
        "selectedChannels": [_serialize_slack_channel_selection(selection) for selection in selected],
        "selected_channels": [_serialize_slack_channel_selection(selection) for selection in selected],
        "selectedChannelCount": len(selected),
        "selected_channel_count": len(selected),
    }


def _slack_next_cursor(payload: dict[str, Any]) -> str:
    response_metadata = payload.get("response_metadata")
    if not isinstance(response_metadata, dict):
        return ""
    return str(response_metadata.get("next_cursor") or "").strip()


def _reset_slack_run_cursor(
    cursor_payload: dict[str, Any],
    *,
    run_id: str,
    oldest_ts: str,
    latest_ts: Optional[str],
) -> dict[str, Any]:
    cursor = dict(cursor_payload or {})
    if cursor.get("startup_update_run_id") == run_id:
        cursor.setdefault("sync_oldest", oldest_ts)
        if latest_ts:
            cursor.setdefault("sync_latest", latest_ts)
        return cursor

    cursor.pop("history_cursor", None)
    cursor.pop("pending_replies", None)
    cursor.pop("run_backfill_complete", None)
    cursor.pop("run_messages_synced", None)
    cursor.pop("run_threads_touched", None)
    cursor["startup_update_run_id"] = run_id
    cursor["sync_oldest"] = oldest_ts
    cursor["latest_seen"] = str(cursor.get("oldest") or "")
    if latest_ts:
        cursor["sync_latest"] = latest_ts
    else:
        cursor.pop("sync_latest", None)
    return cursor


def _slack_selection_needs_work(selection: SlackChannelSelection, *, run_id: str) -> bool:
    cursor_payload = dict(selection.sync_cursor or {})
    if cursor_payload.get("startup_update_run_id") != run_id:
        return True
    return not bool(cursor_payload.get("run_backfill_complete"))


def sync_slack_connection_page(
    connection: ExternalServiceConnection,
    *,
    run_id: str,
    channel_ids: Optional[Iterable[str]] = None,
    oldest: Optional[Any] = None,
    latest: Optional[Any] = None,
) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.SLACK:
        raise ConnectorConfigurationError("Connection is not a Slack connection.")
    if not connection.organization:
        raise ConnectorConfigurationError("Slack connection is not linked to an organization.")
    if not connection.access_token:
        raise ConnectorOAuthError("Slack connection needs to be reauthorised.")

    selected_qs = _selected_slack_channels(connection)
    if channel_ids:
        selected_set = {str(item or "").strip() for item in channel_ids if str(item or "").strip()}
        selected_qs = selected_qs.filter(channel_id__in=selected_set)
    selections = list(selected_qs)
    if not selections:
        raise ConnectorConfigurationError("Select at least one Slack channel before syncing.")

    history_limit = min(max(int(getattr(settings, "SLACK_SYNC_HISTORY_PAGE_LIMIT", 100) or 100), 1), 1000)
    reply_page_budget = max(1, int(getattr(settings, "SLACK_SYNC_REPLY_PAGE_BUDGET", 2) or 2))
    default_oldest_dt = timezone.now() - timedelta(days=int(getattr(settings, "SLACK_SYNC_HISTORY_DAYS", 120) or 120))
    default_oldest = str(default_oldest_dt.timestamp())
    explicit_oldest = str(oldest.timestamp()) if hasattr(oldest, "timestamp") else (str(oldest) if oldest else None)
    explicit_latest = str(latest.timestamp()) if hasattr(latest, "timestamp") else (str(latest) if latest else None)
    selected_run_id = str(run_id or "").strip()
    if not selected_run_id:
        raise ConnectorConfigurationError("Slack sync run id is required.")

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    selection = next(
        (item for item in selections if _slack_selection_needs_work(item, run_id=selected_run_id)),
        None,
    )
    synced_at = timezone.now()
    if selection is None:
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])
        return {
            "connectionId": connection.id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "status": "synced",
            "lastSyncedAt": synced_at.isoformat(),
            "last_synced_at": synced_at.isoformat(),
            "messagesSynced": 0,
            "messages_synced": 0,
            "threadsTouched": 0,
            "threads_touched": 0,
            "channels": [],
            "has_more": False,
        }

    cursor_payload = dict(selection.sync_cursor or {})
    oldest_ts = explicit_oldest or str(cursor_payload.get("oldest") or default_oldest)
    cursor_payload = _reset_slack_run_cursor(
        cursor_payload,
        run_id=selected_run_id,
        oldest_ts=oldest_ts,
        latest_ts=explicit_latest,
    )
    latest_seen = str(cursor_payload.get("latest_seen") or cursor_payload.get("oldest") or "")
    pending_replies = [
        dict(item)
        for item in (cursor_payload.get("pending_replies") or [])
        if isinstance(item, dict) and str(item.get("thread_ts") or "").strip()
    ]
    channel_thread_ts: set[str] = set()
    channel_message_count = 0
    reply_pages_processed = 0
    history_cursor = str(cursor_payload.get("history_cursor") or "").strip()
    had_pending_replies = bool(pending_replies)

    try:
        while pending_replies and reply_pages_processed < reply_page_budget:
            reply_state = pending_replies.pop(0)
            thread_ts = str(reply_state.get("thread_ts") or "").strip()
            if not thread_ts:
                continue
            reply_params = {
                "channel": selection.channel_id,
                "ts": thread_ts,
                "limit": history_limit,
            }
            reply_cursor = str(reply_state.get("cursor") or "").strip()
            if reply_cursor:
                reply_params["cursor"] = reply_cursor
            reply_payload = _slack_api_request(connection, "conversations.replies", params=reply_params)
            replies = [item for item in reply_payload.get("messages", []) if isinstance(item, dict)]
            for reply in replies:
                reply_artifact = _upsert_slack_message(
                    connection=connection,
                    channel_id=selection.channel_id,
                    channel_name=selection.channel_name,
                    message=reply,
                )
                if reply_artifact is None:
                    continue
                channel_message_count += 1
                channel_thread_ts.add(reply_artifact.thread_ts or reply_artifact.slack_message_ts)
                if str(reply_artifact.slack_message_ts) > latest_seen:
                    latest_seen = reply_artifact.slack_message_ts
            next_reply_cursor = _slack_next_cursor(reply_payload)
            if next_reply_cursor:
                pending_replies.append({"thread_ts": thread_ts, "cursor": next_reply_cursor})
            reply_pages_processed += 1

        if not pending_replies and not had_pending_replies:
            params = {
                "channel": selection.channel_id,
                "limit": history_limit,
                "oldest": str(cursor_payload.get("sync_oldest") or oldest_ts),
                "inclusive": "false",
            }
            sync_latest = str(cursor_payload.get("sync_latest") or explicit_latest or "").strip()
            if sync_latest:
                params["latest"] = sync_latest
            if history_cursor:
                params["cursor"] = history_cursor
            payload = _slack_api_request(connection, "conversations.history", params=params)
            messages = [item for item in payload.get("messages", []) if isinstance(item, dict)]
            for message in messages:
                artifact = _upsert_slack_message(
                    connection=connection,
                    channel_id=selection.channel_id,
                    channel_name=selection.channel_name,
                    message=message,
                )
                if artifact is None:
                    continue
                channel_message_count += 1
                channel_thread_ts.add(artifact.thread_ts or artifact.slack_message_ts)
                if str(artifact.slack_message_ts) > latest_seen:
                    latest_seen = artifact.slack_message_ts
                if int(message.get("reply_count") or 0) > 0:
                    pending_replies.append(
                        {
                            "thread_ts": str(message.get("thread_ts") or message.get("ts")),
                            "cursor": "",
                        }
                    )
            history_cursor = _slack_next_cursor(payload)

            while pending_replies and reply_pages_processed < reply_page_budget:
                reply_state = pending_replies.pop(0)
                thread_ts = str(reply_state.get("thread_ts") or "").strip()
                if not thread_ts:
                    continue
                reply_params = {
                    "channel": selection.channel_id,
                    "ts": thread_ts,
                    "limit": history_limit,
                }
                reply_cursor = str(reply_state.get("cursor") or "").strip()
                if reply_cursor:
                    reply_params["cursor"] = reply_cursor
                reply_payload = _slack_api_request(connection, "conversations.replies", params=reply_params)
                replies = [item for item in reply_payload.get("messages", []) if isinstance(item, dict)]
                for reply in replies:
                    reply_artifact = _upsert_slack_message(
                        connection=connection,
                        channel_id=selection.channel_id,
                        channel_name=selection.channel_name,
                        message=reply,
                    )
                    if reply_artifact is None:
                        continue
                    channel_message_count += 1
                    channel_thread_ts.add(reply_artifact.thread_ts or reply_artifact.slack_message_ts)
                    if str(reply_artifact.slack_message_ts) > latest_seen:
                        latest_seen = reply_artifact.slack_message_ts
                next_reply_cursor = _slack_next_cursor(reply_payload)
                if next_reply_cursor:
                    pending_replies.append({"thread_ts": thread_ts, "cursor": next_reply_cursor})
                reply_pages_processed += 1

        total_threads = 0
        for thread_ts in sorted(channel_thread_ts):
            if _upsert_slack_thread_artifact(
                connection=connection,
                channel_id=selection.channel_id,
                channel_name=selection.channel_name,
                thread_ts=thread_ts,
            ):
                total_threads += 1

        cursor_payload["latest_seen"] = latest_seen
        cursor_payload["pending_replies"] = pending_replies
        cursor_payload["run_messages_synced"] = int(cursor_payload.get("run_messages_synced") or 0) + channel_message_count
        cursor_payload["run_threads_touched"] = int(cursor_payload.get("run_threads_touched") or 0) + total_threads
        if history_cursor:
            cursor_payload["history_cursor"] = history_cursor
        else:
            cursor_payload.pop("history_cursor", None)

        selection_has_more = bool(pending_replies or history_cursor)
        if not selection_has_more:
            cursor_payload["run_backfill_complete"] = True
            cursor_payload.pop("pending_replies", None)
            cursor_payload.pop("history_cursor", None)
            if latest_seen:
                cursor_payload["oldest"] = latest_seen
            cursor_payload["last_synced_at"] = synced_at.isoformat()
            selection.last_synced_at = synced_at

        selection.sync_cursor = cursor_payload
        selection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])

        has_more = selection_has_more or any(
            _slack_selection_needs_work(item, run_id=selected_run_id)
            for item in selections
            if item.pk != selection.pk
        )

        if has_more:
            connection.status = ExternalServiceConnectionStatus.SYNCING
        else:
            connection.status = ExternalServiceConnectionStatus.CONNECTED
            connection.last_synced_at = synced_at
        connection.last_error = ""
        connection.sync_cursor = {
            **dict(connection.sync_cursor or {}),
            "last_synced_at": synced_at.isoformat(),
            "latest_seen_by_channel": {
                **dict((connection.sync_cursor or {}).get("latest_seen_by_channel") or {}),
                selection.channel_id: latest_seen,
            },
        }
        connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])

        return {
            "connectionId": connection.id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "status": "syncing" if has_more else "synced",
            "lastSyncedAt": synced_at.isoformat(),
            "last_synced_at": synced_at.isoformat(),
            "messagesSynced": channel_message_count,
            "messages_synced": channel_message_count,
            "threadsTouched": total_threads,
            "threads_touched": total_threads,
            "channels": [
                {
                    "channelId": selection.channel_id,
                    "channel_id": selection.channel_id,
                    "channelName": selection.channel_name,
                    "channel_name": selection.channel_name,
                    "messagesSynced": channel_message_count,
                    "messages_synced": channel_message_count,
                    "threadsTouched": total_threads,
                    "threads_touched": total_threads,
                }
            ],
            "has_more": has_more,
        }
    except ConnectorRateLimitError:
        raise
    except (ConnectorOAuthError, ConnectorRateLimitError, requests.RequestException) as exc:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = str(exc) or "Slack sync failed."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise


def sync_slack_connection(
    connection: ExternalServiceConnection,
    *,
    channel_ids: Optional[Iterable[str]] = None,
    oldest: Optional[Any] = None,
    latest: Optional[Any] = None,
) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.SLACK:
        raise ConnectorConfigurationError("Connection is not a Slack connection.")
    if not connection.organization:
        raise ConnectorConfigurationError("Slack connection is not linked to an organization.")
    if not connection.access_token:
        raise ConnectorOAuthError("Slack connection needs to be reauthorised.")

    selected_qs = _selected_slack_channels(connection)
    if channel_ids:
        selected_set = {str(item or "").strip() for item in channel_ids if str(item or "").strip()}
        selected_qs = selected_qs.filter(channel_id__in=selected_set)
    selections = list(selected_qs)
    if not selections:
        raise ConnectorConfigurationError("Select at least one Slack channel before syncing.")

    history_limit = min(max(int(getattr(settings, "SLACK_SYNC_HISTORY_PAGE_LIMIT", 100) or 100), 1), 1000)
    max_history_pages = max(1, int(getattr(settings, "SLACK_SYNC_HISTORY_MAX_PAGES", 5) or 5))
    max_reply_pages = max(1, int(getattr(settings, "SLACK_SYNC_REPLY_MAX_PAGES", 3) or 3))
    default_oldest_dt = timezone.now() - timedelta(days=int(getattr(settings, "SLACK_SYNC_HISTORY_DAYS", 120) or 120))
    default_oldest = str(default_oldest_dt.timestamp())
    explicit_oldest = str(oldest.timestamp()) if hasattr(oldest, "timestamp") else (str(oldest) if oldest else None)
    explicit_latest = str(latest.timestamp()) if hasattr(latest, "timestamp") else (str(latest) if latest else None)

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    synced_at = timezone.now()
    total_messages = 0
    total_threads = 0
    channel_results = []
    latest_seen_by_channel: dict[str, str] = {}
    try:
        for selection in selections:
            channel_thread_ts: set[str] = set()
            channel_message_count = 0
            cursor_payload = dict(selection.sync_cursor or {})
            oldest_ts = explicit_oldest or str(cursor_payload.get("oldest") or default_oldest)
            latest_seen = str(cursor_payload.get("oldest") or "")
            page_cursor = None
            page_count = 0
            while page_count < max_history_pages:
                params = {
                    "channel": selection.channel_id,
                    "limit": history_limit,
                    "oldest": oldest_ts,
                    "inclusive": "false",
                }
                if explicit_latest:
                    params["latest"] = explicit_latest
                if page_cursor:
                    params["cursor"] = page_cursor
                payload = _slack_api_request(connection, "conversations.history", params=params)
                messages = [item for item in payload.get("messages", []) if isinstance(item, dict)]
                for message in messages:
                    artifact = _upsert_slack_message(
                        connection=connection,
                        channel_id=selection.channel_id,
                        channel_name=selection.channel_name,
                        message=message,
                    )
                    if artifact is None:
                        continue
                    channel_message_count += 1
                    channel_thread_ts.add(artifact.thread_ts or artifact.slack_message_ts)
                    if str(artifact.slack_message_ts) > latest_seen:
                        latest_seen = artifact.slack_message_ts
                    if int(message.get("reply_count") or 0) <= 0:
                        continue
                    reply_cursor = None
                    reply_pages = 0
                    while reply_pages < max_reply_pages:
                        reply_params = {
                            "channel": selection.channel_id,
                            "ts": str(message.get("thread_ts") or message.get("ts")),
                            "limit": history_limit,
                        }
                        if reply_cursor:
                            reply_params["cursor"] = reply_cursor
                        reply_payload = _slack_api_request(connection, "conversations.replies", params=reply_params)
                        replies = [item for item in reply_payload.get("messages", []) if isinstance(item, dict)]
                        for reply in replies:
                            reply_artifact = _upsert_slack_message(
                                connection=connection,
                                channel_id=selection.channel_id,
                                channel_name=selection.channel_name,
                                message=reply,
                            )
                            if reply_artifact is None:
                                continue
                            channel_message_count += 1
                            channel_thread_ts.add(reply_artifact.thread_ts or reply_artifact.slack_message_ts)
                            if str(reply_artifact.slack_message_ts) > latest_seen:
                                latest_seen = reply_artifact.slack_message_ts
                        reply_metadata = reply_payload.get("response_metadata") if isinstance(reply_payload.get("response_metadata"), dict) else {}
                        reply_cursor = str(reply_metadata.get("next_cursor") or "").strip()
                        reply_pages += 1
                        if not reply_cursor:
                            break

                response_metadata = payload.get("response_metadata") if isinstance(payload.get("response_metadata"), dict) else {}
                page_cursor = str(response_metadata.get("next_cursor") or "").strip()
                page_count += 1
                if not page_cursor:
                    break

            for thread_ts in sorted(channel_thread_ts):
                if _upsert_slack_thread_artifact(
                    connection=connection,
                    channel_id=selection.channel_id,
                    channel_name=selection.channel_name,
                    thread_ts=thread_ts,
                ):
                    total_threads += 1
            total_messages += channel_message_count
            if latest_seen:
                latest_seen_by_channel[selection.channel_id] = latest_seen
                selection.sync_cursor = {**cursor_payload, "oldest": latest_seen, "last_synced_at": synced_at.isoformat()}
            else:
                selection.sync_cursor = {**cursor_payload, "last_synced_at": synced_at.isoformat()}
            selection.last_synced_at = synced_at
            selection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
            channel_results.append(
                {
                    "channelId": selection.channel_id,
                    "channel_id": selection.channel_id,
                    "channelName": selection.channel_name,
                    "channel_name": selection.channel_name,
                    "messagesSynced": channel_message_count,
                    "messages_synced": channel_message_count,
                    "threadsTouched": len(channel_thread_ts),
                    "threads_touched": len(channel_thread_ts),
                }
            )
    except (ConnectorOAuthError, requests.RequestException) as exc:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = str(exc) or "Slack sync failed."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise

    connection.status = ExternalServiceConnectionStatus.CONNECTED
    connection.last_error = ""
    connection.last_synced_at = synced_at
    connection.sync_cursor = {
        **dict(connection.sync_cursor or {}),
        "last_synced_at": synced_at.isoformat(),
        "latest_seen_by_channel": latest_seen_by_channel,
        "messages_synced": total_messages,
        "threads_touched": total_threads,
    }
    connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])

    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
        "messagesSynced": total_messages,
        "messages_synced": total_messages,
        "threadsTouched": total_threads,
        "threads_touched": total_threads,
        "channels": channel_results,
    }


def serialize_slack_preview(user, *, limit: int = 5) -> dict[str, Any]:
    connection = _latest_slack_connection(user)
    if not connection:
        return {
            "accountLabel": None,
            "account_label": None,
            "teamId": None,
            "team_id": None,
            "lastSyncedAt": None,
            "last_synced_at": None,
            "selectedChannels": [],
            "selected_channels": [],
            "totalCachedMessages": 0,
            "total_cached_messages": 0,
            "warnings": ["Slack is not connected."],
            "messages": [],
        }
    selected = list(_selected_slack_channels(connection))
    warnings: list[str] = []
    if not selected:
        warnings.append("Select at least one Slack channel before syncing.")
    if connection.status == ExternalServiceConnectionStatus.ERROR and connection.last_error:
        warnings.append(connection.last_error)

    queryset = SlackMessageArtifact.objects.filter(
        connection=connection,
        organization=connection.organization,
    )
    if selected:
        queryset = queryset.filter(channel_id__in=[selection.channel_id for selection in selected])
    total_cached = queryset.count()
    limit = min(max(int(limit or 5), 1), 20)
    messages = [
        {
            "channelId": artifact.channel_id,
            "channel_id": artifact.channel_id,
            "channelName": artifact.channel_name,
            "channel_name": artifact.channel_name,
            "messageTs": artifact.slack_message_ts,
            "message_ts": artifact.slack_message_ts,
            "threadTs": artifact.thread_ts,
            "thread_ts": artifact.thread_ts,
            "authorLabel": artifact.author_name or artifact.author_id,
            "author_label": artifact.author_name or artifact.author_id,
            "postedAt": artifact.posted_at.isoformat() if artifact.posted_at else None,
            "posted_at": artifact.posted_at.isoformat() if artifact.posted_at else None,
            "text": artifact.cleaned_text or artifact.text,
            "relevanceLabel": "selected_channel",
            "relevance_label": "selected_channel",
        }
        for artifact in queryset.order_by("-posted_at", "-id")[:limit]
    ]
    return {
        "accountLabel": connection.account_label or connection.external_account_id,
        "account_label": connection.account_label or connection.external_account_id,
        "teamId": connection.external_account_id,
        "team_id": connection.external_account_id,
        "lastSyncedAt": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "last_synced_at": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "selectedChannels": [_serialize_slack_channel_selection(selection) for selection in selected],
        "selected_channels": [_serialize_slack_channel_selection(selection) for selection in selected],
        "totalCachedMessages": total_cached,
        "total_cached_messages": total_cached,
        "warnings": warnings,
        "messages": messages,
    }


def _serialize_google_source(user) -> dict[str, Any]:
    connection = GoogleConnection.objects.filter(user=user).first()
    configured = is_provider_configured("gmail")
    if connection:
        status_value = "connected"
        warning = None
    elif configured:
        status_value = "not_connected"
        warning = None
    else:
        status_value = "unavailable"
        warning = "Google OAuth is not configured."

    return {
        "key": "gmail",
        "provider": "gmail",
        "label": CONNECTOR_DEFINITIONS["gmail"].label,
        "capabilities": list(CONNECTOR_DEFINITIONS["gmail"].capabilities),
        "selected": status_value == "connected",
        "status": status_value,
        "connectionId": connection.id if connection else None,
        "connection_id": connection.id if connection else None,
        "accountLabel": connection.google_email if connection else None,
        "account_label": connection.google_email if connection else None,
        "lastSyncedAt": None,
        "last_synced_at": None,
        "warning": warning,
        "canDisconnect": bool(connection),
        "can_disconnect": bool(connection),
        "canDeleteData": bool(connection),
        "can_delete_data": bool(connection),
        "googlePermissionsUrl": "https://myaccount.google.com/permissions",
        "google_permissions_url": "https://myaccount.google.com/permissions",
    }


def _serialize_external_source(user, provider: str) -> dict[str, Any]:
    definition = CONNECTOR_DEFINITIONS[provider]
    connection = (
        ExternalServiceConnection.objects.filter(user=user, provider=provider)
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    configuration_error = _provider_configuration_error(provider)
    configured = configuration_error is None
    if connection:
        status_value = connection.status
        warning = connection.last_error or None
    elif configured:
        status_value = "not_connected"
        warning = None
    else:
        status_value = "unavailable"
        warning = configuration_error
    selected_channel_count = 0
    if provider == ExternalServiceProvider.SLACK and connection:
        selected_channel_count = SlackChannelSelection.objects.filter(
            connection=connection,
            selected=True,
        ).count()
        if status_value in {"connected", "syncing"} and selected_channel_count == 0:
            warning = warning or "Select Slack channels before using Slack in a monthly update."
    selected_project_count = 0
    if provider == ExternalServiceProvider.LINEAR and connection:
        selected_project_count = LinearProjectSelection.objects.filter(
            connection=connection,
            selected=True,
        ).count()
        if status_value in {"connected", "syncing"} and selected_project_count == 0:
            warning = warning or "Select Linear projects before using Linear in a monthly update."

    payload = {
        "key": provider,
        "provider": provider,
        "label": definition.label,
        "capabilities": list(definition.capabilities),
        "selected": (
            selected_channel_count > 0
            if provider == ExternalServiceProvider.SLACK
            else selected_project_count > 0
            if provider == ExternalServiceProvider.LINEAR
            else status_value in {"connected", "syncing"}
        ),
        "status": status_value,
        "connectionId": connection.id if connection else None,
        "connection_id": connection.id if connection else None,
        "accountLabel": connection.account_label if connection else None,
        "account_label": connection.account_label if connection else None,
        "externalAccountId": connection.external_account_id if connection else None,
        "external_account_id": connection.external_account_id if connection else None,
        "lastSyncedAt": connection.last_synced_at.isoformat() if connection and connection.last_synced_at else None,
        "last_synced_at": connection.last_synced_at.isoformat() if connection and connection.last_synced_at else None,
        "warning": warning,
        "configured": configured,
        "selectedChannelCount": selected_channel_count,
        "selected_channel_count": selected_channel_count,
        "selectedProjectCount": selected_project_count,
        "selected_project_count": selected_project_count,
    }
    if provider == ExternalServiceProvider.XERO:
        has_report_scope = xero_has_report_scope(connection.scopes if connection else [])
        can_request_report_scopes = _xero_can_request_report_scopes()
        needs_report_scope_configuration = bool(connection) and not has_report_scope and not can_request_report_scopes
        needs_report_reconnect = (
            bool(connection)
            and not has_report_scope
            and can_request_report_scopes
            and xero_needs_report_reconnect(connection.scopes)
        )
        if status_value in {"connected", "syncing"} and needs_report_reconnect:
            payload["warning"] = payload.get("warning") or XERO_REPORT_SCOPE_WARNING
        elif status_value in {"connected", "syncing"} and needs_report_scope_configuration:
            payload["warning"] = payload.get("warning") or XERO_REPORT_SCOPE_CONFIGURATION_WARNING
        payload.update(
            {
                "hasReportScope": has_report_scope,
                "has_report_scope": has_report_scope,
                "needsReportReconnect": needs_report_reconnect,
                "needs_report_reconnect": needs_report_reconnect,
                "canRequestReportScopes": can_request_report_scopes,
                "can_request_report_scopes": can_request_report_scopes,
                "needsReportScopeConfiguration": needs_report_scope_configuration,
                "needs_report_scope_configuration": needs_report_scope_configuration,
                "requiredReportScopes": list(XERO_REQUIRED_REPORT_SCOPES),
                "required_report_scopes": list(XERO_REQUIRED_REPORT_SCOPES),
            }
        )
    return payload


def serialize_source_status(user, *, financial_only: bool = False) -> dict[str, Any]:
    sources = []
    if not financial_only:
        sources.append(_serialize_google_source(user))

    for provider in EXTERNAL_PROVIDER_ORDER:
        definition = CONNECTOR_DEFINITIONS[provider]
        if financial_only and not definition.financial:
            continue
        sources.append(_serialize_external_source(user, provider))

    return {
        "sources": sources,
        "connections": sources,
        "financeUnavailable": False,
        "finance_unavailable": False,
    }


def mark_sources_sync_requested(user, providers: Optional[list[str]] = None, *, financial_only: bool = False) -> dict[str, Any]:
    provider_filter = []
    if providers:
        normalized_providers = [normalize_provider(provider) for provider in providers]
        provider_filter = [provider for provider in normalized_providers if provider != "gmail"]
        if not provider_filter:
            return {"status": "no_connected_sources", "syncRuns": [], "sync_runs": []}
    elif financial_only:
        provider_filter = [
            ExternalServiceProvider.STRIPE,
            ExternalServiceProvider.XERO,
            ExternalServiceProvider.BANK_FEED,
        ]

    queryset = ExternalServiceConnection.objects.filter(user=user).exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
    if provider_filter:
        queryset = queryset.filter(provider__in=provider_filter)

    now = timezone.now()
    updated = []
    for connection in queryset:
        if connection.provider == ExternalServiceProvider.XERO:
            try:
                updated.append(sync_xero_connection(connection))
            except (ConnectorOAuthError, requests.RequestException) as exc:
                logger.exception(
                    "Xero sync failed",
                    extra={"connection_id": connection.id, "user_id": user.id},
                )
                connection.status = ExternalServiceConnectionStatus.ERROR
                connection.last_error = str(exc) or "Xero sync failed."
                connection.save(update_fields=["status", "last_error", "updated_at"])
                updated.append(
                    {
                        "connectionId": connection.id,
                        "connection_id": connection.id,
                        "provider": connection.provider,
                        "status": "error",
                        "error": connection.last_error,
                    }
                )
            continue

        if connection.provider == ExternalServiceProvider.BANK_FEED:
            try:
                updated.append(sync_basiq_connection(connection))
            except (ConnectorOAuthError, requests.RequestException) as exc:
                logger.exception(
                    "Bank Feed sync failed",
                    extra={"connection_id": connection.id, "user_id": user.id},
                )
                connection.status = ExternalServiceConnectionStatus.ERROR
                connection.last_error = str(exc) or "Bank Feed sync failed."
                connection.save(update_fields=["status", "last_error", "updated_at"])
                updated.append(
                    {
                        "connectionId": connection.id,
                        "connection_id": connection.id,
                        "provider": connection.provider,
                        "status": "error",
                        "error": connection.last_error,
                    }
                )
            continue

        if connection.provider == ExternalServiceProvider.SLACK:
            try:
                updated.append(sync_slack_connection(connection))
            except (ConnectorConfigurationError, ConnectorOAuthError, ConnectorRateLimitError, requests.RequestException) as exc:
                logger.exception(
                    "Slack sync failed",
                    extra={"connection_id": connection.id, "user_id": user.id},
                )
                connection.status = ExternalServiceConnectionStatus.ERROR
                connection.last_error = str(exc) or "Slack sync failed."
                connection.save(update_fields=["status", "last_error", "updated_at"])
                updated.append(
                    {
                        "connectionId": connection.id,
                        "connection_id": connection.id,
                        "provider": connection.provider,
                        "status": "error",
                        "error": connection.last_error,
                    }
                )
            continue

        if connection.provider == ExternalServiceProvider.LINEAR:
            try:
                updated.append(sync_linear_connection(connection))
            except (ConnectorConfigurationError, ConnectorOAuthError, ConnectorRateLimitError, requests.RequestException) as exc:
                logger.exception(
                    "Linear sync failed",
                    extra={"connection_id": connection.id, "user_id": user.id},
                )
                connection.status = ExternalServiceConnectionStatus.ERROR
                connection.last_error = str(exc) or "Linear sync failed."
                connection.save(update_fields=["status", "last_error", "updated_at"])
                updated.append(
                    {
                        "connectionId": connection.id,
                        "connection_id": connection.id,
                        "provider": connection.provider,
                        "status": "error",
                        "error": connection.last_error,
                    }
                )
            continue

        connection.last_synced_at = now
        connection.last_error = ""
        if connection.status == ExternalServiceConnectionStatus.ERROR:
            connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.save(update_fields=["last_synced_at", "last_error", "status", "updated_at"])
        updated.append(
            {
                "connectionId": connection.id,
                "connection_id": connection.id,
                "provider": connection.provider,
                "status": "queued",
                "lastSyncedAt": connection.last_synced_at.isoformat(),
                "last_synced_at": connection.last_synced_at.isoformat(),
            }
        )

    if not updated:
        status_value = "no_connected_sources"
    elif any(item.get("status") == "error" for item in updated):
        status_value = "error"
    elif all(item.get("status") == "synced" for item in updated):
        status_value = "synced"
    else:
        status_value = "queued"

    return {
        "status": status_value,
        "syncRuns": updated,
        "sync_runs": updated,
    }


def disconnect_external_connection(user, connection_id: int) -> bool:
    connection = ExternalServiceConnection.objects.filter(user=user, id=connection_id).first()
    if not connection:
        return False
    connection.status = ExternalServiceConnectionStatus.DISCONNECTED
    connection.access_token = ""
    connection.refresh_token = ""
    connection.last_error = ""
    connection.provider_metadata = {}
    connection.sync_cursor = {}
    connection.save(update_fields=["status", "access_token", "refresh_token", "last_error", "provider_metadata", "sync_cursor", "updated_at"])
    return True
