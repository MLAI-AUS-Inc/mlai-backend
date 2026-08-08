"""Optional Sentry error reporting.

Motivation: prod had no error forensics at all. Container logs are the only
record, they live and die with the container, and a deploy therefore erases the
entire history — an intermittent 500 becomes unreproducible by construction
rather than by bad luck. Sentry keeps the traceback and request context beyond
the life of the container that produced it.

This module is deliberately fail-open and opt-in: with no SENTRY_DSN set,
`init_sentry()` does nothing and the app behaves exactly as before. Nothing here
may ever prevent the process from starting.

Privacy posture: this codebase handles magic links (bearer credentials), JWTs,
and connector OAuth tokens. `send_default_pii` is left OFF, and `before_send`
scrubs credential-bearing query parameters using the SAME list the request
logger uses (core.middleware.SENSITIVE_QUERY_PARAMETERS) so the two cannot
drift apart.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

logger = logging.getLogger(__name__)

# Headers that carry credentials and must never reach the error tracker.
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "x-roo-api-key",
}

_REDACTED = "[REDACTED]"


def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back to `default` on anything malformed."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("sentry_invalid_float_env name=%s value=%r", name, raw)
        return default


def _scrub_query_string(value: str) -> str:
    """Redact credential-bearing parameters from a query string."""
    from core.middleware import SENSITIVE_QUERY_PARAMETERS

    try:
        pairs = parse_qsl(value, keep_blank_values=True)
    except (TypeError, ValueError):
        return _REDACTED
    return urlencode(
        [
            (key, _REDACTED if key.lower() in SENSITIVE_QUERY_PARAMETERS else val)
            for key, val in pairs
        ]
    )


def _scrub_url(value: str) -> str:
    """Redact credentials from a full URL while preserving its shape."""
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError):
        return _REDACTED
    if not parts.query:
        return value
    scrubbed = _scrub_query_string(parts.query)
    base = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
    return f"{base}{parts.path}?{scrubbed}"


def _before_send(event: dict, _hint: dict) -> Optional[dict]:
    """Strip credentials from an event immediately before transmission.

    Defensive by construction: any unexpected event shape must not raise, since
    an exception here would be raised inside Sentry's own send path.
    """
    try:
        request = event.get("request")
        if isinstance(request, dict):
            if isinstance(request.get("query_string"), str):
                request["query_string"] = _scrub_query_string(request["query_string"])
            if isinstance(request.get("url"), str):
                request["url"] = _scrub_url(request["url"])
            headers = request.get("headers")
            if isinstance(headers, dict):
                for name in list(headers):
                    if name.lower() in _SENSITIVE_HEADERS:
                        headers[name] = _REDACTED
            # Request bodies can carry passwords/tokens on auth endpoints and are
            # not needed to diagnose a 500. Drop wholesale rather than guess.
            request.pop("data", None)
            request.pop("cookies", None)
    except Exception:  # pragma: no cover - defensive
        logger.exception("sentry_before_send_scrub_failed")
        # Fail CLOSED: if scrubbing did not complete, drop the event rather than
        # risk shipping unredacted credentials to a third party.
        return None
    return event


def init_sentry(*, environment: str, release: str) -> bool:
    """Initialise Sentry when configured. Returns True if it was enabled.

    Never raises: a misconfigured or missing error tracker must not stop the
    application from booting.
    """
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning("sentry_sdk_not_installed dsn_configured=True")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or None,
            integrations=[
                DjangoIntegration(),
                # Breadcrumbs from INFO (so the request_started/request_complete
                # trail is attached), events only from logger.exception/error.
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            # Default 0.0: tracing is billed per-transaction, so it stays off
            # until deliberately enabled. Error reporting is unaffected.
            traces_sample_rate=_env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0),
            # Never attach usernames/emails/IPs automatically. See module docstring.
            send_default_pii=False,
            before_send=_before_send,
        )
    except Exception:
        logger.exception("sentry_init_failed")
        return False

    logger.info("sentry_initialised environment=%s release=%s", environment, release)
    return True
