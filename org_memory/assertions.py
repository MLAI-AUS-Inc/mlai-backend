from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    ActorAssertionReceipt,
    OrganizationIdentity,
    OrganizationIdentityProvider,
    OrganizationSlackWorkspace,
)
from .service_principals import (
    ServicePrincipalAuthContext,
    record_service_principal_audit,
)


ASSERTION_HEADER = "X-MLAI-Actor-Assertion"
SURFACE_HEADER = "X-Roo-Surface"
TEAM_HEADER = "X-Slack-Team-ID"
ACTOR_HEADER = "X-Acting-Slack-User-ID"
CHANNEL_HEADER = "X-Slack-Channel-ID"
THREAD_HEADER = "X-Slack-Thread-TS"
EVENT_HEADER = "X-Slack-Event-ID"
REQUEST_HEADER = "X-Request-ID"

SURFACE_PATTERN = re.compile(r"^(public_roo|admin_roo|roo_gateway)$")
TEAM_PATTERN = re.compile(r"^T[A-Z0-9]+$")
USER_PATTERN = re.compile(r"^[UW][A-Z0-9]+$")
CHANNEL_PATTERN = re.compile(r"^[CGD][A-Z0-9]+$")
THREAD_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
ASSERTION_KEYS = frozenset(
    {
        "v",
        "kid",
        "surface",
        "slack_team_id",
        "acting_slack_user_id",
        "slack_channel_id",
        "slack_thread_ts",
        "event_id",
        "request_id",
        "iat",
        "exp",
        "nonce",
    }
)


class ActorAssertionError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedActorContext:
    organization: object
    workspace: OrganizationSlackWorkspace
    identity: OrganizationIdentity
    surface: str
    slack_team_id: str
    slack_user_id: str
    slack_channel_id: str
    slack_thread_ts: str
    event_id: str
    request_id: str

    @property
    def user(self):
        return self.identity.user


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ActorAssertionError("Malformed actor assertion encoding") from exc


