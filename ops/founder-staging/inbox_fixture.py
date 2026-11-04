"""Bounded, ephemeral inbox for the explicitly configured founder staging gate.

This service does not initialize Django, create accounts, send external mail or
persist verification codes. The normal email outbox must deliver to /deliver;
the CI runner reads only its exact fresh address from /inbox with another key.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

MAX_BODY_BYTES = 2048
MAX_DELIVERIES = 256
TTL_SECONDS = 300
MAX_SOURCE_LIFETIME_SECONDS = 600
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
FRESH_LOCAL = re.compile(r"founder-acceptance-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})")


class Conflict(ValueError):
    """A delivery identity was reused with different content."""


class Capacity(ValueError):
    """Retained unexpired deliveries already fill the bounded inbox."""


def timestamp(value):
    """Parse an aware ISO timestamp, refusing naive or oversized input."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone is required")
    return parsed.astimezone(timezone.utc)


def unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON keys")
    return value


@dataclass(frozen=True)
class Delivery:
    email: str
    code: str
    sent_at: datetime
    issued_at: datetime
    source_expires_at: datetime
    expires_at: datetime
    expires: float
    sequence: int


class Inbox:
    """Retain only bounded, fresh CI-address deliveries until their short TTL."""

    def __init__(self, domain, *, clock=time.monotonic, wall_clock=lambda: datetime.now(timezone.utc),
                 capacity=MAX_DELIVERIES, ttl=TTL_SECONDS):
        if (not isinstance(domain, str) or len(domain) > 253 or not DOMAIN.fullmatch(domain)
                or "staging" not in domain.split(".")):
            raise ValueError("An explicit staging-only email domain is required")
        if type(capacity) is not int or not 1 <= capacity <= MAX_DELIVERIES or not 0 < ttl <= TTL_SECONDS:
            raise ValueError("Inbox bounds cannot be expanded")
        self.domain, self.clock, self.wall_clock = domain, clock, wall_clock
        self.capacity, self.ttl = capacity, ttl
        self._deliveries = {}
        self._sequence = 0
        self._lock = threading.Lock()

    def validate_email(self, email):
        if not isinstance(email, str) or len(email) > 320:
            raise ValueError("Invalid fixture address")
        local, separator, domain = email.partition("@")
        if not separator or domain != self.domain or not FRESH_LOCAL.fullmatch(local):
            raise ValueError("Only exact fresh founder CI addresses are allowed")
        return email

    def _wall_now(self):
        now = self.wall_clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("An aware provider clock is required")
        return now.astimezone(timezone.utc)

    def _expire(self, now, wall_now):
        self._deliveries = {key: value for key, value in self._deliveries.items()
                            if value.expires > now and value.expires_at > wall_now}

    def expire(self):
        with self._lock:
            self._expire(self.clock(), self._wall_now())

    def record(self, delivery_id, email, code, issued_at, expires_at):
        self.validate_email(email)
        if (not isinstance(delivery_id, str) or str(uuid.UUID(delivery_id)) != delivery_id
                or uuid.UUID(delivery_id).version != 4 or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)):
            raise ValueError("A canonical delivery UUID and six-digit code are required")
        issued, source_expiry = timestamp(issued_at), timestamp(expires_at)
        with self._lock:
            now, sent_at = self.clock(), self._wall_now()
            expiry = min(source_expiry, issued + timedelta(seconds=self.ttl))
            if (not issued <= sent_at < expiry or source_expiry <= issued
                    or (source_expiry - issued).total_seconds() > MAX_SOURCE_LIFETIME_SECONDS):
                raise ValueError("Source challenge is expired, future-dated or outside the bounded window")
            self._expire(now, sent_at)
            old = self._deliveries.get(delivery_id)
            if old is not None:
                if (old.email != email or not hmac.compare_digest(old.code, code)
                        or old.issued_at != issued or old.source_expires_at != source_expiry):
                    raise Conflict("Delivery identity changed")
                return  # Retries never invent a later sentAt or extend retention.
            if len(self._deliveries) >= self.capacity:
                raise Capacity("Inbox is full")
            self._sequence += 1
            self._deliveries[delivery_id] = Delivery(email, code, sent_at, issued, source_expiry,
                expiry, now + (expiry - sent_at).total_seconds(), self._sequence)

    def read(self, email, after):
        self.validate_email(email)
        cutoff = timestamp(after)
        with self._lock:
            self._expire(self.clock(), self._wall_now())
            candidates = [value for value in self._deliveries.values() if value.email == email]
            latest = max(candidates, key=lambda value: value.sequence, default=None)
            if latest is None or latest.sent_at < cutoff or latest.issued_at < cutoff:
                return None
            return {"email": email, "code": latest.code,
                    "sentAt": latest.sent_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")}


def handler(inbox, reader_token, writer_token):
    """Create an HTTP handler with separate read and provider-write authority."""
    if (not isinstance(reader_token, str) or not isinstance(writer_token, str)
            or not TOKEN.fullmatch(reader_token) or not TOKEN.fullmatch(writer_token)
            or hmac.compare_digest(reader_token, writer_token)):
        raise ValueError("Distinct scoped inbox credentials are required")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, _format, *_args):
            pass  # Neither request URLs, addresses, bodies nor credentials enter logs.

        def respond(self, status, value):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def send_error(self, code, message=None, explain=None):
            self.respond(code, {"error": "invalid_request"})

        def authorized(self, token):
            values = self.headers.get_all("Authorization") or []
            return len(values) == 1 and len(values[0]) <= 256 and hmac.compare_digest(values[0], "Bearer " + token)

        def parse_target(self):
            if len(self.path) > 1024:
                raise ValueError("Request target is too large")
            value = urlsplit(self.path)
            if value.scheme or value.netloc or value.fragment:
                raise ValueError("Only local paths are allowed")
            return value

        def do_GET(self):
            try:
                path = self.parse_target()
                if path.path == "/healthz" and not path.query:
                    inbox.expire()
                    return self.respond(200, {"status": "ok"})
                if path.path != "/inbox":
                    return self.respond(404, {"error": "not_found"})
                if not self.authorized(reader_token):
                    return self.respond(401, {"error": "unauthorized"})
                query = parse_qs(path.query, keep_blank_values=True, strict_parsing=True)
                if set(query) != {"email", "after"} or any(len(value) != 1 for value in query.values()):
                    raise ValueError("Exact inbox lookup is required")
                value = inbox.read(query["email"][0], query["after"][0])
                return self.respond(200 if value else 404, value or {"error": "not_found"})
            except (ValueError, TypeError):
                self.respond(400, {"error": "invalid_request"})

        def do_POST(self):
            try:
                path = self.parse_target()
                if path.path != "/deliver" or path.query:
                    return self.respond(404, {"error": "not_found"})
                if not self.authorized(writer_token):
                    return self.respond(401, {"error": "unauthorized"})
                if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                    raise ValueError("A bounded JSON body is required")
                lengths = self.headers.get_all("Content-Length") or []
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0]):
                    raise ValueError("One body length is required")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY_BYTES:
                    return self.respond(413, {"error": "body_too_large"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete body")
                value = json.loads(raw, object_pairs_hook=unique_object)
                if not isinstance(value, dict) or set(value) != {"deliveryId", "email", "code", "issuedAt", "expiresAt"}:
                    raise ValueError("Unexpected delivery fields")
                inbox.record(value["deliveryId"], value["email"], value["code"], value["issuedAt"], value["expiresAt"])
                self.respond(202, {"accepted": True, "deliveryId": value["deliveryId"]})
            except Conflict:
                self.respond(409, {"error": "delivery_conflict"})
            except Capacity:
                self.respond(503, {"error": "capacity"})
            except (ValueError, TypeError, UnicodeError, AttributeError):
                self.respond(400, {"error": "invalid_request"})

    return Handler


class InboxServer(HTTPServer):
    """Single bounded request loop; expire idle records without another request."""
    request_queue_size = 16

    def service_actions(self):
        self.inbox.expire()


def main():
    if os.environ.get("APP_ENV") != "founder-inbox-staging" or os.environ.get("FOUNDER_STAGING_INBOX_ENABLED") != "true":
        raise SystemExit("The isolated founder inbox is not explicitly enabled")
    inbox = Inbox(os.environ.get("FOUNDER_STAGING_EMAIL_DOMAIN", ""))
    server = InboxServer(("0.0.0.0", 8990), handler(inbox,
        os.environ.get("FOUNDER_STAGING_INBOX_READ_TOKEN", ""), os.environ.get("FOUNDER_STAGING_INBOX_WRITE_TOKEN", "")))
    server.inbox = inbox
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
