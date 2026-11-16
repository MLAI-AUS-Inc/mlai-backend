"""Send a founder back to MLAI Chat when a provider connection does not finish.

A Chat-started connection never ends on a plain-text page on the API host.
Every refusal or failure redirects to the Chat return URL chosen when the
connection started, with a reason code the app turns into plain words.
Connections started outside Chat keep their existing responses.

The native return (``mlaichat://connections?company_id=…&provider=…``) has a
fixed grammar that installed apps validate exactly, so it is returned
unchanged: the app refreshes the source status and finds it not connected.
"""
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core import signing

from integrations.services.chat_oauth_return import (
    native_chat_connection_return_url,
    native_chat_startup_return_url,
    oauth_return_redirect,
)

logger = logging.getLogger(__name__)

# Reason codes in the web return URL's ``error`` parameter.
CANCELLED = "cancelled"  # The founder pressed Cancel or refused at the provider.
EXPIRED = "expired"  # The connection link expired, was already used, or the Chat session ended.
UNAVAILABLE = "unavailable"  # MLAI cannot offer this connection right now (configuration).
DOMAIN_CLAIMED = "domain_claimed"  # The startup's website belongs to another MLAI account.
FAILED = "failed"  # Anything else: the provider or MLAI could not finish the connection.
REASONS = frozenset({CANCELLED, EXPIRED, UNAVAILABLE, DOMAIN_CLAIMED, FAILED})

# OAuth ``error`` values that mean the founder chose not to continue.
_CANCELLED_PROVIDER_ERRORS = frozenset({
    "access_denied", "user_cancelled", "user_canceled", "user_denied",
    "consent_required", "cancelled", "canceled", "interaction_required",
})


def provider_error_reason(error) -> str:
    """The reason code for a provider's ``?error=`` value."""
    return CANCELLED if str(error or "").strip().lower() in _CANCELLED_PROVIDER_ERRORS else FAILED


def chat_state_payload(state):
    """A Chat-started connector state's signed data, read without consuming it.

    The age is deliberately not checked: only the return URL is used, and it
    is revalidated before use. Non-Chat states return None.
    """
    from integrations.services.external_connectors import CONNECTOR_OAUTH_STATE_SIGNING_SALT
    try:
        payload = signing.loads(str(state or ""), salt=CONNECTOR_OAUTH_STATE_SIGNING_SALT)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("chat_company_id"):
        return None
    return payload


def return_url_with_reason(next_url, reason) -> str:
    """The validated Chat return URL, carrying ``error=<reason>`` on the web."""
    from integrations.services.external_connectors import normalize_connector_next
    if reason not in REASONS:
        reason = FAILED
    target = normalize_connector_next(next_url)
    if native_chat_connection_return_url(target) or native_chat_startup_return_url(target):
        return target
    parsed = urlsplit(target)
    if not parsed.path.startswith("/my-startup/"):
        return target
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "error"]
    query.append(("error", reason))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def chat_error_redirect(next_url, reason, *, provider, exc=None):
    """Redirect to Chat with a reason; log the internal detail, never show it."""
    logger.warning(
        "Chat connection did not finish",
        extra={"provider": provider, "reason": reason, "error_type": type(exc).__name__ if exc else ""},
    )
    return oauth_return_redirect(return_url_with_reason(next_url, reason))


def reason_for_exception(exc, *, at_start=False) -> str:
    """Map a connector exception to a reason code."""
    from integrations.services.external_connectors import (
        CONNECT_DOMAIN_CLAIMED_MESSAGE,
        ConnectorConfigurationError,
        ConnectorStateError,
    )
    if isinstance(exc, ConnectorStateError):
        return EXPIRED
    if str(exc) == CONNECT_DOMAIN_CLAIMED_MESSAGE:
        return DOMAIN_CLAIMED
    if at_start and isinstance(exc, ConnectorConfigurationError):
        return UNAVAILABLE
    return FAILED
