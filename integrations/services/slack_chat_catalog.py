"""Account-scoped Slack conversation metadata stored in connector state.

This contains names, member profiles and types, never message bodies. The
existing owner mirror remains the authority for membership and relay access.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import uuid

from django.db.models import Exists, OuterRef, Q, Subquery

STALLED_DELIVERY_ATTEMPTS = 3  # Queued rows that failed this often no longer hold an import open.

CATALOG_KEY = "mlai_chat_conversations_v1"
PRIVATE_CHANNEL_CONSENT = "slack-chat-v4-private-channels"
# Zero is all available history only after this explicit owner consent. Legacy
# zero-valued grants remain bounded until the owner chooses the new option.
ALL_HISTORY_CONSENT = "slack-chat-v5-all-available-history"
PRIVATE_CHANNEL_CONSENTS = frozenset({PRIVATE_CHANNEL_CONSENT, ALL_HISTORY_CONSENT})
PRIVATE_CHANNEL_SCOPES = {"groups:read", "groups:history"}
PUBLIC_CHANNEL_SCOPES = frozenset({"channels:read", "channels:history", "chat:write"})
OWNER_OPENED_KEY = "owner_opened_v1"


def supported_catalog_kinds(raw):
    """Parse the explicit client capability; absence retains legacy catalogs."""
    known = frozenset({"im", "mpim", "private_channel", "public_channel"})
    if isinstance(raw, (set, frozenset, tuple, list)):
        return frozenset(raw) if all(isinstance(value, str) and value in known for value in raw) else frozenset()
    if not isinstance(raw, str) or not raw or len(raw) > 256:
        return frozenset()
    values = [value.strip() for value in raw.split(",")]
    return frozenset(values) if all(value in known for value in values) else frozenset()


def _member_room_acknowledged(bridge, current_hash):
    """Match a real adapter acknowledgement to this immutable room identity."""
    state = bridge.room_state if isinstance(bridge.room_state, dict) else {}
    applied, ack = state.get("applied"), state.get("audience_applied")
    try:
        channel_id = str(uuid.UUID(str(bridge.destination_channel_id)))
        generation = str(uuid.UUID(str(bridge.audience_generation)))
    except (ValueError, TypeError, AttributeError):
        return None
    if (generation == str(uuid.UUID(int=0)) or bridge.audience_sync_pending or bridge.room_sync_pending
            or not isinstance(applied, dict) or applied.get("audience") != "members"
            or not isinstance(ack, dict) or not re.fullmatch(r"[0-9a-f]{64}", bridge.audience_hash or "")
            or current_hash != bridge.audience_hash
            or ack != {"generation": generation, "hash": bridge.audience_hash,
                       "workspace_id": bridge.slack_workspace_id, "channel_id": bridge.slack_channel_id,
                       "destination_channel_id": bridge.destination_channel_id}):
        return None
    return channel_id


def member_channel_room_id(grant, public_key, bridge, *, catalog_kinds=None):
    """Read-only capability/authority snapshot for one acknowledged Members room.

    Re-derive the complete key hash so any newly verified or revoked device
    cannot inherit an older audience acknowledgement. This snapshot grants no
    provider or relay authority; those boundaries recheck their own fences.
    """
    from integrations.services import slack_member_access as access
    from integrations.services import slack_channel_membership as membership
    from integrations.services.community_bridge.member_audiences import audience_hash, current_audience_keys
    from integrations.services.community_bridge.member_channels import member_channel_in_scope
    from integrations.services.slack_member_devices import member_device_key_allowed

    key = str(public_key or "").strip().lower()
    try:
        source_age = (datetime.now(timezone.utc) - bridge.slack_seen_at).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None
    if ("public_channel" not in supported_catalog_kinds(catalog_kinds) or not access.enabled()
            or bridge.audience != "members" or bridge.destination_platform != "buzz" or not bridge.enabled
            or bridge.slack_workspace_id != grant.slack_workspace_id or bridge.slack_visibility != "public"
            or bridge.slack_archived or bridge.slack_deleted or bridge.mirror_decision != "mirror" or bridge.hold_reason
            or not 0 <= source_age <= 300
            or not member_channel_in_scope(bridge.slack_workspace_id, bridge.slack_channel_id)
            or not public_channels_enabled(grant) or not re.fullmatch(r"[0-9a-f]{64}", key)
            or not member_device_key_allowed(grant.user_id, key)):
        return None
    members = membership.consenting_members(bridge.slack_workspace_id, bridge.slack_channel_id)
    if not any(member.pk == grant.pk and member.user_id == grant.user_id for member in members):
        return None
    keys = current_audience_keys(bridge.slack_workspace_id, bridge.slack_channel_id)
    if keys is None or key not in keys:
        return None
    return _member_room_acknowledged(bridge, audience_hash(keys))


def member_channel_catalog_payload(grant, public_key, *, catalog_kinds=None):
    """Expose public_channel only after the client opts into that chat type."""
    from integrations.models import CommunityBridgeChannel
    if "public_channel" not in supported_catalog_kinds(catalog_kinds):
        return []
    result = []
    for bridge in CommunityBridgeChannel.objects.filter(
        slack_workspace_id=grant.slack_workspace_id, audience="members", destination_platform="buzz", enabled=True,
    ).order_by("slack_channel_id"):
        channel_id = member_channel_room_id(grant, public_key, bridge, catalog_kinds=catalog_kinds)
        if channel_id is not None:
            result.append({"channel_id": channel_id, "slack_conversation_id": bridge.slack_channel_id,
                           "kind": "public_channel", "last_message_at": None,
                           "ready_for_display": True, "history_complete": False,
                           "history_oldest_ts": "", "source_archived": False})
    return result


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
        grant.consent_version in PRIVATE_CHANNEL_CONSENTS
        and PRIVATE_CHANNEL_SCOPES.issubset(set(grant.connection.scopes or []))
    )


def public_channels_enabled(grant, *, authority=None):
    """Use existing mirror consent and exact OAuth authority for public sources.

    This is an eligibility snapshot, not a lease. Every provider call must
    revalidate the captured authority under the existing user consent lock.
    No token refresh or Slack request is performed by this predicate.
    """
    from integrations.models import SlackDmMirrorGrant
    from integrations.services.slack_dm_mirror import (
        SlackDmMirrorError, _capture_slack_grant_api_authority,
        _slack_grant_api_authority_is_current,
    )
    from integrations.services.slack_oauth_authority import current_slack_oauth_generation

    try:
        scopes = grant.connection.scopes
        if (
            grant.consent_version not in PRIVATE_CHANNEL_CONSENTS | {SlackDmMirrorGrant.CONSENT_VERSION}
            or grant.consented_at is None
            or not grant.user.is_active
            or not isinstance(scopes, (list, tuple, set, frozenset))
            or not all(isinstance(scope, str) for scope in scopes)
            or not PUBLIC_CHANNEL_SCOPES.issubset({scope.strip() for scope in scopes})
        ):
            return False
        authority = authority or _capture_slack_grant_api_authority(grant, refresh_token=False)
        return current_slack_oauth_generation(grant.user_id) == authority.oauth_generation and _slack_grant_api_authority_is_current(
            grant, grant.connection, authority, required_scopes=PUBLIC_CHANNEL_SCOPES,
        )
    except (AttributeError, TypeError, ValueError, SlackDmMirrorError):
        return False


def catalog_conversations(conversations):
    """Load the account's shared catalogue once instead of once per mirror.

    A connection's provider_metadata can contain thousands of conversations.
    Joining it onto every mirror row multiplies transfer and JSON decoding.
    Prefetch preserves the caller's owner/status filters and shares FK objects.
    """
    from integrations.models import BridgeSyncState, SlackDmMirrorDelivery
    from integrations.services.message_sync.private_coverage import coverage_attempt_rows, current_coverage_rows

    unfinished = SlackDmMirrorDelivery.objects.filter(
        conversation_id=OuterRef("pk"), source_platform="slack",
        metadata__backfill=True,
        status__in=["pending", "processing", "failed", "dead"],
    ).exclude(
        # Room replacement leaves content-free cancellation tombstones. A later
        # audience update can rebind their metadata; these are not undelivered
        # changes in the replacement room (the refresh status uses this too).
        status="dead", last_error="Private conversation participants changed",
    ).filter(
        Q(metadata__history_recovery_superseded__isnull=True)
        | Q(metadata__history_recovery_superseded=False),
    ).filter(
        Q(metadata__history_outside_window__isnull=True)
        | Q(metadata__history_outside_window=False),
    )
    unfinished = unfinished.filter(
        Q(metadata__dependency_superseded__isnull=True) | Q(metadata__dependency_superseded=False),
    ).exclude(status__in=['pending', 'processing'], attempts__gte=STALLED_DELIVERY_ATTEMPTS)
    return conversations.select_related(None).prefetch_related(
        "grant__connection",
    ).annotate(
        _import_pending=Exists(unfinished),
        _import_verified=Exists(current_coverage_rows()),
        _import_limited=Exists(coverage_attempt_rows().filter(
            verified_ranges__archive__classification="source_limited",
        )),
        _publication_scope=Subquery(BridgeSyncState.objects.filter(
            private_conversation_id=OuterRef("pk"),
        ).values("verified_ranges__publication__scope")[:1]),
        _presentation_scope=Subquery(BridgeSyncState.objects.filter(
            private_conversation_id=OuterRef("pk"),
        ).values("verified_ranges__presentation__scope")[:1]),
        _presentation_source_ts=Subquery(BridgeSyncState.objects.filter(
            private_conversation_id=OuterRef("pk"),
        ).values("verified_ranges__presentation__source_message_ts")[:1]),
    )


def history_oldest_ts(conversation, *, now=None):
    """Return the current consent cutoff without treating legacy zero as all."""
    from integrations.services.slack_dm_mirror import _grant_history_days

    days = _grant_history_days(conversation.grant)
    now = now or datetime.now(timezone.utc)
    return str(int((now - timedelta(days=days)).timestamp())) if days else ""


def owner_open_intent(grant, public_key):
    """Bind an explicit compose action to the current owner consent and device."""
    from integrations.services.slack_dm_mirror import _grant_history_days
    from integrations.services.slack_oauth_authority import connection_slack_oauth_generation

    return {
        "grant_id": grant.pk,
        "consented_at": grant.consented_at.isoformat(),
        "history_days": _grant_history_days(grant),
        "oauth_generation": connection_slack_oauth_generation(grant.connection),
        "public_key": str(public_key or "").strip().lower(),
    }


def history_complete(conversation):
    """Certify the selected archive independently of partial display readiness."""
    completed = conversation.history_backfilled_at
    return bool(
        completed is not None and completed >= conversation.grant.consented_at
        and getattr(conversation, "_import_verified", False)
        and not getattr(conversation, "_import_pending", True)
        and not getattr(conversation, "_import_limited", False)
    )


def source_limited_without_pending(grant, source_id):
    """Identify a completed current-room attempt after receipt recovery is empty."""
    conversation = catalog_conversations(grant.conversations.filter(
        slack_conversation_id=source_id, status="live",
    )).first()
    completed_limited = bool(
        conversation is not None and conversation._import_limited
        and not conversation._import_pending
        and conversation.history_backfilled_at is not None
        and conversation.history_backfilled_at >= grant.consented_at
    )
    if not completed_limited:
        return False
    # A live callback can be delivering the first accessible message while the
    # older archive is limited. Do not terminate that open ahead of its receipt.
    return not conversation.deliveries.filter(
        source_platform="slack", metadata__participant_hash=conversation.participant_hash,
        status__in=["pending", "processing", "failed", "dead"],
    ).exclude(source_message_id__startswith="history-state:").filter(
        Q(metadata__history_outside_window__isnull=True) | Q(metadata__history_outside_window=False),
    ).filter(
        Q(metadata__history_recovery_superseded__isnull=True) | Q(metadata__history_recovery_superseded=False),
    ).exclude(status="dead", last_error="Private conversation participants changed").exists()


def ready_for_display(conversation, *, now=None, public_key=None):
    """Show acknowledged history immediately while the remaining archive imports."""
    from integrations.services.message_sync.presentation import presentation_scope

    now = now or datetime.now(timezone.utc)
    grant = conversation.grant
    activity = conversation_activity_at(conversation)
    if (
        grant.status != "active" or grant.revoked_at is not None
        or conversation.status != "live"
    ):
        return False
    published = getattr(conversation, "_publication_scope", None) == _publication_key(conversation)
    presented = False
    if getattr(conversation, "_presentation_scope", None):
        oldest = history_oldest_ts(conversation, now=now)
        try:
            delivered = Decimal(str(getattr(conversation, "_presentation_source_ts", "")))
            presented = bool(
                conversation._presentation_scope == presentation_scope(conversation)
                and delivered.is_finite() and delivered > 0
                and (not oldest or delivered >= Decimal(oldest))
                and delivered <= Decimal(str(now.timestamp() + 300))
            )
        except InvalidOperation:
            pass
    if not published and not presented and not history_complete(conversation):
        return False
    if activity is None:
        # A deliberately opened empty DM needs a composer after its first scan.
        # Background discovery and actual old source activity never take this path.
        return bool(public_key and conversation_metadata(conversation).get(OWNER_OPENED_KEY)
                    == owner_open_intent(grant, public_key))
    oldest = history_oldest_ts(conversation, now=now)
    return not oldest or datetime.fromisoformat(activity).timestamp() >= int(oldest)


def _publication_key(conversation):
    """Bind durable publication to the exact owner, consent, source and audience."""
    from integrations.services.slack_dm_mirror import _grant_history_days

    grant = conversation.grant
    value = ":".join(str(v) for v in (
        getattr(grant, "pk", ""), getattr(grant, "user_id", ""), grant.consented_at.isoformat(),
        getattr(grant, "consent_version", ""),
        getattr(grant, "connection_id", ""), getattr(grant, "slack_workspace_id", ""),
        getattr(grant, "slack_user_id", ""),
        getattr(conversation, "slack_workspace_id", ""), getattr(conversation, "slack_conversation_id", ""),
        ",".join(sorted(getattr(conversation, "participant_slack_ids", None) or [])),
        conversation.mlai_channel_id, getattr(conversation, "participant_hash", ""),
        ",".join(sorted(conversation.participant_buzz_pubkeys or [])),
        _grant_history_days(grant),
    ))
    return "slack-import-published-v3:" + hashlib.sha256(value.encode()).hexdigest()


def catalog_payload(conversations, public_key):
    """Expose only mirrors provisioned for this verified device."""
    from integrations.services.slack_channel_mentions import roo_channel_targets

    key = str(public_key or "").lower()
    conversations = [
        conversation for conversation in conversations
        if key and conversation.mlai_channel_id
        and key in (conversation.participant_buzz_pubkeys or [])
    ]
    readiness = {
        id(c): ready_for_display(c, public_key=key)
        for c in conversations
    }
    return [
        {
            "channel_id": str(conversation.mlai_channel_id),
            "slack_conversation_id": conversation.slack_conversation_id,
            "kind": conversation_kind(conversation),
            "last_message_at": conversation_activity_at(conversation),
            "ready_for_display": readiness[id(conversation)],
            "history_complete": history_complete(conversation),
            "history_oldest_ts": history_oldest_ts(conversation),
            "source_archived": bool(
                conversation_metadata(conversation).get("source_archived")
            ),
            **(
                {"mention_targets": targets}
                if (targets := roo_channel_targets(conversation))
                else {}
            ),
            **(
                {"participants": catalog_participants(conversation)}
                if conversation_kind(conversation) in {"im", "mpim"}
                else {}
            ),
        }
        for conversation in conversations
    ]


def retired_catalog_payload(owner, public_key, current_channel_ids):
    """Return owner-scoped ID-only fences for superseded relay registrations.

    Old relay memberships can outlive an adapter registration. These entries
    prevent them being counted as native group chats; they confer no access and
    disclose no historical names, people or message bodies.
    """
    from community_chat.models import CommunityChatDevice
    from integrations.models import SlackDmMirrorConversation, SlackDmMirrorDelivery

    owner_id = getattr(owner, "user_id", None) or owner.pk
    key = str(public_key or "").strip().lower()
    if not key or not CommunityChatDevice.objects.filter(
        user_id=owner_id, public_key=key, status="verified", revoked_at__isnull=True,
    ).exists():
        return []
    rows = SlackDmMirrorDelivery.objects.filter(
        conversation__grant__user_id=owner_id, source_platform="buzz",
        source_message_id__startswith="registration-state:",
        metadata__registration_control=True,
    ).order_by("metadata__channel_id").values_list("metadata__channel_id", flat=True).distinct()
    known_ids = {str(value) for value in rows if value}
    # Pre-ledger mirrors can retain a current relay ID after participant cleanup.
    # These ID-only owner fences survive paused/disconnected grants as well.
    known_ids.update(str(value) for value in SlackDmMirrorConversation.objects.filter(
        grant__user_id=owner_id, mlai_channel_id__isnull=False,
    ).values_list("mlai_channel_id", flat=True))
    return [
        {"channel_id": channel_id, "kind": "mpim", "ready_for_display": False,
         "last_message_at": None, "history_oldest_ts": ""}
        for channel_id in sorted(known_ids - set(current_channel_ids))
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
