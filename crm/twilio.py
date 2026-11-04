"""Twilio calls for SMS and WhatsApp blasts, and the Content template list.

A send is never retried. Twilio's Messages API has no idempotency key, so a
timeout, connection error or 5xx comes back ``unknown`` for a person to
decide rather than risking a second text to the same person.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from django.conf import settings

from integrations import http_client
from integrations.services.notification_adapters import (
    _whatsapp_address,
    post_twilio_message,
    twilio_credentials,
)

SENT = "sent"
FAILED = "failed"
UNKNOWN = "unknown"

CONTENT_API_URL = "https://content.twilio.com/v1/ContentAndApprovals"
CONTENT_API_HOST = "content.twilio.com"
CONTENT_PAGE_SIZE = 100
MAX_CONTENT_PAGES = 50
TEMPLATE_CATEGORIES = ("utility", "marketing", "authentication")
_SLOT = re.compile(r"\{\{\s*(\d+)\s*\}\}")


@dataclass(frozen=True)
class SendResult:
    """What one Messages API call did: ``sent``, ``failed`` or ``unknown``."""

    outcome: str
    sid: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class ContentTemplate:
    """The parts of a Twilio Content item the crm template list stores."""

    sid: str
    name: str
    body: str
    category: str
    approved: bool
    variable_indexes: tuple[int, ...]


class TwilioContentError(RuntimeError):
    """Twilio's Content API could not be read; the message is a short code."""


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def sms_configured() -> bool:
    """Credentials and a Messaging Service are set."""

    account_sid, auth_token = twilio_credentials()
    return bool(account_sid and auth_token and _setting("TWILIO_MESSAGING_SERVICE_SID"))


def whatsapp_configured() -> bool:
    """Credentials and the WhatsApp sender are set."""

    account_sid, auth_token = twilio_credentials()
    return bool(account_sid and auth_token and _setting("TWILIO_WHATSAPP_FROM"))


def send_sms(to: str, body: str, status_callback: str) -> SendResult:
    """Send one SMS through the Messaging Service (Advanced Opt-Out applies)."""

    service_sid = _setting("TWILIO_MESSAGING_SERVICE_SID")
    if not service_sid:
        return SendResult(FAILED, error_code="not_configured")
    payload = {"MessagingServiceSid": service_sid, "To": str(to or "").strip(), "Body": body}
    if status_callback:
        payload["StatusCallback"] = status_callback
    return _send(payload)


def send_whatsapp_template(
    to: str, content_sid: str, variables: Optional[dict[str, Any]], status_callback: str
) -> SendResult:
    """Send one approved WhatsApp Content template from ``TWILIO_WHATSAPP_FROM``."""

    sender = _setting("TWILIO_WHATSAPP_FROM")
    if not sender or not content_sid:
        return SendResult(FAILED, error_code="not_configured")
    payload = {
        "From": _whatsapp_address(sender),
        "To": _whatsapp_address(str(to or "").strip()),
        "ContentSid": content_sid,
    }
    if variables:
        payload["ContentVariables"] = json.dumps(
            {str(key): str(value) for key, value in variables.items()}
        )
    if status_callback:
        payload["StatusCallback"] = status_callback
    return _send(payload)


def _send(payload: dict[str, str]) -> SendResult:
    account_sid, auth_token = twilio_credentials()
    if not account_sid or not auth_token:
        return SendResult(FAILED, error_code="not_configured")
    try:
        response = post_twilio_message(payload, account_sid=account_sid, auth_token=auth_token)
    except http_client.exceptions.Timeout:
        return SendResult(UNKNOWN, error_code="timeout")
    except http_client.RequestException:
        return SendResult(UNKNOWN, error_code="connection_error")
    return classify_response(response.status_code, _json(response))


def classify_response(status_code: int, payload: Any) -> SendResult:
    """2xx with a SID is sent; 4xx failed with Twilio's code; else unknown."""

    payload = payload if isinstance(payload, dict) else {}
    code = str(payload.get("code") or "").strip()[:32]
    if 200 <= status_code < 300:
        sid = str(payload.get("sid") or "").strip()[:64]
        return SendResult(SENT, sid=sid) if sid else SendResult(UNKNOWN, error_code="missing_sid")
    if 400 <= status_code < 500:
        return SendResult(FAILED, error_code=code or f"http_{status_code}")
    return SendResult(UNKNOWN, error_code=code or f"http_{status_code}")


def _json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def list_content_templates() -> list[dict[str, Any]]:
    """Every item from ``ContentAndApprovals``, following ``meta.next_page_url``."""

    account_sid, auth_token = twilio_credentials()
    if not account_sid or not auth_token:
        raise TwilioContentError("not_configured")
    url = f"{CONTENT_API_URL}?PageSize={CONTENT_PAGE_SIZE}"
    contents: list[dict[str, Any]] = []
    for _ in range(MAX_CONTENT_PAGES):
        response = http_client.get(url, auth=(account_sid, auth_token), timeout=(3, 15))
        if response.status_code >= 300:
            raise TwilioContentError(f"http_{response.status_code}")
        payload = _json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("contents"), list):
            raise TwilioContentError("unreadable_response")
        contents.extend(item for item in payload["contents"] if isinstance(item, dict))
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        url = str(meta.get("next_page_url") or "")
        if not url:
            return contents
        parsed = urlparse(url)
        # The next page carries our credentials, so it must stay on Twilio.
        if parsed.scheme != "https" or parsed.hostname != CONTENT_API_HOST:
            raise TwilioContentError("unexpected_next_page_url")
    raise TwilioContentError("too_many_pages")


def parse_content_template(item: dict[str, Any]) -> Optional[ContentTemplate]:
    """Read one Content item; ``None`` when it has no SID."""

    sid = str(item.get("sid") or "").strip()
    if not sid:
        return None
    approval = _whatsapp_approval(item)
    body = _template_body(item.get("types"))
    category = str(approval.get("category") or "").strip().lower()
    indexes = {int(slot) for slot in _SLOT.findall(body)}
    declared = item.get("variables")
    if isinstance(declared, dict):
        indexes.update(int(key) for key in declared if str(key).isdigit())
    name = (
        str(item.get("friendly_name") or "").strip()
        or str(approval.get("name") or "").strip()
        or sid
    )
    return ContentTemplate(
        sid=sid[:64],
        name=name[:200],
        body=body,
        category=category if category in TEMPLATE_CATEGORIES else "",
        approved=str(approval.get("status") or "").strip().lower() == "approved",
        variable_indexes=tuple(sorted(index for index in indexes if index > 0)),
    )


def _whatsapp_approval(item: dict[str, Any]) -> dict[str, Any]:
    approvals = item.get("approval_requests")
    if isinstance(approvals, dict):
        nested = approvals.get("whatsapp")
        return nested if isinstance(nested, dict) else approvals
    if isinstance(approvals, list):
        for approval in approvals:
            if isinstance(approval, dict) and str(approval.get("type") or "whatsapp") == "whatsapp":
                return approval
    return {}


def _template_body(types: Any) -> str:
    """The WhatsApp body if there is one, else ``twilio/text``, else any body."""

    if not isinstance(types, dict):
        return ""
    bodies = [
        (key, str(value.get("body") or ""))
        for key, value in types.items()
        if isinstance(value, dict) and value.get("body")
    ]
    for prefer in (
        lambda key: str(key).startswith("whatsapp/"),
        lambda key: key == "twilio/text",
        lambda key: True,
    ):
        for key, body in bodies:
            if prefer(key):
                return body
    return ""
