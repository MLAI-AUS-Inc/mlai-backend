#!/usr/bin/env python3
"""Validate the CRM's release switches and delivery settings before production mutation.

Every setting is optional: an empty value keeps whatever the host's ``.env``
already holds, so a release never switches the CRM off by omission.
"""

from __future__ import annotations

import os
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crm.deliverability.policy import DEFAULTS as DELIVERY_DEFAULTS, environment_settings

FLAG_KEYS = (
    "COMMUNITY_CHAT_CRM_ENABLED",
    "CRM_LUMA_SYNC_ENABLED",
    "CRM_SENDING_ENABLED",
    "CRM_SMS_ENABLED",
    "CRM_WHATSAPP_ENABLED",
    "CRM_WARMUP_ENABLED",
    "CRM_ENGAGEMENT_REFRESH_ENABLED",
    "CRM_SUBSTACK_ACTIVITY_ENABLED",
    "CRM_GUARDIAN_ENABLED",
    "CRM_GUARDIAN_DRY_RUN",
    "CRM_WAVES_ENABLED",
    "CRM_PREFLIGHT_ENABLED",
    "CRM_MONITORS_ENABLED",
    "CRM_LIFECYCLE_ENABLED",
    "CRM_SEED_HEARTBEAT_ENABLED",
    "CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED",
)
LISTMONK_BASE_URL = "LISTMONK_BASE_URL"
LISTMONK_API_USER = "LISTMONK_API_USER"
MESSAGING_SERVICE = "TWILIO_MESSAGING_SERVICE_SID"
KEYS = (*FLAG_KEYS, LISTMONK_BASE_URL, LISTMONK_API_USER, MESSAGING_SERVICE,
        "CRM_RESEND_MONITORED_SENDERS", "CRM_WARMUP_RUN_ID", "CRM_ENGAGEMENT_REFRESH_HOUR",
        "CRM_SENDING_DOMAINS", *(key for key in DELIVERY_DEFAULTS if key not in FLAG_KEYS))


def validate_single(key: str, value: str, *, optional: bool = False) -> None:
    """Reject malformed single-line configuration without echoing its value."""
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in FLAG_KEYS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key in DELIVERY_DEFAULTS:
        try:
            environment_settings({key: value})
            if key in ("RSPAMD_URL", "CRM_LISTMONK_PUBLIC_URL"):
                parsed = urlsplit(value)
                parsed.port
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
                    raise ValueError
            if key == "CRM_SEED_MAILBOXES":
                entries = json.loads(value)
                fields = {"provider", "address", "imap_host", "imap_port", "auth", "secret_env", "enabled"}
                if not isinstance(entries, list):
                    raise ValueError
                for entry in entries:
                    if (not isinstance(entry, dict) or set(entry) - fields
                            or entry.get("provider") not in ("gmail", "google_workspace", "outlook", "microsoft365", "yahoo", "icloud", "other")
                            or entry.get("auth") not in ("app_password", "xoauth2")
                            or not re.fullmatch(r"CRM_SEED_[A-Z0-9_]+", entry.get("secret_env", ""))
                            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", entry.get("address", ""))
                            or not re.fullmatch(r"[a-zA-Z0-9.-]+", entry.get("imap_host", ""))
                            or type(entry.get("imap_port", 993)) is not int or not 1 <= entry.get("imap_port", 993) <= 65535
                            or type(entry.get("enabled", False)) is not bool):
                        raise ValueError
            if key == "CRM_DELIVERABILITY_OWNERS" and not all(
                re.fullmatch(r"[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}", item) for item in value.split(",")
            ):
                raise ValueError
            if key == "CRM_PROTECTED_DOMAINS" and not all(
                re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", item) for item in value.split(",")
            ):
                raise ValueError
            if key == "CRM_STREAMS":
                for entry in value.split(";"):
                    parts = entry.split(":")
                    if (len(parts) != 4 or not re.fullmatch(r"[a-z0-9_-]{1,40}", parts[0])
                            or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", parts[1])
                            or not re.fullmatch(r"[a-z0-9._+-]+@" + re.escape(parts[1]), parts[2])
                            or parts[3] not in ("marketing", "event_update", "marketing|event_update", "transactional")
                            or (parts[1] == "mlai.au" and parts[3] != "transactional")):
                        raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"{key} is not a valid delivery policy value") from None
    elif key == "CRM_SENDING_DOMAINS":
        if not all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}:(resend|google_relay)", item) for item in value.split(",")):
            raise ValueError(f"{key} must be comma-separated domain:route pairs")
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
    elif key == "CRM_ENGAGEMENT_REFRESH_HOUR":
        if not re.fullmatch(r"(?:[0-9]|1[0-9]|2[0-3])", value):
            raise ValueError(f"{key} must be an integer from 0 through 23")
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
