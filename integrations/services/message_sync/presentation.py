"""Publish delivered private messages without waiting for the entire archive.

This is a presentation receipt, never a claim of complete Slack coverage. The
existing relay owns message bodies; only its successful delivery acknowledgement
can create this receipt under the grant/conversation authority locks.
"""

import hashlib

from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from integrations.models import BridgeSyncState, SlackDmMirrorDelivery
from integrations.services.slack_oauth_authority import connection_slack_oauth_generation
from .publication import _audience, _boundary

PRESENTATION_KEY = "presentation"


def presentation_scope(conversation):
    """Fence partial availability by consent, source, room, audience and OAuth."""
    value = f"{_audience(conversation)['scope']}:{connection_slack_oauth_generation(conversation.grant.connection)}"
    return "slack-import-presented-v1:" + hashlib.sha256(value.encode()).hexdigest()


def _presentation_boundary(conversation):
    return {
        **_boundary(conversation),
        "oauth_generation": connection_slack_oauth_generation(conversation.grant.connection),
    }


def record_presentation_locked(conversation, deliveries):
    """Expose an acknowledged create in this exact authorized room and window.

    Call only from the guarded delivery writer, after persisting the relay's
    acknowledgement in the same transaction. Historical/tombstone rows must not
    be used to infer a new room's availability.
    """
    from integrations.services.slack_dm_mirror import _grant_history_days, _slack_ts_sort_key, SlackDmMirrorError

    grant = conversation.grant
    if (grant.status != "active" or grant.revoked_at is not None
            or conversation.status != "live" or not conversation.mlai_channel_id
            or not conversation.participant_hash):
        return False
    now = timezone.now()
    days = _grant_history_days(grant)
    floor = max(0, int(now.timestamp()) - days * 86400) if days else 0
    acknowledged = []
    for delivery in deliveries:
        metadata = delivery.metadata or {}
        if (delivery.conversation_id != conversation.pk or delivery.source_platform != "slack"
                or delivery.operation != "create" or delivery.status != "completed"
                or delivery.completed_at is None or delivery.completed_at < grant.consented_at
                or metadata.get("participant_hash") != conversation.participant_hash
                or metadata.get("destination_channel_id") != str(conversation.mlai_channel_id)
                or not metadata.get("destination_message_id")
                or any(metadata.get(key) for key in (
                    "history_outside_window", "history_recovery_superseded", "dependency_superseded",
                ))):
            continue
        try:
            stamp = _slack_ts_sort_key(delivery.source_message_id)
        except SlackDmMirrorError:
            continue
        if floor <= stamp[0] <= int(now.timestamp()) + 300:
            acknowledged.append((stamp, delivery.source_message_id))
    if not acknowledged:
        return False
    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    if state is None:
        return False
    scope = presentation_scope(conversation)
    proof = (state.verified_ranges or {}).get(PRESENTATION_KEY) or {}
    source_ts = max(acknowledged)[1]
    if proof.get("scope") == scope:
        try:
            if _slack_ts_sort_key(proof.get("source_message_ts")) >= _slack_ts_sort_key(source_ts):
                return True
        except SlackDmMirrorError:
            pass
    state.verified_ranges = {
        **(state.verified_ranges or {}),
        PRESENTATION_KEY: {
            **_presentation_boundary(conversation), **_audience(conversation),
            "scope": scope, "source_message_ts": source_ts,
            "published_at": now.isoformat(),
        },
    }
    state.save(update_fields=["verified_ranges"])
    return True


def presentation_for_transition(conversation):
    """Freeze a partial receipt for an authorized stable-room device change."""
    from integrations.services.slack_dm_registration_ledger import (
        REGISTRATION_STATE_PREFIX, grant_consent_generation,
        registration_generation, registration_slack_participant_ids,
    )

    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    proof = dict((state.verified_ranges or {}).get(PRESENTATION_KEY) or {}) if state else {}
    if not proof or any(proof.get(key) != value for key, value in _presentation_boundary(conversation).items()):
        return None
    if proof.get("scope") == presentation_scope(conversation):
        return proof
    for attempt in SlackDmMirrorDelivery.objects.filter(
        conversation=conversation, source_message_id__startswith=REGISTRATION_STATE_PREFIX,
        metadata__private_audience__presentation_proof=proof,
    ):
        if (registration_generation(attempt) == grant_consent_generation(conversation.grant)
                and registration_slack_participant_ids(attempt) == sorted(conversation.participant_slack_ids or [])
                and (attempt.metadata or {}).get("private_audience", {}).get("channel_id") == str(conversation.mlai_channel_id)):
            return proof
    return None


