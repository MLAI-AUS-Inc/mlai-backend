"""Send one rendered message through Resend's ``POST /emails`` API.

Uses the transactional key ``SYSTEM_EMAIL_RESEND_API_KEY`` only. It never
falls back to the CRM or legacy ``RESEND_API_KEY``, so marketing and system
mail can't share a key by accident.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from django.conf import settings

from integrations import http_client

from .render import RenderedEmail

logger = logging.getLogger(__name__)
RESEND_EMAILS_URL = "https://api.resend.com/emails"
TIMEOUT = (3, 8)


class SystemEmailError(Exception):
    """Base error; ``code`` is safe to log and store (no address, link or code)."""

    retryable = False

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SystemEmailRetryable(SystemEmailError):
    """A timeout, rate limit or provider outage: retrying with the same key is safe."""

    retryable = True


class SystemEmailRejected(SystemEmailError):
    """The provider refused the message, or it isn't configured: don't retry."""


def api_key() -> str:
    return str(getattr(settings, "SYSTEM_EMAIL_RESEND_API_KEY", "") or "").strip()


def from_address() -> str:
    return str(getattr(settings, "MLAI_SYSTEM_FROM_EMAIL", "") or "").strip()


def build_payload(
    *,
    to: str,
    rendered: RenderedEmail,
    message_key: str,
    entity_ref: str,
    unsubscribe: bool = False,
    unsubscribe_url: str = "",
) -> dict[str, Any]:
    """The whole Resend request body for one message.

    Optional mail (``unsubscribe=True``) gets ``List-Unsubscribe`` with the
    caller's one-click URL and/or the configured mailto. One-click POST is only
    advertised when there is a URL to POST to.
    """
    headers = {"X-Entity-Ref-ID": entity_ref}
    if unsubscribe:
        mailto = str(getattr(settings, "SYSTEM_EMAIL_UNSUBSCRIBE_MAILTO", "") or "").strip()
        values = [f"<{unsubscribe_url}>"] if unsubscribe_url else []
        if mailto:
            values.append(f"<mailto:{mailto}?subject={quote('unsubscribe ' + message_key)}>")
        if values:
            headers["List-Unsubscribe"] = ", ".join(values)
        if unsubscribe_url:
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    payload: dict[str, Any] = {
        "from": from_address(),
        "to": [to],
        "subject": rendered.subject,
        "text": rendered.text,
        "headers": headers,
        "tags": [{"name": "message", "value": message_key}],
    }
    if rendered.html:
        payload["html"] = rendered.html
    reply_to = str(getattr(settings, "SYSTEM_EMAIL_REPLY_TO", "") or "").strip()
    if reply_to:
        payload["reply_to"] = reply_to
    return payload


def send(payload: dict[str, Any], *, idempotency_key: str) -> str:
    """POST ``payload`` and return Resend's email id.

    Raises ``SystemEmailRetryable`` for 408, 429, 5xx, timeouts and concurrent
    idempotent requests, and ``SystemEmailRejected`` for everything else.
    """
    key = api_key()
    if not key or not payload.get("from"):
        raise SystemEmailRejected("system_email_unconfigured")
    if not idempotency_key or len(idempotency_key) > 256:
        raise SystemEmailRejected("invalid_idempotency_key")
    try:
        response = http_client.post(
            RESEND_EMAILS_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            timeout=TIMEOUT,
        )
    except http_client.RequestException as exc:
        raise SystemEmailRetryable(f"request_{exc.__class__.__name__}"[:120]) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    status = response.status_code
    name = str(body.get("name") or "") if isinstance(body, dict) else ""
    if status in (408, 429) or status >= 500 or (status == 409 and name == "concurrent_idempotent_requests"):
        raise SystemEmailRetryable(f"resend_{status}")
    if status not in (200, 201, 202) or not isinstance(body, dict) or not body.get("id"):
        # ``name`` is Resend's error class (e.g. validation_error); never the message text,
        # which can echo the recipient address.
        raise SystemEmailRejected(f"resend_{status}{('_' + name) if name else ''}"[:120])
    return str(body["id"])[:255]
