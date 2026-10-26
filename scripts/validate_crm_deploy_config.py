#!/usr/bin/env python3
"""Validate the CRM's release switches and delivery settings before production mutation.

Every setting is optional: an empty value keeps whatever the host's ``.env``
already holds, so a release never switches the CRM off by omission.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit


FLAG_KEYS = (
    "COMMUNITY_CHAT_CRM_ENABLED",
    "CRM_LUMA_SYNC_ENABLED",
    "CRM_SENDING_ENABLED",
    "CRM_SMS_ENABLED",
    "CRM_WHATSAPP_ENABLED",
    "CRM_WARMUP_ENABLED",
)
LISTMONK_BASE_URL = "LISTMONK_BASE_URL"
LISTMONK_API_USER = "LISTMONK_API_USER"
MESSAGING_SERVICE = "TWILIO_MESSAGING_SERVICE_SID"
KEYS = (*FLAG_KEYS, LISTMONK_BASE_URL, LISTMONK_API_USER, MESSAGING_SERVICE,
        "CRM_RESEND_MONITORED_SENDERS", "CRM_WARMUP_RUN_ID")


def validate_single(key: str, value: str, *, optional: bool = False) -> None:
    """Reject malformed single-line configuration without echoing its value."""
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in FLAG_KEYS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key == LISTMONK_BASE_URL:
        # listmonk's origin on the private mlai-shared network, e.g. http://listmonk:9000.
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{key} must be an HTTP origin") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            raise ValueError(f"{key} must be an HTTP origin")
    elif key == LISTMONK_API_USER:
        if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", value):
            raise ValueError(f"{key} must be a listmonk API user name")
    elif key == "CRM_RESEND_MONITORED_SENDERS":
        if not all(re.fullmatch(r"[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}", item) for item in value.split(",")):
            raise ValueError(f"{key} must be comma-separated email addresses")
    elif key == "CRM_WARMUP_RUN_ID":
        if not re.fullmatch(r"[a-z0-9-]{1,48}", value):
            raise ValueError(f"{key} must be a lowercase run identifier")
    elif key == MESSAGING_SERVICE:
        if not re.fullmatch(r"MG[0-9a-f]{32}", value):
            raise ValueError(f"{key} must be a Twilio Messaging Service SID")
    else:
        raise ValueError("Unsupported CRM configuration key")


def validate(environment: dict[str, str]) -> None:
    """Validate every CRM setting a release may carry; each may be left empty."""
    for key in KEYS:
        validate_single(key, environment.get(key, ""), optional=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif len(sys.argv) == 1:
            validate(dict(os.environ))
        else:
            raise ValueError("Use no arguments, or --stdin with a configuration key")
    except ValueError as error:
        raise SystemExit(str(error)) from error
