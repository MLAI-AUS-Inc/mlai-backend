"""Founder copy fallback telemetry from the Chat clients.

When the client's founder error mapper cannot match an error to a specific
sentence it shows the generic one and reports which copy fell back and why, so
we can measure how often founders see a sentence that does not help them.

The endpoint is deliberately narrow: every field is an identifier with a fixed
character set and length, the raw error text is never sent (only a 16-hex-digit
digest of it), the request body is capped, nothing is written to the database
and each accepted event becomes exactly one structured log line.
"""

import json
import logging
import re

from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from core.client_ip import trusted_client_ip

from .authentication import CommunityChatOnboardingAuthentication


logger = logging.getLogger(__name__)

CLIENT_EVENT_LOG_MESSAGE = "founder_client_event"
INVALID_CLIENT_EVENTS = {"error": "invalid_client_events"}

MAX_EVENTS_PER_REQUEST = 20
# Twenty maximal events serialise to well under 16 KiB.
MAX_BODY_BYTES = 16 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/-]+")
_RAW_CODE = re.compile(r"[A-Za-z0-9_.:-]{0,120}")
_RAW_DIGEST = re.compile(r"[0-9a-fA-F]{16}")
# A path only: no query, fragment, spaces or "@", so emails and tokens in
# query strings never reach the logs.
_ROUTE = re.compile(r"/[A-Za-z0-9._~%:/-]{0,199}")
_APP_VERSION = re.compile(r"[A-Za-z0-9._+-]{1,40}")
_CLIENTS = frozenset({"web", "desktop"})
_EVENT_KINDS = frozenset({"founder_copy_fallback"})
_EVENT_FIELDS = frozenset({
    "kind",
    "copy_id",
    "raw_code",
    "raw_digest",
    "status",
    "route",
    "surface",
    "client",
    "app_version",
    "occurred_at",
})
_MAX_OCCURRED_AT_LENGTH = 40


class InvalidClientEvent(ValueError):
    """A client event that does not match the telemetry contract."""


class OptionalChatAccountAuthentication(CommunityChatOnboardingAuthentication):
    """Attribute events to a Chat account when one is present, never require it.

    Pending members are accepted. An expired or invalid session is treated as
    anonymous instead of failing the request: the event is still worth
    counting, and an error report must not itself produce a sign-in error.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None


class _ClientIpRateThrottle(SimpleRateThrottle):
    """Key on the trusted client IP, not DRF's header-derived identity."""

    def client_ident(self, request):
        return trusted_client_ip(request)


class ClientEventsCallerThrottle(_ClientIpRateThrottle):
    """Per signed-in account, or per trusted client IP when anonymous."""

    scope = "community_chat_client_events"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            ident = f"user:{user.pk}"
        else:
            ident = f"ip:{self.client_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ClientEventsIpThrottle(_ClientIpRateThrottle):
    """A per-IP ceiling for every caller, signed in or not."""

    scope = "community_chat_client_events_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.client_ident(request)}


def _identifier(value, max_length):
    if not isinstance(value, str) or not 0 < len(value) <= max_length:
        raise InvalidClientEvent
    if not _IDENTIFIER.fullmatch(value):
        raise InvalidClientEvent
    return value


def _validated_event(event):
    """Return the loggable fields of one event, or raise ``InvalidClientEvent``."""
    if not isinstance(event, dict) or set(event) - _EVENT_FIELDS:
        raise InvalidClientEvent
    kind = event.get("kind")
    if kind not in _EVENT_KINDS:
        raise InvalidClientEvent
    raw_code = event.get("raw_code")
    if not isinstance(raw_code, str) or not _RAW_CODE.fullmatch(raw_code):
        raise InvalidClientEvent
    raw_digest = event.get("raw_digest")
    if not isinstance(raw_digest, str) or not _RAW_DIGEST.fullmatch(raw_digest):
        raise InvalidClientEvent
    http_status = event.get("status")
    if http_status is not None and (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 0 <= http_status <= 599
    ):
        raise InvalidClientEvent
    route = event.get("route")
    if not isinstance(route, str) or not _ROUTE.fullmatch(route):
        raise InvalidClientEvent
    client = event.get("client")
    if client not in _CLIENTS:
        raise InvalidClientEvent
    app_version = event.get("app_version")
    if not isinstance(app_version, str) or not _APP_VERSION.fullmatch(app_version):
        raise InvalidClientEvent
    occurred_raw = event.get("occurred_at")
    if not isinstance(occurred_raw, str) or len(occurred_raw) > _MAX_OCCURRED_AT_LENGTH:
        raise InvalidClientEvent
    try:
        occurred_at = parse_datetime(occurred_raw)
    except ValueError as exc:
        raise InvalidClientEvent from exc
    if occurred_at is None:
        raise InvalidClientEvent
    return {
        "kind": kind,
        "copy_id": _identifier(event.get("copy_id"), 80),
        "raw_code": raw_code,
        "raw_digest": raw_digest.lower(),
        "status": http_status,
        "route": route,
        "surface": _identifier(event.get("surface"), 60),
        "client": client,
        "app_version": app_version,
        "occurred_at": occurred_at.isoformat(),
    }


def _validated_events(body):
    if not isinstance(body, dict) or set(body) != {"events"}:
        raise InvalidClientEvent
    events = body["events"]
    if not isinstance(events, list) or not 0 < len(events) <= MAX_EVENTS_PER_REQUEST:
        raise InvalidClientEvent
    return [_validated_event(event) for event in events]


def _declared_length(request):
    raw = request.META.get("CONTENT_LENGTH")
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


class ClientEventsView(APIView):
    """Accept a batch of founder copy fallback events and log each one."""

    authentication_classes = [OptionalChatAccountAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [ClientEventsIpThrottle, ClientEventsCallerThrottle]

    def post(self, request):
        content_type = str(request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return Response(INVALID_CLIENT_EVENTS, status=status.HTTP_400_BAD_REQUEST)
        # Check the declared size before reading: the WSGI stream is bounded
        # by Content-Length, so an oversized body is never buffered.
        declared = _declared_length(request)
        if declared < 0 or declared > MAX_BODY_BYTES:
            return Response(INVALID_CLIENT_EVENTS, status=status.HTTP_400_BAD_REQUEST)
        raw_body = request.body
        if len(raw_body) > MAX_BODY_BYTES:
            return Response(INVALID_CLIENT_EVENTS, status=status.HTTP_400_BAD_REQUEST)
        try:
            events = _validated_events(json.loads(raw_body))
        except (ValueError, RecursionError):
            # ValueError covers InvalidClientEvent, UnicodeDecodeError and
            # JSONDecodeError, and also the plain ValueError json.loads raises
            # for an integer longer than Python's digit limit. RecursionError:
            # a small body can still nest deeply enough to exhaust the decoder.
            # All of these are malformed input like any other.
            return Response(INVALID_CLIENT_EVENTS, status=status.HTTP_400_BAD_REQUEST)

        user = getattr(request, "user", None)
        user_id = user.pk if user is not None and getattr(user, "is_authenticated", False) else None
        for event in events:
            fields = {**event, "user_id": user_id}
            logger.info(
                "%s %s",
                CLIENT_EVENT_LOG_MESSAGE,
                json.dumps(fields, sort_keys=True, separators=(",", ":")),
                extra={"founder_client_event": fields},
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
