"""Narrow HTTP client for the private Buzz membership adapter."""

import dataclasses
import uuid
from typing import Optional

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime


class MembershipAdapterError(RuntimeError):
    pass


class MembershipAdapterUnavailable(MembershipAdapterError):
    pass


class MembershipAdapterConflict(MembershipAdapterError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class IssuedInvite:
    code: str
    invite_id: str
    expires_at: object
    request_id: uuid.UUID


@dataclasses.dataclass(frozen=True)
class RelayMembership:
    is_member: bool
    role: Optional[str]
    joined_at: Optional[object]


def _headers(request_id):
    token = settings.COMMUNITY_CHAT_ADAPTER_TOKEN
    if not token:
        raise MembershipAdapterUnavailable("adapter_not_configured")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Request-ID": str(request_id),
    }


def _request(method, path, *, json_body=None, request_id=None):
    request_id = request_id or uuid.uuid4()
    try:
        response = requests.request(
            method,
            f"{settings.COMMUNITY_CHAT_ADAPTER_URL}{path}",
            headers=_headers(request_id),
            json=json_body,
            timeout=settings.COMMUNITY_CHAT_ADAPTER_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MembershipAdapterUnavailable("adapter_unavailable") from exc

    if response.status_code == 409:
        try:
            code = response.json().get("error", "conflict")
        except ValueError:
            code = "conflict"
        raise MembershipAdapterConflict(str(code))
    if response.status_code < 200 or response.status_code >= 300:
        raise MembershipAdapterUnavailable("adapter_rejected_request")
    try:
        return response.json(), request_id
    except ValueError as exc:
        raise MembershipAdapterUnavailable("adapter_invalid_response") from exc


def issue_member_invite(public_key):
    payload, request_id = _request(
        "POST",
        "/v1/member-invites",
        json_body={"public_key": public_key},
    )
    code = str(payload.get("invite_code") or "")
    invite_id = str(payload.get("invite_id") or "")
    expires_at = parse_datetime(str(payload.get("expires_at") or ""))
    if (
        not code
        or len(code) > 512
        or not invite_id
        or len(invite_id) > 128
        or expires_at is None
        or payload.get("role") != "member"
        or payload.get("max_uses") != 1
    ):
        raise MembershipAdapterUnavailable("adapter_invalid_response")
    return IssuedInvite(code, invite_id, expires_at, request_id)


def get_relay_membership(public_key):
    payload, _ = _request("GET", f"/v1/members/{public_key}")
    role = payload.get("role")
    joined_at = parse_datetime(str(payload.get("joined_at") or "")) if payload.get("joined_at") else None
    return RelayMembership(bool(payload.get("is_member")), str(role) if role else None, joined_at)


def revoke_relay_membership(public_key):
    payload, request_id = _request("DELETE", f"/v1/members/{public_key}")
    if payload.get("status") not in {"revoked", "not_found"}:
        raise MembershipAdapterUnavailable("adapter_invalid_response")
    return str(payload["status"]), request_id
