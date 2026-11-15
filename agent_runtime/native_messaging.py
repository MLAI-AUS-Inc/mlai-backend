"""Proactive Dot messages in MLAI Chat: post, open a DM, list conversations.

A Dot acts only for its owner. Every call is a short personal delegation the
relay re-checks against the owner's current access: a post lands only where
the owner can read and write, and a DM the Dot opens always includes the
owner, so the owner sees everything their Dot says. Members who mute the Dot
cannot be DMed by it. Daily caps bound volume, and audits record identifiers
only, never message text.
"""

import hashlib
import re
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import native_crypto, native_sources, personal_context, runtime
from .models import AgentAuditEvent
from .native_invocations import native_day_start
from .policy import AgentPermissionDenied

SCOPE = "agent:messages:send"
SEND_EVENT = "native.dot.message_sent"
DM_EVENT = "native.dot.dm_opened"
DAILY_SENDS = 100
DAILY_NEW_DMS = 20
MAX_TEXT_BYTES = 16384
MAX_PARTICIPANTS = 7
HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TOOLS = [
    {"name": "list_my_conversations",
     "description": "List the MLAI Chat channels and DMs your owner belongs to, newest membership first, with each DM's "
                    "participant keys. Use a conversation id with send_message.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    {"name": "open_dm",
     "description": "Open (or find) a DM in MLAI Chat between your owner, you and up to 7 other members, so you can "
                    "message them with send_message. Members are MLAI account emails or chat public keys. Your owner "
                    "is always in the DM and sees what you send. Members who muted you cannot be added.",
     "inputSchema": {"type": "object", "properties": {
         "members": {"type": "array", "minItems": 1, "maxItems": MAX_PARTICIPANTS, "items": {"type": "string"}}},
         "required": ["members"], "additionalProperties": False}},
    {"name": "send_message",
     "description": "Post a new message as yourself in an MLAI Chat channel or DM your owner belongs to (an id from "
                    "list_my_conversations or open_dm). Use it for updates your owner asked for, such as a daily "
                    "summary. To answer a mention, use reply_to_invocation instead. No @everyone or @channel. "
                    f"Limits: {DAILY_SENDS} messages and {DAILY_NEW_DMS} new DMs a day. Use one stable "
                    "idempotency_key per message; retrying with the same key never posts twice.",
     "inputSchema": {"type": "object", "properties": {
         "conversation_id": {"type": "string", "format": "uuid"},
         "text": {"type": "string", "minLength": 1, "maxLength": 12000},
         "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}},
         "required": ["conversation_id", "text", "idempotency_key"], "additionalProperties": False}},
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def enabled():
    return bool(getattr(settings, "AGENT_RUNTIME_DOT_MESSAGING_ENABLED", False) and personal_context.enabled())


def _require(native):
    if not enabled():
        raise AgentPermissionDenied("dot_messaging_unavailable")
    installation = native.grant.installation
    if installation.app_id != "openai-dot" or installation.state != "active" or not installation.is_active:
        raise AgentPermissionDenied("installation_inactive")
    if not installation.owner_device_id:
        raise AgentPermissionDenied("owner_action_required")
    return installation


def _directive(native, operation, *, now, **extra):
    installation = _require(native)
    grant = native.grant
    community, config = native_sources.community_config(str(installation.community_id))
    device = native_sources.current_device(installation.owner, public_key=installation.owner_device.public_key)
    directive = {"operation": operation, "execution_mode": "personal_context", "community_id": community,
        "owner_pubkey": device.public_key, "bot_pubkey": installation.public_key,
        "installation_id": str(installation.pk), "grant_generation": grant.generation,
        "invocation_id": str(uuid.uuid4()),
        "expires_at": min(int(grant.expires_at.timestamp()), int(now.timestamp()) + 840), **extra}
    return installation, config, directive


def _sent_today(installation, event_type, now):
    return AgentAuditEvent.objects.filter(grant__installation=installation, event_type=event_type,
        created_at__gte=native_day_start(now)).count()


def list_conversations(native, arguments):
    now = timezone.now()
    limit = arguments.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("limit must be between 1 and 100.")
    _, config, directive = _directive(native, "conversations", now=now, limit=limit)
    result = native_sources._request(config, "/query", [{"native_agent": directive}])
    rows = result.get("conversations")
    if not isinstance(rows, list):
        raise native_sources.SourceUnavailable()
    safe = []
    for row in rows[:limit]:
        if not isinstance(row, dict) or row.get("type") not in {"stream", "dm"}:
            continue
        item = {"id": str(row.get("id", "")), "type": "dm" if row["type"] == "dm" else "channel",
                "name": str(row.get("name") or "")[:255]}
        if row["type"] == "dm":
            item["participants"] = [key for key in row.get("participants", []) if isinstance(key, str) and HEX_KEY.fullmatch(key)][:9]
        safe.append(item)
    return {"conversations": safe}


def _member_key(value, owner):
    if not isinstance(value, str):
        raise ValidationError("Members must be emails or public keys.")
    value = value.strip()
    if HEX_KEY.fullmatch(value.lower()):
        return value.lower()
    if not EMAIL.fullmatch(value):
        raise ValidationError("Members must be emails or public keys.")
    from community_chat.models import CommunityChatDevice
    user = get_user_model().objects.filter(email__iexact=value, is_active=True).first()
    device = (CommunityChatDevice.objects.filter(user=user, status="verified", revoked_at__isnull=True)
              .order_by("-verified_at", "pk").first()) if user else None
    if device is None:
        raise ValidationError("That member has not joined MLAI Chat yet.", code="member_unavailable")
    if user.pk == owner.pk:
        raise ValidationError("Your owner is always in the DM; list only the other members.")
    return device.public_key


def open_dm(native, arguments):
    now = timezone.now()
    members = arguments.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_PARTICIPANTS:
        raise ValidationError(f"Choose 1 to {MAX_PARTICIPANTS} members.")
    installation = _require(native)
    keys = []
    for member in members:
        key = _member_key(member, installation.owner)
        if key not in keys:
            keys.append(key)
    if _sent_today(installation, DM_EVENT, now) >= DAILY_NEW_DMS:
        raise AgentPermissionDenied("daily_limit_reached")
    _, config, directive = _directive(native, "open_dm", now=now, participants=keys)
    if directive["owner_pubkey"] in keys or directive["bot_pubkey"] in keys:
        raise ValidationError("List only the other members.")
    result = native_sources._request(config, "/query", [{"native_agent": directive}])
    channel = result.get("channel_id")
    try:
        channel = str(uuid.UUID(str(channel)))
    except ValueError as exc:
        raise native_sources.SourceUnavailable() from exc
    if result.get("created") is True:
        runtime.audit(DM_EVENT, grant=native.grant, channel_id=channel, participants=len(keys))
    return {"conversation_id": channel, "created": result.get("created") is True}


def post_event(installation, directive, text, *, created_at):
    tags = [["h", directive["channel_id"]], ["native_invocation", native_crypto.canonical_json(directive)],
            ["native_installation", str(installation.pk)], ["client", "mlai-agent-gateway"]]
    return native_crypto.sign_event(native_crypto.unseal_key(installation.sealed_signing_key),
                                   kind=9, created_at=created_at, tags=tags, content=text)


def send_message(native, arguments):
    now = timezone.now()
    text, key = arguments.get("text"), arguments.get("idempotency_key")
    if (not isinstance(text, str) or not text.strip() or len(text) > 12000
            or len(text.encode()) > MAX_TEXT_BYTES or "\x00" in text):
        raise ValidationError("Message must contain bounded plain text.")
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise ValidationError("Use a stable idempotency_key of 8 to 128 characters.")
    try:
        channel = str(uuid.UUID(str(arguments.get("conversation_id"))))
    except ValueError as exc:
        raise ValidationError("Choose a conversation id from list_my_conversations.") from exc
    installation = _require(native)
    digest = native_crypto.canonical_json({"channel": channel, "key": key})
    previous = AgentAuditEvent.objects.filter(grant__installation=installation, event_type=SEND_EVENT,
        metadata__key_digest=_digest(digest)).first()
    if previous is not None:
        return {"conversation_id": channel, "event_id": previous.metadata.get("event_id"), "status": "sent"}
    if _sent_today(installation, SEND_EVENT, now) >= DAILY_SENDS:
        raise AgentPermissionDenied("daily_limit_reached")
    _, config, directive = _directive(native, "post", now=now, channel_id=channel)
    event = post_event(installation, directive, text, created_at=int(now.timestamp()))
    result = native_sources._request(config, "/events", event)
    if result.get("event_id") != event["id"] or result.get("accepted") is not True:
        raise native_sources.SourceUnavailable("Native relay did not acknowledge the exact message.")
    runtime.audit(SEND_EVENT, grant=native.grant, channel_id=channel, event_id=event["id"], key_digest=_digest(digest))
    return {"conversation_id": channel, "event_id": event["id"], "status": "sent"}


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def execute(native, name, arguments):
    if name not in NAMES:
        raise ValidationError("Unknown tool.")
    if not isinstance(arguments, dict):
        raise ValidationError("Tool arguments must be an object.")
    if name == "list_my_conversations":
        return list_conversations(native, arguments)
    if name == "open_dm":
        return open_dm(native, arguments)
    return send_message(native, arguments)

