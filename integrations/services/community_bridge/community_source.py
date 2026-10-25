"""Authenticated member-only Community intake and retained legacy evidence.

No callback or worker writes comparison epochs. Historical receipt markers are
validated solely for retention; they cannot authorize content or restore bot sync.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings
from integrations.models import CommunityBridgeChannel, SlackDmMirrorConversation
from . import community_source_codec as codec

_current = ContextVar("community_authenticated_source", default=None)


@dataclass(frozen=True)
class AuthenticatedObservation:
    app_id: str
    workspace_id: str
    source_event_id: str
    received_ns: int
    inbox_id: int | None


def roles():
    bot = getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "")
    member = getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", "")
    if (not codec.identifier(bot, "A") or not codec.identifier(member, "A") or bot == member):
        return None
    return {bot: "bot", member: "members"}


def authenticated_role(payload):
    context = _current.get()
    configured = roles()
    if (context is None or not configured or context.app_id not in configured
            or context.workspace_id != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
            or payload.get("api_app_id") != context.app_id
            or payload.get("team_id") != context.workspace_id):
        return None
    return configured[context.app_id]


def retirement_policy(row, payload):
    """Recheck under the exact Channel lock; no receipt key grants authority."""
    role = authenticated_role(payload)
    if _current.get() is None:
        return None
    if row is None or row.audience != "community" or row.destination_platform != "buzz":
        return None
    if role is None:
        return "hold"
    if role == "bot":
        # Reader loss never promotes the bot's copy into the content lane.
        return "retire"
    from integrations.services import slack_member_access as access
    assessment = access.community_reader_policy_current(row)
    if (assessment is None or assessment.effective_source == "hold"
            or not assessment.complete and assessment.eligible_count <= 0):
        return "hold"
    access.record_community_reader_state(row, assessment)
    return "active" if assessment.effective_source == role else "retire"


def strip_untrusted(payload):
    return {key: value for key, value in payload.items() if key != codec.KEY}


@contextmanager
def authenticated_callback(payload, *, app_id, received_at, inbox_id=None, source_event_id=None):
    """Call only after signature verification, or from the verified inbox row.

    Values are supplied separately from provider JSON. Historical/synthetic
    calls and direct store entry have no context and cannot create evidence.
    """
    context = None
    event_id = source_event_id if source_event_id is not None else payload.get("event_id")
    if (type(app_id) is str and app_id and len(app_id) <= 100
            and payload.get("api_app_id") == app_id
            and type(event_id) is str and event_id and len(event_id) <= 255
            and (payload.get("event_id") == event_id or payload.get("type") == "app_rate_limited")
            and (inbox_id is None or type(inbox_id) is int and inbox_id > 0)):
        context = AuthenticatedObservation(app_id, str(payload.get("team_id") or ""), event_id,
                                           codec.stamp(received_at), inbox_id)
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def room_identity(row):
    return {"row_id": row.pk, "workspace_id": row.slack_workspace_id,
            "channel_id": row.slack_channel_id, "destination_id": row.destination_channel_id}


def public_room(row):
    return bool(row is not None and row.audience == "community" and row.enabled
                and row.destination_platform == "buzz" and codec.room_valid(room_identity(row))
                and row.slack_workspace_id == getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
                and row.slack_visibility == "public" and row.slack_seen_at is not None
                and not row.slack_archived and not row.slack_deleted
                and row.mirror_decision == "mirror" and not row.hold_reason)


def wake_source_revalidation(payload):
    """Metadata-only wake before callback hints can acquire any Channel."""
    if authenticated_role(payload) != "members":
        return
    from .member_channel_events import _channel_id, _private_hint, _external
    from integrations.services import slack_member_access as access
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if event.get("type") not in {"message", "reaction_added", "reaction_removed"} or _private_hint(event) or _external(payload, event):
        return
    row = CommunityBridgeChannel.objects.filter(
        slack_workspace_id=payload.get("team_id"), slack_channel_id=_channel_id(event),
        audience="community", destination_platform="buzz",
    ).first()
    if (row is None or not access.community_source_needs_revalidation(row)
            or SlackDmMirrorConversation.objects.filter(
                slack_workspace_id=row.slack_workspace_id, slack_conversation_id=row.slack_channel_id).exists()):
        return
    from integrations.models import BridgeSyncState
    from integrations.services.message_sync.history import ensure_state
    from integrations.services.message_sync.head_repair import wake_head_locked
    state = BridgeSyncState.objects.select_for_update().filter(public_channel=row).first()
    if state is None:
        seeded = ensure_state(row)
        state = BridgeSyncState.objects.select_for_update().get(pk=seeded.pk)
    if state.workspace_id == row.slack_workspace_id and state.source_channel_id == row.slack_channel_id:
        wake_head_locked(state)


def retained_payload(receipt):
    value = codec.marker((receipt.payload or {}).get(codec.KEY)) if isinstance(receipt.payload, dict) else None
    if (value is None or receipt.platform != "slack" or receipt.channel_id is None
            or receipt.channel.audience != "community" or receipt.channel.destination_platform != "buzz"
            or room_identity(receipt.channel) != value["room"]):
        return {}
    return {codec.KEY: value}


def status():
    """Read current reader availability without writing rollout evidence."""
    from integrations.services import slack_member_access as access
    rooms = []
    for row in CommunityBridgeChannel.objects.filter(
        audience="community", destination_platform="buzz",
        slack_workspace_id=getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", ""),
    ).order_by("pk"):
        assessment = access.assess_readers(row.slack_workspace_id, row.slack_channel_id)
        rooms.append({
            "room_id": row.pk, "eligible_reader_count": assessment.eligible_count,
            "unknown_reader_count": assessment.unknown_count, "complete": assessment.complete,
            "effective_source": assessment.effective_source if public_room(row) else "hold",
            "hold_reason": assessment.fallback_reason or ("reader_unknown" if not assessment.eligible_count else ""),
        })
    return {"mode": "members", "policy": "permanent", "rooms": rooms}
