"""Native relay delegation for one verified member and explicitly selected rooms.

The gateway signs a ten-second, one-use request, not an impersonated Nostr
identity. The relay checks current member ACLs and ingests separately signed
agent events through its ordinary event pipeline. Delegations and signing keys
never leave this server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit
from uuid import UUID

import requests
from coincurve import PrivateKey
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from .account_sessions import _valid_session
from .models import CommunityChatAccountSession, CommunityChatDevice, DeviceBindingStatus
from .nostr import InvalidDeviceProof, normalize_public_key

MESSAGE_KINDS = [9, 40002, 45001, 45003]


def configuration():
    """Return validated, deployment-pinned relay authority, failing closed."""
    origin = str(getattr(settings, "MLAI_MCP_COMMUNITY_RELAY_URL", "")).rstrip("/")
    secret = str(getattr(settings, "MLAI_MCP_COMMUNITY_SECRET", ""))
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        parsed = None
    if (not getattr(settings, "MLAI_MCP_COMMUNITY_ENABLED", False) or parsed is None
            or parsed.scheme != "https" or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or len(secret.encode()) < 32):
        raise PermissionDenied("Native community MCP access is not configured.")
    return origin, secret.encode()


def current_owner(user, session):
    """Require the current, verified native device of this revocable account."""
    if not _valid_session(session, timezone.now()) or session.user_id != user.pk:
        raise AuthenticationFailed("The community account session was revoked.")
    try:
        owner = normalize_public_key(session.public_key)
    except InvalidDeviceProof as exc:
        raise PermissionDenied("The native community device identity is unavailable.") from exc
    device = CommunityChatDevice.objects.filter(user=user, public_key=owner,
        status=DeviceBindingStatus.VERIFIED, revoked_at__isnull=True).first()
    if device is None:
        raise PermissionDenied("Connect a verified MLAI Chat device before granting community access.")
    return owner


def session_for(principal):
    """Resolve owner authority afresh instead of retaining device credentials."""
    session = CommunityChatAccountSession.objects.select_related("user").filter(
        pk=principal.grant["session_id"], user=principal.user).first()
    return session, current_owner(principal.user, session)


def actor_key(grant_id):
    """Derive a distinct, private signing identity for this grant only."""
    _, secret = configuration()
    seed = hmac.new(secret, b"mlai-mcp-actor-v1:" + str(grant_id).encode(), hashlib.sha256).digest()
    # SHA-256 almost always falls within the secp256k1 scalar range. A retry is
    # deterministic and avoids failing the rare zero/out-of-range derivation.
    for counter in range(256):
        try:
            return PrivateKey(seed)
        except ValueError:
            seed = hashlib.sha256(seed + bytes([counter])).digest()
    raise PermissionDenied("The community agent identity is unavailable.")


def signed_event(grant_id, *, content, channel_id, request_id, owner, client_name,
                 reply_to=None, thread_root=None, title=None, kind=9):
    """Sign an additive message with server-supplied owner/client attribution."""
    signer = actor_key(grant_id)
    actor = signer.public_key.format(compressed=True)[1:].hex()
    label = re.sub(r"[\r\n\x00-\x1f]", " ", str(client_name or "AI assistant")).encode()[:100].decode("utf-8", errors="ignore").strip()
    tags = [["h", channel_id], ["mlai-mcp", owner, request_id], ["client", label]]
    if reply_to:
        tags.extend([["e", thread_root or reply_to, "", "root"], ["e", reply_to, "", "reply"]])
    if title:
        tags.append(["title", title])
    text = content.rstrip() + f"\n\n_Sent with {label} via MLAI MCP for {owner[:8]}._"
    timestamp = int(time.time())
    digest = hashlib.sha256(json.dumps([0, actor, timestamp, kind, tags, text],
        ensure_ascii=False, separators=(",", ":")).encode()).digest()
    return {"id": digest.hex(), "pubkey": actor, "created_at": timestamp,
        "kind": kind, "tags": tags, "content": text, "sig": signer.sign_schnorr(digest).hex()}


def request(path, *, owner, scope, grant_id, channel_ids, payload, actor=None, community_id=None):
    """Send one bound delegation; redirects, provider detail and secrets stay private."""
    origin, secret = configuration()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    issued = int(time.time())
    claims = {"version": 1, "ownerPubkey": owner, "actorPubkey": actor or owner,
        "grantId": str(grant_id), "scope": scope, "channelIds": list(channel_ids),
        "communityId": community_id, "relayUrl": origin, "method": "POST", "path": path,
        "bodyHash": hashlib.sha256(body).hexdigest(), "issuedAt": issued,
        "expiresAt": issued + 10, "nonce": secrets.token_hex(32)}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=")
    signature = hmac.new(secret, b"mlai-mcp-delegation-v1:" + encoded, hashlib.sha256).hexdigest()
    try:
        response = requests.post(origin + path, data=body, headers={"Content-Type": "application/json",
            "X-MLAI-MCP-Delegation": encoded.decode(), "X-MLAI-MCP-Signature": signature},
            timeout=3, allow_redirects=False)
    except requests.RequestException as exc:
        raise ValidationError("The native community relay is unavailable. Retry using the same requestId.") from exc
    if response.status_code in {401, 403, 404}:
        raise PermissionDenied("Community membership or channel access is no longer available.")
    if not 200 <= response.status_code < 300:
        raise ValidationError("The native community relay rejected the request.")
    try:
        return response.json()
    except ValueError as exc:
        raise ValidationError("The native community relay returned an invalid response.") from exc


def consent_channels(user, session):
    """List only live, joined non-DM rooms for a verified account device."""
    owner = current_owner(user, session)
    payload = request("/query", owner=owner, scope="community:consent", grant_id="consent",
        channel_ids=[], payload=[{"kinds": [39000], "mcp_channels": True, "limit": 100}])
    origin, _ = configuration()
    if (not isinstance(payload, dict) or payload.get("relayUrl") != origin
            or payload.get("ownerPubkey") != owner or not isinstance(payload.get("channels"), list)):
        raise ValidationError("The native community channel list is unavailable.")
    try:
        community_id = str(UUID(str(payload.get("communityId"))))
    except ValueError as exc:
        raise ValidationError("The native community channel list is unavailable.") from exc
    channels = []
    for item in payload["channels"]:
        if not isinstance(item, dict) or item.get("channelType") not in {"stream", "forum"}:
            raise ValidationError("The native community channel list is unavailable.")
        try:
            channel_id = str(UUID(str(item.get("id"))))
        except ValueError as exc:
            raise ValidationError("The native community channel list is unavailable.") from exc
        channels.append({"id": channel_id, "name": str(item.get("name", ""))[:200],
            "channelType": item["channelType"], "relayUrl": origin, "ownerPubkey": owner, "communityId": community_id})
    return channels


def validate_channel_selection(user, session, channel_ids):
    """Store authoritative resources, never a caller's host, author or wildcard."""
    if (not isinstance(channel_ids, list) or not 1 <= len(channel_ids) <= 100
            or any(not isinstance(item, str) for item in channel_ids) or len(set(channel_ids)) != len(channel_ids)):
        raise ValidationError("Select between one and 100 distinct joined community channels.")
    try:
        selected = [str(UUID(item)) for item in channel_ids]
    except ValueError as exc:
        raise ValidationError("Select valid community channel IDs.") from exc
    available = {item["id"]: item for item in consent_channels(user, session)}
    if set(selected) - available.keys():
        raise PermissionDenied("Select only channels where your current device is a member.")
    return [available[item] for item in selected]
