"""Fixed replies when someone other than the owner tags a personal Dot.

A personal Dot acts only for its owner. When another member mentions it, MLAI
answers once with a short message it writes itself, signed by the Dot key. The
agent provider is never contacted and no message text is read: the relay scan
returns identifiers only and leaves out mentions the Dot has already answered.
Each tick therefore rescans a recent window and keeps no reply state, which
also picks up late or same-second mentions.
"""

import re
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import native_crypto, native_sources, personal_context, runtime
from .models import AgentAuditEvent, AgentGrant, AgentInstallation
from .policy import AgentPermissionDenied

WINDOW = timedelta(minutes=30)
PAGE_LIMIT = 50
MAX_PAGES = 5
# Bounds what a member who keeps tagging someone's Dot can cause it to post.
DAILY_REPLY_CAP = 50
REPLY_SCOPE_SECONDS = 600
HEX_ID = re.compile(r"^[0-9a-f]{64}$")
REPLIED = "native.guest.replied"


def enabled():
    return bool(getattr(settings, "AGENT_RUNTIME_NATIVE_GUEST_REPLY_ENABLED", False)) and personal_context.enabled()


def reply_text(installation):
    name = " ".join(str(installation.name or "").split())[:80] or "This assistant"
    return (f"Hi, I'm {name}, a personal assistant. I only take requests from the person I work for, "
            "so I haven't read or passed on your message. Please contact them directly.")


def answer_guest_mentions(*, limit=50, now=None):
    """Post the fixed reply to unanswered guest mentions; return how many were sent."""
    if not enabled():
        return 0
    try:
        native_sources.require_native(reply=True)
    except PermissionDenied:
        return 0
    now = now or timezone.now()
    ids = list(AgentInstallation.objects.filter(app_id="openai-dot", is_active=True, state="active",
        community_id__isnull=False, public_key__isnull=False).exclude(sealed_signing_key="")
        .order_by("pk").values_list("pk", flat=True)[:min(limit, 100)])
    sent = 0
    for installation_id in ids:
        try:
            sent += _answer_installation(installation_id, now=now)
        except (AgentPermissionDenied, PermissionDenied, ValidationError, native_sources.SourceUnavailable, ValueError):
            # Nothing is recorded, so the next tick rescans the same window.
            continue
    return sent


def _answer_installation(installation_id, *, now):
    installation = AgentInstallation.objects.select_related("owner", "owner_device").get(pk=installation_id)
    if not installation.owner.is_active or installation.owner_device_id is None:
        return 0
    # A deliberately revoked connection never speaks for its owner again.
    grant = AgentGrant.objects.filter(installation=installation, revoked_at__isnull=True).order_by("-created_at").first()
    if grant is None or len(grant.resources) != 1:
        return 0
    device = native_sources.current_device(installation.owner, public_key=installation.owner_device.public_key)
    if device.pk != installation.owner_device_id:
        return 0
    community, anchor = native_sources.parse_resource(grant.resources[0])
    if community != str(installation.community_id):
        return 0
    _, config = native_sources.community_config(community)
    used = AgentAuditEvent.objects.filter(grant__installation=installation, event_type=REPLIED,
        created_at__gte=now - timedelta(days=1)).count()
    if used >= DAILY_REPLY_CAP:
        return 0
    base = {"execution_mode": "guest", "community_id": community, "owner_pubkey": device.public_key,
        "bot_pubkey": installation.public_key, "installation_id": str(installation.pk),
        "grant_generation": grant.generation}
    since, after = int((now - WINDOW).timestamp()), None
    sent = 0
    for _ in range(MAX_PAGES):
        directive = {**base, "operation": "guest_mentions", "channel_id": anchor, "since": since, "limit": PAGE_LIMIT}
        if after:
            directive["after_event_id"] = after
        page = native_sources._request(config, "/query", [{"native_agent": directive}])
        events, cursor, more = page.get("events"), page.get("cursor"), page.get("has_more")
        if not isinstance(events, list) or not isinstance(cursor, dict) or type(more) is not bool:
            raise native_sources.SourceUnavailable("Guest mention page is invalid.")
        for mention in events:
            if used + sent >= DAILY_REPLY_CAP:
                return sent
            try:
                sent += _reply(installation, grant, base, config, _mention(mention, community, device.public_key), now=now)
            except (PermissionDenied, ValidationError):
                # This mention is no longer answerable (guest left, edited
                # away, already answered); keep answering the rest.
                continue
        if not more:
            break
        if type(cursor.get("since")) is not int or not HEX_ID.fullmatch(str(cursor.get("after_event_id", ""))):
            raise native_sources.SourceUnavailable("Guest mention cursor is invalid.")
        since, after = cursor["since"], cursor["after_event_id"]
    return sent


def _mention(value, community, owner_key):
    if (not isinstance(value, dict) or value.get("community_id") != community
            or not all(HEX_ID.fullmatch(str(value.get(key, ""))) for key in ("event_id", "root_event_id", "author_pubkey"))
            or value["author_pubkey"] == owner_key):
        raise ValidationError("Invalid guest mention.")
    try:
        channel = str(UUID(str(value.get("channel_id"))))
    except ValueError as exc:
        raise ValidationError("Invalid guest mention channel.") from exc
    if channel != value.get("channel_id"):
        raise ValidationError("Invalid guest mention channel.")
    return value


def _reply(installation, grant, base, config, mention, *, now):
    created = int(timezone.now().timestamp())
    context = {**base, "operation": "guest_reply", "channel_id": mention["channel_id"],
        "trigger_event_id": mention["event_id"], "root_event_id": mention["root_event_id"],
        "guest_pubkey": mention["author_pubkey"], "expires_at": created + REPLY_SCOPE_SECONDS}
    tags = [["h", mention["channel_id"]], ["e", mention["root_event_id"], "", "root"],
        ["e", mention["event_id"], "", "reply"], ["p", mention["author_pubkey"]],
        ["native_invocation", native_crypto.canonical_json(context)],
        ["native_installation", str(installation.pk)], ["client", "mlai-agent-gateway"]]
    event = native_crypto.sign_event(native_crypto.unseal_key(installation.sealed_signing_key),
        kind=9, created_at=created, tags=tags, content=reply_text(installation))
    result = native_sources._request(config, "/events", event)
    if result.get("event_id") != event["id"] or result.get("accepted") is not True:
        raise native_sources.SourceUnavailable("Guest reply receipt is unavailable.")
    # Identifiers only: no message text and no guest identity in the audit log.
    runtime.audit(REPLIED, grant=grant, event_id=event["id"], trigger_event_id=mention["event_id"],
        public=bool(mention.get("public")))
    return 1
