"""MLAI calendar metadata only. A guest or host record is never a room roster."""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm.luma_guests import event_record
from integrations.services.luma import LumaAttendeeReportService


def decode_webhook(provider, config, headers, body):
    secret = os.getenv(config.get("webhook_secret_env", "MANAGED_CHANNEL_LUMA_WEBHOOK_SECRET"), "")
    parts = dict(part.partition("=")[::2] for part in headers.get("Webhook-Signature", "").split(","))
    stamp = parts.get("t", "")
    if (not secret or not stamp.isdigit() or abs(time.time() - int(stamp)) > 300
            or not hmac.compare_digest(parts.get("v1", ""), hmac.new(
                secret.encode(), stamp.encode() + b"." + body, hashlib.sha256
            ).hexdigest())):
        raise PermissionError("Invalid Luma signature")
    payload = json.loads(body)
    action = payload.get("type")
    if action not in {"calendar.event.added", "event.created", "event.updated", "event.canceled"}:
        return None
    data = payload.get("data") or {}
    if action.startswith("event.") and data.get("calendar_id") != config["scope"]:
        raise PermissionError("Unexpected Luma calendar")
    event_id = data.get("id")
    delivery = headers.get("Webhook-Id")
    if not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (delivery, event_id)):
        raise ValueError("Invalid Luma webhook identifiers")
    return delivery, event_id, "canceled" if action == "event.canceled" else "refresh"


def event_status(start, end, now):
    if end and now >= end:
        return "ended"
    if start and now < start:
        return "scheduled"
    if start and end:
        return "live"
    return "unknown"


def _instant(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if not isinstance(parsed, datetime) or timezone.is_naive(parsed):
        raise ValueError("Invalid event time")
    return parsed


def snapshot(raw, config, now=None):
    event = event_record(raw)
    if not event or not raw.get("name") or not isinstance(raw.get("id"), str):
        raise ValueError("Invalid Luma calendar listing")
    start, end = _instant(raw.get("start_at")), _instant(raw.get("end_at"))
    if start and end and end < start:
        raise ValueError("Invalid Luma event schedule")
    url = urlsplit(event.url)
    safe_url = event.url if (url.scheme == "https" and url.hostname and not url.username and not url.password
                             and "\\" not in event.url and not any(ord(c) < 32 for c in event.url)) else ""
    public = raw.get("visibility") == "public"
    # External listings have no visibility flag. The configured calendar may
    # explicitly authorize its approved external listings for public discussion.
    if raw.get("platform") == "external":
        public = config.get("public_external_listings") is True
    return {
        "external_id": event.luma_event_id, "platform": raw.get("platform", "luma"),
        "title": event.name, "url": safe_url, "start_at": start.isoformat() if start else "",
        "end_at": end.isoformat() if end else "", "timezone": event.timezone,
        "status": event_status(start, end, now or timezone.now()),
        "visibility": "public" if public else "private",
        "public_authorized": public and config.get("public_event_rooms") is True,
    }


def _service(config):
    key = os.getenv(config.get("api_key_env", "LUMA_API_KEY"), "")
    service = LumaAttendeeReportService(api_key=key, timeout=8)
    if not key or service.get_calendar().get("id") != config["scope"]:
        raise PermissionError("Unverified Luma calendar identity")
    return service


def inventory(provider, config):
    service = _service(config)
    return [snapshot(row, config) for row in service.list_crm_events(strict_pagination=True)]


def get_snapshot(provider, config, external_id, previous=None):
    service = _service(config)
    for row in service.list_crm_events(strict_pagination=True):
        if row.get("id") == external_id:
            return snapshot(row, config)
    return confirm_missing(service, external_id, previous or {})


def confirm_missing(service, external_id, previous):
    platform = previous.get("platform", "luma")
    if platform == "external" and not previous.get("url"):
        raise ValueError("Cannot confirm external listing identity")
    params = {"platform": platform}
    if platform == "external":
        params["url"] = previous["url"]
    else:
        params["event_id"] = external_id
    body = service._get("/v1/calendars/events/lookup", params=params)
    if "event" not in body:
        raise ValueError("Incomplete calendar lookup")
    record = body["event"]
    if record is None or (isinstance(record, dict) and record.get("status") == "rejected"):
        return None
    raise ValueError("Calendar listing requires revalidation")


def status_now(snapshot_data):
    return event_status(_instant(snapshot_data.get("start_at")), _instant(snapshot_data.get("end_at")),
                        timezone.now())