def rebind_presentation_locked(conversation, registration):
    """Retain a frozen receipt only after the relay audience CAS has succeeded."""
    proof = (registration.metadata or {}).get("private_audience", {}).get("presentation_proof")
    if not proof or any(proof.get(key) != value for key, value in _presentation_boundary(conversation).items()):
        return
    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    if state is None or (state.verified_ranges or {}).get(PRESENTATION_KEY) != proof:
        return
    state.verified_ranges = {
        **state.verified_ranges,
        PRESENTATION_KEY: {**proof, **_audience(conversation), "scope": presentation_scope(conversation)},
    }
    state.save(update_fields=["verified_ranges"])


def recover_presentation(grant, authority, device, source_id, *, before_id=None):
    """Recover pre-upgrade receipts in bounded worker turns, without Slack I/O.

    Old metadata alone cannot identify the destination room. Ask the relay's
    existing receipt API for up to 20 acknowledged creates, then stamp the exact
    room only for IDs the relay confirms. An exhausted batch advances the next
    worker turn so deleted or replaced-room receipts cannot starve older rows.
    """
    from integrations.services import slack_dm_mirror as dm

    candidate = grant.conversations.filter(
        slack_conversation_id=source_id, status="live",
    ).first()
    if candidate is None or not candidate.mlai_channel_id:
        return False, None
    scopes = dm._history_required_scopes(source_id, kind=dm.conversation_kind(candidate))
    with transaction.atomic():
        conversation, _ = dm._locked_history_write_context(candidate.pk, grant.pk, authority, scopes)
        if (device.public_key not in (conversation.participant_buzz_pubkeys or [])
                or dm._locked_active_verified_device(grant.user_id, device.public_key) is None):
            return False, None
        registration = dm._ensure_current_registration_row_locked(conversation, conversation.grant)
        if (registration is None or dm._registration_state(registration) != dm.REGISTRATION_STATE_ACTIVE
                or dm._registration_channel_id(registration) != str(conversation.mlai_channel_id)):
            return False, None
        rows = SlackDmMirrorDelivery.objects.filter(
            conversation=conversation, source_platform="slack", operation="create", status="completed",
            completed_at__gte=conversation.grant.consented_at,
            metadata__participant_hash=conversation.participant_hash,
        )
        for key in ("history_outside_window", "history_recovery_superseded", "dependency_superseded"):
            rows = rows.filter(Q(**{f"metadata__{key}__isnull": True}) | Q(**{f"metadata__{key}": False}))
        if before_id is not None:
            rows = rows.filter(pk__lt=before_id)
        candidates = list(rows.order_by("-pk")[:20])
        if not candidates:
            return False, None
        now = timezone.now()
        days = dm._grant_history_days(conversation.grant)
        floor = max(0, int(now.timestamp()) - days * 86400) if days else 0
        eligible = []
        for row in candidates:
            try:
                stamp = dm._slack_ts_sort_key(row.source_message_id)
            except dm.SlackDmMirrorError:
                continue
            if floor <= stamp[0] <= int(now.timestamp()) + 300:
                eligible.append(row)
        receipts = dm.BuzzBridgeClient.private_delivery_receipts(
            str(conversation.mlai_channel_id), [str(row.pk) for row in eligible],
        ) if eligible else {}
        confirmed = []
        for row in eligible:
            receipt = receipts.get(str(row.pk))
            if not receipt:
                continue
            row.metadata = {
                **row.metadata, "destination_channel_id": str(conversation.mlai_channel_id),
                "destination_message_id": receipt["message_id"],
            }
            row.save(update_fields=["metadata", "updated_at"])
            confirmed.append(row)
        if confirmed and record_presentation_locked(conversation, confirmed):
            # Older resets may have cleared recency even though the relay still
            # has this exact room's messages. Restore only acknowledged source
            # activity; import time must never masquerade as recent Slack usage.
            latest = max((row.source_message_id for row in confirmed), key=dm._slack_ts_sort_key)
            dm._advance_latest_synced_ts(conversation, latest)
            conversation.save(update_fields=["latest_synced_ts", "updated_at"])
            return True, None
        return False, candidates[-1].pk if len(candidates) == 20 else None
