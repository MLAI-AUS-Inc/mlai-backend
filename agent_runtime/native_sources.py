"""Native source adapter through the relay's restricted query protocol.

The gateway admission lock is held by its caller. Relay service requests must
never call back into that lock; ordinary bot-key registry reads are separate.
Public pilot reads remain exact-resource scoped. Explicit personal-context
invocations delegate only their canonical owner-signed request to its own adapter.
"""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import native_crypto
from .sources import SourceUnavailable


NATIVE_RE = re.compile(r"^community:([0-9a-f-]{36}):channel:([0-9a-f-]{36})$")


class NativeCursorReset(SourceUnavailable):
    default_detail = "Native update history changed; fresh listening is required."
    default_code = "native_cursor_reset_required"


def parse_resource(resource):
    match = NATIVE_RE.fullmatch(str(resource))
    if not match:
        raise ValidationError("Choose an exact native community channel.")
    try:
        values = tuple(str(UUID(value)) for value in match.groups())
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Native channel identifiers must be canonical UUIDs.") from exc
    if values != match.groups():
        raise ValidationError("Native channel identifiers must be canonical UUIDs.")
    return values


def community_config(community_id):
    try:
        identifier = str(UUID(str(community_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Invalid community.") from exc
    configurations = getattr(settings, "AGENT_RUNTIME_NATIVE_COMMUNITIES", {})
    configuration = configurations.get(identifier) if isinstance(configurations, dict) else None
    if not isinstance(configuration, dict):
        raise PermissionDenied("Community is outside the agent pilot.")
    origin = configuration.get("relay_url", "")
    try:
        parts = urlsplit(origin)
    except ValueError as exc:
        raise PermissionDenied("Native relay is not configured.") from exc
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise PermissionDenied("Native relay must have a configured canonical HTTPS origin.")
    channel_ids = configuration.get("channel_ids", [])
    if not isinstance(channel_ids, list) or len(channel_ids) > 100:
        raise PermissionDenied("Native channel allowlist is not configured.")
    try:
        channels = [str(UUID(value)) for value in channel_ids]
    except (ValueError, TypeError, AttributeError) as exc:
        raise PermissionDenied("Native channel allowlist is not configured.") from exc
    if channels != channel_ids:
        raise PermissionDenied("Native channel allowlist must use canonical UUIDs.")
    return identifier, {"relay_url": origin.rstrip("/"), "channel_ids": channels}


def community_for_relay(relay_url):
    raw = str(relay_url or "")
    if raw.startswith("wss://"):
        raw = "https://" + raw[6:]
    for identifier in getattr(settings, "AGENT_RUNTIME_NATIVE_COMMUNITIES", {}):
        _, config = community_config(identifier)
        if raw.rstrip("/") == config["relay_url"]:
            return identifier, config
    raise PermissionDenied("This community has no agent pilot configuration.")


def require_native(*, reply=False):
    if not getattr(settings, "AGENT_RUNTIME_ENABLED", False) or not getattr(settings, "AGENT_RUNTIME_NATIVE_ENABLED", False):
        raise PermissionDenied("Native agent access is disabled.")
    if reply and not getattr(settings, "AGENT_RUNTIME_NATIVE_REPLIES_ENABLED", False):
        raise PermissionDenied("Native agent replies are disabled.")


def current_device(owner, *, public_key=None):
    from community_chat.models import CommunityChatDevice
    if not owner.is_active:
        raise PermissionDenied("Account is no longer active.")
    rows = CommunityChatDevice.objects.filter(user=owner, status="verified", revoked_at__isnull=True)
    if public_key is not None:
        rows = rows.filter(public_key=public_key)
    device = rows.order_by("-verified_at", "pk").first()
    if device is None:
        raise PermissionDenied("Connect a verified MLAI Chat device first.")
    return device


def _request(config, path, payload):
    key = native_crypto.signing_key(getattr(settings, "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", ""))
    url = config["relay_url"] + path
    body = native_crypto.canonical_json(payload).encode()
    if len(body) > 128 * 1024:
        raise ValidationError("Native request is too large.")
    event = native_crypto.sign_event(key, kind=27235, created_at=timezone.now().timestamp(),
        tags=[["u", url], ["method", "POST"], ["payload", hashlib.sha256(body).hexdigest()],
              ["nonce", secrets.token_hex(16)]], content="")
    headers = {"Content-Type": "application/json", "Authorization": "Nostr " + base64.b64encode(
        native_crypto.canonical_json(event).encode()).decode()}
    from .webhook_transport import post_webhook, CallbackEndpointError
    try:
        response = post_webhook(url, body, headers, timeout=10, max_response_bytes=262144)
        if response.status in {401, 403, 404}:
            raise PermissionDenied("Native relay rejected this agent's authority.")
        if response.status == 409 and path == "/query":
            raise NativeCursorReset()
        if not 200 <= response.status < 300:
            raise SourceUnavailable()
        result = json.loads(response.body)
        if not isinstance(result, dict):
            raise SourceUnavailable("Native relay returned an invalid response.")
        return result
    except (CallbackEndpointError, ValueError) as exc:
        raise SourceUnavailable() from exc



def query(owner, resource, operation, *, owner_public_key=None, **context):
    require_native()
    community, channel = parse_resource(resource)
    _, config = community_config(community)
    if channel not in config["channel_ids"]:
        raise PermissionDenied("Channel is outside the native pilot.")
    device = current_device(owner, public_key=owner_public_key)
    directive = {"operation": operation, "community_id": community,
                 "owner_pubkey": device.public_key, "channel_id": channel, **context}
    return _request(config, "/query", [{"native_agent": directive}])


def eligible_channels(owner, community, *, owner_public_key=None):
    require_native()
    community, config = community_config(community)
    device = current_device(owner, public_key=owner_public_key)
    payload = [{"native_agent": {"operation": "channels", "community_id": community,
        "owner_pubkey": device.public_key, "channel_ids": config["channel_ids"]}}]
    result = _request(config, "/query", payload)
    channels = result.get("channels")
    if not isinstance(channels, list):
        raise SourceUnavailable()
    safe = []
    for channel in channels:
        if not isinstance(channel, dict) or channel.get("id") not in config["channel_ids"]:
            raise SourceUnavailable()
        safe.append({"id": channel["id"], "resource": f"community:{community}:channel:{channel['id']}",
                     "name": str(channel.get("name", ""))[:255]})
    return safe


def live_access(owner, scope, resource, *, owner_public_key=None):
    if scope not in {"messages:read", "messages:reply", "events:read"}:
        return False
    try:
        community, channel = parse_resource(resource)
        if scope == "messages:reply":
            require_native(reply=True)
        return any(row["id"] == channel for row in eligible_channels(owner, community, owner_public_key=owner_public_key))
    except (PermissionDenied, ValidationError):
        return False


def invocation_context(invocation, *, operation, now=None):
    now = now or timezone.now()
    installation = invocation.installation
    from . import personal_context
    community, channel = parse_resource(personal_context.actual_resource(invocation.resource))
    context = {"operation": operation, "community_id": community, "channel_id": channel,
        "owner_pubkey": invocation.owner_public_key, "bot_pubkey": installation.public_key,
        "installation_id": str(installation.pk), "grant_generation": invocation.generation,
        "invocation_id": str(invocation.pk), "trigger_event_id": invocation.source_event_id,
        "root_event_id": invocation.thread_id,
        "expires_at": min(int(invocation.expires_at.timestamp()), int(now.timestamp()) + 840)}
    if personal_context.is_resource(invocation.resource):
        personal_context.validate_mode(invocation.grant, invocation.resource)
        context["execution_mode"] = "personal_context"
    return context


def read_invocation(invocation, *, limit=100, cursor=""):
    if cursor:
        raise ValidationError("Native pilot context is bounded and does not support pagination.")
    context = invocation_context(invocation, operation="thread")
    for field in ("community_id", "channel_id", "owner_pubkey", "operation"):
        context.pop(field)
    from . import personal_context
    if personal_context.is_resource(invocation.resource):
        context.pop("execution_mode")
        return personal_context.relay_query(invocation.grant, invocation.resource, "thread",
            owner_public_key=invocation.owner_public_key, limit=limit, **context)
    return query(invocation.installation.owner, invocation.resource, "thread",
        owner_public_key=invocation.owner_public_key, limit=limit, **context)


def send_reply(reply):
    require_native(reply=True)
    from . import personal_context
    if personal_context.is_resource(reply.invocation.resource):
        personal_context.validate_mode(reply.invocation.grant, reply.invocation.resource)
    community, _ = parse_resource(personal_context.actual_resource(reply.invocation.resource))
    _, config = community_config(community)
    result = _request(config, "/events", reply.event)
    event_id = result.get("event_id")
    if event_id != reply.event.get("id") or result.get("accepted") is not True:
        raise SourceUnavailable("Native relay did not acknowledge the exact reply.")
    return {"event_id": event_id, "accepted": True, "resource": personal_context.actual_resource(reply.invocation.resource),
            "thread_id": reply.invocation.thread_id}
