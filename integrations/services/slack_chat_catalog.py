"""Account-scoped Slack conversation metadata stored in connector state.

This contains names, member profiles and types, never message bodies. The
existing owner mirror remains the authority for membership and relay access.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

CATALOG_KEY = "mlai_chat_conversations_v1"
PRIVATE_CHANNEL_CONSENT = "slack-chat-v4-private-channels"
PRIVATE_CHANNEL_SCOPES = {"groups:read", "groups:history"}


def catalog_tombstones(metadata):
    """Retain only type fences after disconnect; erase names and other metadata."""
    return {
        channel_id: {"kind": entry["kind"]}
        for channel_id, entry in (metadata or {}).get(CATALOG_KEY, {}).items()
        if isinstance(entry, dict)
        and entry.get("kind") in {"im", "mpim", "private_channel"}
    }


def conversation_metadata(conversation):
    catalog = (conversation.grant.connection.provider_metadata or {}).get(
        CATALOG_KEY, {}
    )
    return catalog.get(conversation.slack_conversation_id, {})


def conversation_kind(conversation):
    kind = conversation_metadata(conversation).get("kind")
    if kind in {"im", "mpim", "private_channel"}:
        return kind
    return "im" if conversation.slack_conversation_id.startswith("D") else "mpim"


def raw_conversation_kind(raw):
    if raw.get("is_im") or str(raw.get("id", "")).startswith("D"):
        return "im"
    if raw.get("is_mpim"):
        return "mpim"
    if raw.get("is_private"):
        return "private_channel"
    return None


def private_channels_enabled(grant):
    return (
        grant.consent_version == PRIVATE_CHANNEL_CONSENT
        and PRIVATE_CHANNEL_SCOPES.issubset(set(grant.connection.scopes or []))
    )


def catalog_payload(conversations, public_key):
    """Expose only mirrors provisioned for this verified device."""
    key = str(public_key or "").lower()
    return [
        {
            "channel_id": str(conversation.mlai_channel_id),
            "kind": conversation_kind(conversation),
            "last_message_at": conversation_activity_at(conversation),
            **(
                {"participants": catalog_participants(conversation)}
                if conversation_kind(conversation) in {"im", "mpim"}
                else {}
            ),
        }
        for conversation in conversations
        if key
        and conversation.mlai_channel_id
        and key in (conversation.participant_buzz_pubkeys or [])
    ]


def conversation_activity_at(conversation):
    """Latest source-message time, independent of import and metadata updates."""
    timestamps = []
    for value in (
        conversation_metadata(conversation).get("latest_message_ts"),
        getattr(conversation, "latest_synced_ts", None),
    ):
        try:
            seconds = Decimal(str(value))
            if not seconds.is_finite() or seconds <= 0:
                continue
            stamp = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
            if stamp.timestamp() <= datetime.now(timezone.utc).timestamp() + 300:
                timestamps.append(stamp)
        except (InvalidOperation, ValueError, OverflowError, OSError):
            continue
    return max(timestamps).isoformat() if timestamps else None


def catalog_participants(conversation):
    """Present Slack people independently of the mirror's transport/device keys."""
    profiles = conversation.participant_profiles or {}
    return [
        {
            "slack_user_id": slack_id,
            "display_name": str(
                (profiles.get(slack_id) or {}).get("display_name") or slack_id
            )[:255],
            "avatar_url": str((profiles.get(slack_id) or {}).get("avatar_url") or "")[
                :2000
            ],
            "is_owner": slack_id == conversation.grant.slack_user_id,
        }
        for slack_id in dict.fromkeys(conversation.participant_slack_ids or [])
    ]
