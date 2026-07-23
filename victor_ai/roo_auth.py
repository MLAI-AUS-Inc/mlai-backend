"""Signed, channel-bound authentication for Roo's Victor application reads."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.permissions import BasePermission

from .models import VictorRooRequestReceipt


logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Victor-Roo-Signature"
TIMESTAMP_HEADER = "X-Victor-Roo-Timestamp"
NONCE_HEADER = "X-Victor-Roo-Nonce"
SURFACE_HEADER = "X-Roo-Surface"
TEAM_HEADER = "X-Slack-Team-ID"
ACTOR_HEADER = "X-Acting-Slack-User-ID"
CHANNEL_HEADER = "X-Slack-Channel-ID"
THREAD_HEADER = "X-Slack-Thread-TS"
EVENT_HEADER = "X-Slack-Event-ID"
REQUEST_HEADER = "X-Request-ID"

TEAM_PATTERN = re.compile(r"^T[A-Z0-9]+$")
USER_PATTERN = re.compile(r"^[UW][A-Z0-9]+$")
CHANNEL_PATTERN = re.compile(r"^[CG][A-Z0-9]+$")
THREAD_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


class VictorRooAuthError(ValueError):
    pass


@dataclass(frozen=True)
class VictorRooActor:
    surface: str
    slack_team_id: str
    slack_user_id: str
    slack_channel_id: str
    slack_thread_ts: str
    event_id: str
    request_id: str


def _header(request, name: str) -> str:
    return str(request.headers.get(name, "") or "").strip()


def canonical_actor_payload(
    *,
    surface: str,
    slack_team_id: str,
    acting_slack_user_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    event_id: str,
    request_id: str,
    timestamp: int,
    nonce: str,
) -> bytes:
    return json.dumps(
        {
            "acting_slack_user_id": acting_slack_user_id,
            "event_id": event_id,
            "nonce": nonce,
            "request_id": request_id,
            "slack_channel_id": slack_channel_id,
            "slack_team_id": slack_team_id,
            "slack_thread_ts": slack_thread_ts,
            "surface": surface,
            "timestamp": timestamp,
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _claim_nonce(*, nonce: str, request_id: str, event_id: str, max_age: int) -> None:
    now = timezone.now()
    try:
        with transaction.atomic():
            VictorRooRequestReceipt.objects.filter(expires_at__lte=now).delete()
            VictorRooRequestReceipt.objects.create(
                nonce=nonce,
                request_id=request_id,
                event_id=event_id,
                expires_at=now + timedelta(seconds=max_age),
            )
    except IntegrityError as exc:
        raise VictorRooAuthError("Signed Roo request has already been used") from exc


def verify_victor_roo_request(request) -> VictorRooActor:
    if not bool(getattr(settings, "VICTOR_AI_ROO_ENABLED", False)):
        raise VictorRooAuthError("Victor Roo access is disabled")

    secret = str(getattr(settings, "VICTOR_AI_ROO_SIGNING_SECRET", "") or "")
    if len(secret) < 32:
        raise VictorRooAuthError("Victor Roo access is not securely configured")

    values = {
        "surface": _header(request, SURFACE_HEADER),
        "slack_team_id": _header(request, TEAM_HEADER),
        "acting_slack_user_id": _header(request, ACTOR_HEADER),
        "slack_channel_id": _header(request, CHANNEL_HEADER),
        "slack_thread_ts": _header(request, THREAD_HEADER),
        "event_id": _header(request, EVENT_HEADER),
        "request_id": _header(request, REQUEST_HEADER),
        "nonce": _header(request, NONCE_HEADER),
    }
    try:
        timestamp = int(_header(request, TIMESTAMP_HEADER))
    except ValueError as exc:
        raise VictorRooAuthError("Signed Roo request timestamp is invalid") from exc

    signature = _header(request, SIGNATURE_HEADER).lower()
    if not signature.startswith("v1=") or len(signature) != 67:
        raise VictorRooAuthError("Signed Roo request signature is invalid")
    if values["surface"] != "public_roo":
        raise VictorRooAuthError("Victor application reads require Public Roo")
    if not TEAM_PATTERN.fullmatch(values["slack_team_id"]):
        raise VictorRooAuthError("Slack workspace identity is invalid")
    if not USER_PATTERN.fullmatch(values["acting_slack_user_id"]):
        raise VictorRooAuthError("Slack actor identity is invalid")
    if not CHANNEL_PATTERN.fullmatch(values["slack_channel_id"]):
        raise VictorRooAuthError("Slack channel identity is invalid")
    if values["slack_thread_ts"] and not THREAD_PATTERN.fullmatch(values["slack_thread_ts"]):
        raise VictorRooAuthError("Slack thread identity is invalid")
    if not IDENTIFIER_PATTERN.fullmatch(values["event_id"]):
        raise VictorRooAuthError("Slack event identity is invalid")
    if not IDENTIFIER_PATTERN.fullmatch(values["request_id"]):
        raise VictorRooAuthError("Request identity is invalid")
    if not NONCE_PATTERN.fullmatch(values["nonce"]):
        raise VictorRooAuthError("Signed Roo request nonce is invalid")

    max_age = int(getattr(settings, "VICTOR_AI_ROO_ASSERTION_MAX_AGE_SECONDS", 60))
    clock_skew = int(getattr(settings, "VICTOR_AI_ROO_ASSERTION_CLOCK_SKEW_SECONDS", 5))
    now = int(time.time())
    if timestamp > now + clock_skew or now - timestamp > max_age:
        raise VictorRooAuthError("Signed Roo request is expired or not yet valid")

    payload = canonical_actor_payload(timestamp=timestamp, **values)
    expected = "v1=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise VictorRooAuthError("Signed Roo request signature does not match")
    _claim_nonce(
        nonce=values["nonce"],
        request_id=values["request_id"],
        event_id=values["event_id"],
        max_age=max_age,
    )
    return VictorRooActor(
        surface=values["surface"],
        slack_team_id=values["slack_team_id"],
        slack_user_id=values["acting_slack_user_id"],
        slack_channel_id=values["slack_channel_id"],
        slack_thread_ts=values["slack_thread_ts"],
        event_id=values["event_id"],
        request_id=values["request_id"],
    )


class HasVictorRooAccess(BasePermission):
    message = "Victor application data is not available in this context."

    def has_permission(self, request, view):
        if request.method not in {"GET", "HEAD"}:
            return False
        try:
            request.victor_roo_actor = verify_victor_roo_request(request)
        except VictorRooAuthError as exc:
            logger.warning(
                "VICTOR_ROO_ACCESS_DENIED reason=%s request_id=%s channel_id=%s",
                str(exc),
                _header(request, REQUEST_HEADER),
                _header(request, CHANNEL_HEADER),
            )
            return False
        return True