def _canonical_payload(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def build_actor_assertion(
    token: str,
    *,
    credential_id: str,
    surface: str,
    slack_team_id: str,
    acting_slack_user_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    event_id: str,
    request_id: str,
    issued_at: Optional[int] = None,
    ttl_seconds: int = 45,
    nonce: Optional[str] = None,
) -> str:
    now = int(time.time()) if issued_at is None else int(issued_at)
    payload = {
        "v": 1,
        "kid": str(credential_id),
        "surface": str(surface),
        "slack_team_id": str(slack_team_id),
        "acting_slack_user_id": str(acting_slack_user_id),
        "slack_channel_id": str(slack_channel_id or ""),
        "slack_thread_ts": str(slack_thread_ts or ""),
        "event_id": str(event_id),
        "request_id": str(request_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    encoded_payload = _b64url_encode(_canonical_payload(payload))
    signature = hmac.new(
        str(token).encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_b64url_encode(signature)}"


def actor_identity_headers(
    *,
    assertion: str,
    surface: str,
    slack_team_id: str,
    acting_slack_user_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    event_id: str,
    request_id: str,
) -> dict[str, str]:
    return {
        ASSERTION_HEADER: assertion,
        SURFACE_HEADER: surface,
        TEAM_HEADER: slack_team_id,
        ACTOR_HEADER: acting_slack_user_id,
        CHANNEL_HEADER: slack_channel_id or "",
        THREAD_HEADER: slack_thread_ts or "",
        EVENT_HEADER: event_id,
        REQUEST_HEADER: request_id,
    }


def _header(headers, name: str) -> str:
    return str(headers.get(name, "") or "").strip()


def _parse_and_verify_assertion(
    assertion: str,
    *,
    token: str,
) -> dict:
    try:
        payload_part, signature_part = assertion.split(".", 1)
    except ValueError as exc:
        raise ActorAssertionError("Malformed actor assertion") from exc
    expected = hmac.new(
        token.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    provided = _b64url_decode(signature_part)
    if not hmac.compare_digest(expected, provided):
        raise ActorAssertionError("Actor assertion signature does not match")
    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActorAssertionError("Malformed actor assertion payload") from exc
    if not isinstance(payload, dict) or set(payload) != ASSERTION_KEYS:
        raise ActorAssertionError("Actor assertion has an invalid schema")
    return payload


def _validate_payload(payload: dict, headers, auth: ServicePrincipalAuthContext) -> None:
    max_age = int(getattr(settings, "ORG_MEMORY_ACTOR_ASSERTION_MAX_AGE_SECONDS", 60))
    clock_skew = int(getattr(settings, "ORG_MEMORY_ACTOR_ASSERTION_CLOCK_SKEW_SECONDS", 5))
    string_fields = ASSERTION_KEYS - {"v", "iat", "exp"}
    if type(payload["v"]) is not int or any(
        not isinstance(payload[field], str) for field in string_fields
    ):
        raise ActorAssertionError("Actor assertion field types are invalid")
    if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
        raise ActorAssertionError("Actor assertion timestamps are invalid")
    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise ActorAssertionError("Actor assertion timestamps are invalid") from exc
    now = int(time.time())
    if issued_at > now + clock_skew or expires_at <= now:
        raise ActorAssertionError("Actor assertion is expired or not yet valid")
    if expires_at <= issued_at or expires_at - issued_at > max_age:
        raise ActorAssertionError("Actor assertion lifetime is invalid")
    if payload["v"] != 1:
        raise ActorAssertionError("Unsupported actor assertion version")
    if str(payload["kid"]) != str(auth.credential.pk):
        raise ActorAssertionError("Actor assertion credential binding does not match")

    values = {
        "surface": str(payload["surface"]),
        "slack_team_id": str(payload["slack_team_id"]),
        "acting_slack_user_id": str(payload["acting_slack_user_id"]),
        "slack_channel_id": str(payload["slack_channel_id"]),
        "slack_thread_ts": str(payload["slack_thread_ts"]),
        "event_id": str(payload["event_id"]),
        "request_id": str(payload["request_id"]),
        "nonce": str(payload["nonce"]),
    }
    if not SURFACE_PATTERN.fullmatch(values["surface"]):
        raise ActorAssertionError("Actor assertion surface is invalid")
    if not TEAM_PATTERN.fullmatch(values["slack_team_id"]):
        raise ActorAssertionError("Actor assertion Slack team is invalid")
    if not USER_PATTERN.fullmatch(values["acting_slack_user_id"]):
        raise ActorAssertionError("Actor assertion Slack user is invalid")
    if not CHANNEL_PATTERN.fullmatch(values["slack_channel_id"]):
        raise ActorAssertionError("Actor assertion Slack channel is invalid")
    if values["slack_thread_ts"] and not THREAD_PATTERN.fullmatch(values["slack_thread_ts"]):
        raise ActorAssertionError("Actor assertion Slack thread is invalid")
    if not IDENTIFIER_PATTERN.fullmatch(values["event_id"]):
        raise ActorAssertionError("Actor assertion event ID is invalid")
    if not IDENTIFIER_PATTERN.fullmatch(values["request_id"]):
        raise ActorAssertionError("Actor assertion request ID is invalid")
    if not NONCE_PATTERN.fullmatch(values["nonce"]):
        raise ActorAssertionError("Actor assertion nonce is invalid")

    header_bindings = {
        "surface": _header(headers, SURFACE_HEADER),
        "slack_team_id": _header(headers, TEAM_HEADER),
        "acting_slack_user_id": _header(headers, ACTOR_HEADER),
        "slack_channel_id": _header(headers, CHANNEL_HEADER),
        "slack_thread_ts": _header(headers, THREAD_HEADER),
        "event_id": _header(headers, EVENT_HEADER),
        "request_id": _header(headers, REQUEST_HEADER),
    }
    if any(values[key] != header_bindings[key] for key in header_bindings):
        raise ActorAssertionError("Actor assertion does not match request identity headers")
    if not auth.principal.allows_surface(values["surface"]):
        raise ActorAssertionError("Service principal is not allowed on this surface")


def _claim_assertion(auth: ServicePrincipalAuthContext, payload: dict) -> None:
    expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=datetime_timezone.utc)
    with transaction.atomic():
        ActorAssertionReceipt.objects.filter(expires_at__lte=timezone.now()).delete()
        _, created = ActorAssertionReceipt.objects.get_or_create(
            principal=auth.principal,
            nonce=str(payload["nonce"]),
            defaults={
                "request_id": str(payload["request_id"]),
                "event_id": str(payload["event_id"]),
                "expires_at": expires_at,
            },
        )
    if not created:
        raise ActorAssertionError("Actor assertion has already been used")


def verify_and_resolve_actor_assertion(
    request,
    auth: ServicePrincipalAuthContext,
    *,
    required_surface: str = "admin_roo",
) -> VerifiedActorContext:
    assertion = _header(request.headers, ASSERTION_HEADER)
    if not assertion:
        raise ActorAssertionError("Actor assertion is required")
    try:
        payload = _parse_and_verify_assertion(assertion, token=auth.token)
        _validate_payload(payload, request.headers, auth)
        if str(payload["surface"]) != str(required_surface):
            raise ActorAssertionError(
                f"Actor assertion requires the {required_surface} surface"
            )

        workspace = OrganizationSlackWorkspace.objects.select_related("organization").filter(
            slack_team_id=str(payload["slack_team_id"]),
            is_active=True,
        ).first()
        if not workspace:
            raise ActorAssertionError("Slack workspace is not mapped to an organisation")
        if workspace.organization_id != auth.principal.organization_id:
            raise ActorAssertionError("Service principal cannot cross organisation boundaries")

        identity = OrganizationIdentity.objects.select_related("user").filter(
            organization=workspace.organization,
            provider=OrganizationIdentityProvider.SLACK,
            external_tenant_id=workspace.slack_team_id,
            external_user_id=str(payload["acting_slack_user_id"]),
            is_active=True,
            verified_at__isnull=False,
            user__isnull=False,
        ).first()
        if not identity:
            raise ActorAssertionError("Slack actor is not verifiably mapped to the organisation")
        if not identity.user.is_active:
            raise ActorAssertionError("Slack actor is inactive")

        _claim_assertion(auth, payload)
    except ActorAssertionError as exc:
        record_service_principal_audit(
            "actor_assertion_rejected",
            principal=auth.principal,
            credential=auth.credential,
            request_id=_header(request.headers, REQUEST_HEADER),
            remote_address=request.META.get("REMOTE_ADDR") or None,
            metadata={"reason": str(exc)},
        )
        raise

    record_service_principal_audit(
        "actor_assertion_verified",
        principal=auth.principal,
        credential=auth.credential,
        request_id=str(payload["request_id"]),
        remote_address=request.META.get("REMOTE_ADDR") or None,
        metadata={
            "surface": str(payload["surface"]),
            "slack_team_id": str(payload["slack_team_id"]),
            "slack_user_id": str(payload["acting_slack_user_id"]),
            "event_id": str(payload["event_id"]),
        },
    )
    return VerifiedActorContext(
        organization=workspace.organization,
        workspace=workspace,
        identity=identity,
        surface=str(payload["surface"]),
        slack_team_id=str(payload["slack_team_id"]),
        slack_user_id=str(payload["acting_slack_user_id"]),
        slack_channel_id=str(payload["slack_channel_id"]),
        slack_thread_ts=str(payload["slack_thread_ts"]),
        event_id=str(payload["event_id"]),
        request_id=str(payload["request_id"]),
    )
