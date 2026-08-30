"""Durable adapter-registration ledger for owner-consented Slack DM mirrors.

The adapter API is necessarily outside the database transaction.  Each POST
therefore gets its own content-free database row before network I/O starts, and
each DELETE is driven from that row until its outcome is reconciled.  Replaying
POST and DELETE is safe because both adapter operations are idempotent.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgePlatform,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.community_bridge.buzz import BuzzBridgeClient

PRIVATE_REGISTRATION_REVOCATION_PENDING = (
    "MLAI Chat private registration revocation is pending"
)
REGISTRATION_STATE_PREFIX = "registration-state:"
REGISTRATION_STATE_PROVISIONING = "provisioning"
REGISTRATION_STATE_AMBIGUOUS = "ambiguous"
REGISTRATION_STATE_ACTIVE = "active"
REGISTRATION_STATE_CLEANUP_PENDING = "cleanup_pending"
REGISTRATION_STATE_CLEANUP_PROCESSING = "cleanup_processing"
REGISTRATION_STATE_CLEANED = "cleaned"
REGISTRATION_CLEANUP_LEASE_SECONDS = 300


class RegistrationCleanupPending(RuntimeError):
    """Raised while a durable private-registration cleanup still needs work."""


def conversation_owner_device_pubkeys(
    conversation: SlackDmMirrorConversation,
) -> set[str]:
    """Return owner device keys without the deterministic shadow identities."""

    identity_map = conversation.participant_identity_map or {}
    shadow_pubkeys = {
        str(public_key or "").lower()
        for slack_user_id, public_key in identity_map.items()
        if slack_user_id != conversation.grant.slack_user_id
    }
    return {
        str(public_key or "").lower()
        for public_key in conversation.participant_buzz_pubkeys or []
    } - shadow_pubkeys


def conversation_name(conversation: SlackDmMirrorConversation) -> str:
    """Build the existing user-facing mirror name from stored Slack profiles."""

    profiles = conversation.participant_profiles or {}
    counterpart_ids = [
        value
        for value in conversation.participant_slack_ids or []
        if value != conversation.grant.slack_user_id
    ]
    if not counterpart_ids:
        return "Slack DM"
    display_names = []
    for counterpart_id in counterpart_ids:
        profile = profiles.get(counterpart_id) or {}
        display_names.append(str(profile.get("display_name") or counterpart_id))
    return ", ".join(display_names)[:255]


def grant_consent_generation(grant: SlackDmMirrorGrant) -> str:
    """Return the durable consent epoch used to reject stale POST results."""

    return grant.consented_at.isoformat()


def registration_rows_for_grant(
    grant_id: int,
    *,
    for_update: bool = False,
):
    rows = SlackDmMirrorDelivery.objects.filter(
        conversation__grant_id=grant_id,
        source_platform=CommunityBridgePlatform.BUZZ,
        source_message_id__startswith=REGISTRATION_STATE_PREFIX,
        operation=CommunityBridgeDeliveryType.CREATE,
    ).select_related("conversation")
    if for_update:
        # Callers already hold the grant (and, where needed, conversation)
        # boundary. Lock only the control row so the JOIN cannot acquire a
        # hidden conversation lock in delivery-first order.
        rows = rows.select_for_update(of=("self",))
    return rows.order_by("id")


def registration_state(row: SlackDmMirrorDelivery) -> str:
    return str((row.metadata or {}).get("registration_state") or "").strip()


def registration_channel_id(row: SlackDmMirrorDelivery) -> str:
    return str((row.metadata or {}).get("channel_id") or "").strip()


def registration_participant_hash(row: SlackDmMirrorDelivery) -> str:
    return str((row.metadata or {}).get("participant_hash") or "").strip()


def registration_slack_participant_ids(row: SlackDmMirrorDelivery) -> list[str]:
    return sorted(
        {
            str(value or "").strip()
            for value in (row.metadata or {}).get("participant_slack_ids") or []
            if str(value or "").strip()
        }
    )


def registration_generation(row: SlackDmMirrorDelivery) -> str:
    return str((row.metadata or {}).get("consent_generation") or "").strip()


def registration_is_attempt(row: SlackDmMirrorDelivery) -> bool:
    return bool((row.metadata or {}).get("provision_attempt"))


def create_registration_row_locked(
    conversation: SlackDmMirrorConversation,
    *,
    grant: SlackDmMirrorGrant,
    state: str,
    participant_pubkeys: list[str],
    callback_author_pubkeys: list[str],
    participant_hash: str,
    conversation_name_value: str,
    channel_id: str = "",
    provision_attempt: bool,
    legacy: bool = False,
) -> SlackDmMirrorDelivery:
    """Persist one exact registration/attempt before any adapter network call."""

    now = timezone.now()
    return SlackDmMirrorDelivery.objects.create(
        conversation=conversation,
        source_platform=CommunityBridgePlatform.BUZZ,
        source_message_id=f"{REGISTRATION_STATE_PREFIX}{uuid.uuid4().hex}",
        source_author_id="",
        operation=CommunityBridgeDeliveryType.CREATE,
        encrypted_text="",
        metadata={
            "registration_control": True,
            "registration_state": state,
            "consent_generation": grant_consent_generation(grant),
            "participant_hash": str(participant_hash or ""),
            "participant_slack_ids": sorted(
                {
                    str(value or "").strip()
                    for value in conversation.participant_slack_ids or []
                    if str(value or "").strip()
                }
            ),
            "participant_pubkeys": list(participant_pubkeys),
            "callback_author_pubkeys": list(callback_author_pubkeys),
            "conversation_name": str(conversation_name_value or ""),
            "channel_id": str(channel_id or ""),
            "provision_attempt": bool(provision_attempt),
            "legacy": bool(legacy),
        },
        status=CommunityBridgeDeliveryStatus.COMPLETED,
        available_at=now,
        completed_at=now,
    )


def ensure_current_registration_row_locked(
    conversation: SlackDmMirrorConversation,
    grant: SlackDmMirrorGrant,
) -> SlackDmMirrorDelivery | None:
    """Backfill a durable active row for a pre-ledger/current channel."""

    channel_id = str(conversation.mlai_channel_id or "").strip()
    if not channel_id:
        return None
    rows = list(
        SlackDmMirrorDelivery.objects.select_for_update()
        .filter(
            conversation=conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id__startswith=REGISTRATION_STATE_PREFIX,
            operation=CommunityBridgeDeliveryType.CREATE,
        )
        .order_by("id")
    )
    existing = next(
        (
            row
            for row in reversed(rows)
            if registration_channel_id(row) == channel_id
            and registration_state(row) == REGISTRATION_STATE_ACTIVE
            and registration_generation(row) == grant_consent_generation(grant)
            and registration_participant_hash(row) == conversation.participant_hash
            and registration_slack_participant_ids(row)
            == sorted(conversation.participant_slack_ids or [])
        ),
        None,
    )
    if existing is not None:
        return existing
    same_channel = next(
        (
            row
            for row in reversed(rows)
            if registration_channel_id(row) == channel_id
            and registration_state(row) != REGISTRATION_STATE_CLEANED
        ),
        None,
    )
    if same_channel is not None and (
        grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
        or conversation.status != SlackDmMirrorConversationStatus.LIVE
    ):
        # Revocation/retry must not mint an ACTIVE alias for a row whose DELETE
        # already owns the network lease.  Active/live repair below is the one
        # case that deliberately creates an authority row for restoration.
        return same_channel
    return create_registration_row_locked(
        conversation,
        grant=grant,
        state=REGISTRATION_STATE_ACTIVE,
        participant_pubkeys=list(conversation.participant_buzz_pubkeys or []),
        callback_author_pubkeys=sorted(conversation_owner_device_pubkeys(conversation)),
        participant_hash=conversation.participant_hash,
        conversation_name_value=conversation_name(conversation),
        channel_id=channel_id,
        provision_attempt=False,
        legacy=True,
    )


def save_registration_state_locked(
    row: SlackDmMirrorDelivery,
    state: str,
    *,
    channel_id: str | None = None,
    last_error: str = "",
    available_at=None,
) -> None:
    metadata = dict(row.metadata or {})
    metadata["registration_state"] = state
    if channel_id is not None:
        metadata["channel_id"] = str(channel_id or "")
    row.metadata = metadata
    row.encrypted_text = ""
    row.last_error = str(last_error or "")[:2000]
    if state in {
        REGISTRATION_STATE_CLEANUP_PENDING,
        REGISTRATION_STATE_CLEANUP_PROCESSING,
    }:
        row.status = (
            CommunityBridgeDeliveryStatus.PROCESSING
            if state == REGISTRATION_STATE_CLEANUP_PROCESSING
            else CommunityBridgeDeliveryStatus.PENDING
        )
        row.completed_at = None
    else:
        row.status = CommunityBridgeDeliveryStatus.COMPLETED
        row.completed_at = timezone.now()
    row.available_at = available_at or timezone.now()
    row.save(
        update_fields=(
            "metadata",
            "encrypted_text",
            "status",
            "available_at",
            "completed_at",
            "last_error",
            "updated_at",
        )
    )


def mark_registration_cleanup_pending_locked(
    row: SlackDmMirrorDelivery,
    *,
    reason: str,
    channel_id: str | None = None,
    available_at=None,
) -> None:
    state = registration_state(row)
    previous_channel_id = registration_channel_id(row)
    no_known_channel = not str(channel_id or previous_channel_id).strip()
    resolved_in_flight_post = bool(channel_id and not previous_channel_id)
    if state == REGISTRATION_STATE_CLEANUP_PROCESSING or (
        state
        in {
            REGISTRATION_STATE_PROVISIONING,
            REGISTRATION_STATE_AMBIGUOUS,
        }
        and no_known_channel
    ):
        # The original POST may still be inside its bounded adapter timeout.
        # Replaying POST+DELETE before that request settles could let the
        # original request recreate the registration after cleanup.  A normal
        # response finalizes the row (and makes it immediately eligible); a
        # crashed caller is safely reconciled after this conservative lease.
        # Phase two can wait behind authority locks long after the attempt row
        # was created. Start the ambiguity fence when cleanup first observes
        # an unresolved attempt, not from that potentially stale row time.
        provision_lease = timezone.now() + timedelta(
            seconds=REGISTRATION_CLEANUP_LEASE_SECONDS
        )
        if available_at is None or available_at < provision_lease:
            available_at = provision_lease
    elif (
        state == REGISTRATION_STATE_CLEANUP_PENDING
        and row.available_at > timezone.now()
        and not resolved_in_flight_post
        and (available_at is None or available_at < row.available_at)
    ):
        # Repeated revoke/reactivation attempts must not shorten the original
        # network lease and reintroduce the late-POST-after-DELETE race.
        available_at = row.available_at
    save_registration_state_locked(
        row,
        REGISTRATION_STATE_CLEANUP_PENDING,
        channel_id=channel_id,
        last_error=reason,
        available_at=available_at,
    )


def registration_cleanup_pending_locked(
    grant_id: int,
    *,
    conversation_id: int | None = None,
) -> bool:
    rows = registration_rows_for_grant(grant_id, for_update=True)
    if conversation_id is not None:
        rows = rows.filter(conversation_id=conversation_id)
    return any(
        registration_state(row)
        in {
            REGISTRATION_STATE_CLEANUP_PENDING,
            REGISTRATION_STATE_CLEANUP_PROCESSING,
        }
        for row in rows
    )


def update_registration_cleanup_summary_locked(grant: SlackDmMirrorGrant) -> None:
    pending = registration_cleanup_pending_locked(grant.pk)
    next_error = PRIVATE_REGISTRATION_REVOCATION_PENDING if pending else ""
    if pending or grant.last_error == PRIVATE_REGISTRATION_REVOCATION_PENDING:
        grant.last_error = next_error
        grant.save(update_fields=("last_error", "updated_at"))


def prepare_registration_cleanup_locked(
    grant: SlackDmMirrorGrant,
    conversations: list[SlackDmMirrorConversation],
    *,
    reason: str,
) -> None:
    """Make every known or ambiguous registration durably cleanup-pending."""

    for conversation in conversations:
        ensure_current_registration_row_locked(conversation, grant)
    for row in registration_rows_for_grant(grant.pk, for_update=True):
        state = registration_state(row)
        if state in {
            REGISTRATION_STATE_CLEANED,
            REGISTRATION_STATE_CLEANUP_PROCESSING,
        }:
            continue
        mark_registration_cleanup_pending_locked(row, reason=reason)
    update_registration_cleanup_summary_locked(grant)


def prepare_conversation_registration_cleanup_locked(
    grant: SlackDmMirrorGrant,
    conversation: SlackDmMirrorConversation,
    *,
    reason: str,
) -> None:
    """Fence every registration belonging to one changed conversation intent."""

    ensure_current_registration_row_locked(conversation, grant)
    for row in registration_rows_for_grant(grant.pk, for_update=True).filter(
        conversation_id=conversation.pk
    ):
        state = registration_state(row)
        if state in {
            REGISTRATION_STATE_CLEANED,
            REGISTRATION_STATE_CLEANUP_PROCESSING,
        }:
            continue
        mark_registration_cleanup_pending_locked(row, reason=reason)
    update_registration_cleanup_summary_locked(grant)


def prepare_generation_transition_locked(
    grant: SlackDmMirrorGrant,
    conversations: list[SlackDmMirrorConversation],
) -> None:
    """Retire only uncertain/orphaned rows before advancing OAuth consent.

    A known current registration can remain in place across reauthorization.
    Its ledger generation is adopted atomically with the new consent below.
    Every in-flight, ambiguous, or superseded attempt must be reconciled first.
    """

    conversation_by_id = {
        conversation.pk: conversation for conversation in conversations
    }
    for conversation in conversations:
        ensure_current_registration_row_locked(conversation, grant)
    for row in registration_rows_for_grant(grant.pk, for_update=True):
        state = registration_state(row)
        if state == REGISTRATION_STATE_CLEANED:
            continue
        conversation = conversation_by_id.get(row.conversation_id)
        valid_current = bool(
            state == REGISTRATION_STATE_ACTIVE
            and conversation is not None
            and registration_channel_id(row) == str(conversation.mlai_channel_id or "")
            and registration_participant_hash(row) == conversation.participant_hash
            and registration_slack_participant_ids(row)
            == sorted(conversation.participant_slack_ids or [])
        )
        if valid_current:
            continue
        if state != REGISTRATION_STATE_CLEANUP_PROCESSING:
            mark_registration_cleanup_pending_locked(
                row,
                reason="Reconcile private registration before new consent",
            )
    update_registration_cleanup_summary_locked(grant)


def adopt_current_registration_generation_locked(grant: SlackDmMirrorGrant) -> None:
    """Move known current rows to the newly committed consent generation."""

    conversations = {
        conversation.pk: conversation
        for conversation in SlackDmMirrorConversation.objects.select_for_update().filter(
            grant=grant
        )
    }
    for row in registration_rows_for_grant(grant.pk, for_update=True):
        if registration_state(row) != REGISTRATION_STATE_ACTIVE:
            continue
        conversation = conversations.get(row.conversation_id)
        if (
            conversation is None
            or registration_channel_id(row) != str(conversation.mlai_channel_id or "")
            or registration_participant_hash(row) != conversation.participant_hash
            or registration_slack_participant_ids(row)
            != sorted(conversation.participant_slack_ids or [])
        ):
            mark_registration_cleanup_pending_locked(
                row,
                reason="Private registration no longer matches the active consent",
            )
            continue
        metadata = dict(row.metadata or {})
        metadata["consent_generation"] = grant_consent_generation(grant)
        row.metadata = metadata
        row.save(update_fields=("metadata", "updated_at"))
    update_registration_cleanup_summary_locked(grant)


def registration_request(row: SlackDmMirrorDelivery) -> dict[str, Any]:
    metadata = row.metadata or {}
    return {
        "participant_pubkeys": [
            str(value or "").strip().lower()
            for value in metadata.get("participant_pubkeys") or []
        ],
        "callback_author_pubkeys": [
            str(value or "").strip().lower()
            for value in metadata.get("callback_author_pubkeys") or []
        ],
        "conversation_name": str(metadata.get("conversation_name") or ""),
    }


def _authoritative_registration_locked(
    grant: SlackDmMirrorGrant,
    conversation: SlackDmMirrorConversation,
    *,
    channel_id: str,
) -> SlackDmMirrorDelivery | None:
    """Return the exact current registration that must survive cleanup."""

    if (
        grant.status != SlackDmMirrorGrantStatus.ACTIVE
        or grant.revoked_at is not None
        or str(conversation.mlai_channel_id or "") != channel_id
    ):
        return None
    ensure_current_registration_row_locked(conversation, grant)
    return next(
        (
            row
            for row in reversed(
                list(registration_rows_for_grant(grant.pk, for_update=True))
            )
            if row.conversation_id == conversation.pk
            and registration_state(row) == REGISTRATION_STATE_ACTIVE
            and registration_channel_id(row) == channel_id
            and registration_generation(row) == grant_consent_generation(grant)
            and registration_participant_hash(row) == conversation.participant_hash
            and registration_slack_participant_ids(row)
            == sorted(conversation.participant_slack_ids or [])
        ),
        None,
    )


def _registration_cleanup_disposition_locked(
    grant: SlackDmMirrorGrant,
    conversation: SlackDmMirrorConversation,
    row: SlackDmMirrorDelivery,
    *,
    channel_id: str,
) -> tuple[str, dict[str, Any] | None]:
    """Return delete, owned, or defer plus an authoritative restore snapshot."""

    authoritative = _authoritative_registration_locked(
        grant,
        conversation,
        channel_id=channel_id,
    )
    if authoritative is not None and authoritative.pk != row.pk:
        return (
            "owned",
            {
                "row_id": authoritative.pk,
                "generation": registration_generation(authoritative),
                "participant_hash": registration_participant_hash(authoritative),
                "request": registration_request(authoritative),
            },
        )
    if grant.status != SlackDmMirrorGrantStatus.REVOKED:
        participant_hash = registration_participant_hash(row)
        now = timezone.now()
        colliding_attempts = [
            other
            for other in registration_rows_for_grant(grant.pk, for_update=True)
            if other.conversation_id == conversation.pk
            and other.pk != row.pk
            and registration_participant_hash(other) == participant_hash
            and registration_state(other) == REGISTRATION_STATE_PROVISIONING
        ]
        if any(
            other.updated_at
            >= now - timedelta(seconds=REGISTRATION_CLEANUP_LEASE_SECONDS)
            for other in colliding_attempts
        ):
            return "defer", None
        for other in colliding_attempts:
            # Age only tells us the network call should have returned; it does
            # not revoke that caller's authority to finalize. Fence the row
            # durably before DELETE so a delayed caller cannot promote the
            # now-unregistered idempotent channel.
            mark_registration_cleanup_pending_locked(
                other,
                reason="Superseded private registration lease expired",
                available_at=now,
            )
    return "delete", None


def _mark_channel_registration_cleaned_locked(
    grant: SlackDmMirrorGrant,
    *,
    channel_id: str,
    completed_row_id: int,
) -> None:
    now = timezone.now()
    for row in registration_rows_for_grant(grant.pk, for_update=True):
        if registration_channel_id(row) != channel_id:
            continue
        state = registration_state(row)
        if state == REGISTRATION_STATE_ACTIVE or (
            state == REGISTRATION_STATE_CLEANUP_PROCESSING
            and row.pk != completed_row_id
        ):
            continue
        if (
            row.pk != completed_row_id
            and state == REGISTRATION_STATE_CLEANUP_PENDING
            and row.available_at > now
        ):
            # A sibling success proves the channel's current state, but cannot
            # prove a timed-out POST/DELETE will not arrive later. Preserve
            # that attempt's full ambiguity lease so reactivation remains
            # fenced until its own outcome is reconciled.
            continue
        save_registration_state_locked(
            row,
            REGISTRATION_STATE_CLEANED,
            channel_id=channel_id,
        )
    conversations = list(
        SlackDmMirrorConversation.objects.select_for_update().filter(
            grant=grant,
            mlai_channel_id=channel_id,
        )
    )
    for conversation in conversations:
        if (
            grant.status != SlackDmMirrorGrantStatus.ACTIVE
            or conversation.status != SlackDmMirrorConversationStatus.LIVE
        ):
            conversation.mlai_channel_id = None
            conversation.save(update_fields=("mlai_channel_id", "updated_at"))


def _record_registration_cleanup_failure(
    grant_id: int,
    row_id: int,
    exc: Exception,
    *,
    channel_id: str = "",
    operation: str = "inspect",
) -> None:
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant_id)
        row = SlackDmMirrorDelivery.objects.select_for_update().get(pk=row_id)
        row.attempts = min(row.attempts + 1, 32_767)
        metadata = dict(row.metadata or {})
        metadata["registration_state"] = REGISTRATION_STATE_CLEANUP_PENDING
        if channel_id:
            metadata["channel_id"] = str(channel_id)
        metadata["cleanup_operation"] = operation
        row.metadata = metadata
        row.status = CommunityBridgeDeliveryStatus.PENDING
        row.available_at = timezone.now() + timedelta(
            seconds=min(60, 2 ** min(row.attempts, 5))
        )
        if operation in {"resolve", "restore", "delete"}:
            # A timed-out POST or DELETE can still begin server-side after the
            # client releases its locks. Preserve the full ambiguity lease
            # before a retry can mutate the same deterministic channel; an old
            # late DELETE must not remove a replacement registration.
            ambiguity_lease = timezone.now() + timedelta(
                seconds=REGISTRATION_CLEANUP_LEASE_SECONDS
            )
            if row.available_at < ambiguity_lease:
                row.available_at = ambiguity_lease
        row.completed_at = None
        row.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
        row.encrypted_text = ""
        row.save(
            update_fields=(
                "attempts",
                "metadata",
                "status",
                "available_at",
                "completed_at",
                "last_error",
                "encrypted_text",
                "updated_at",
            )
        )
        grant.last_error = PRIVATE_REGISTRATION_REVOCATION_PENDING
        grant.save(update_fields=("last_error", "updated_at"))


def _claim_registration_cleanup(grant_id: int) -> dict[str, Any] | None:
    now = timezone.now()
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant_id)
        stale_rows = list(
            registration_rows_for_grant(grant_id, for_update=True).filter(
                status=CommunityBridgeDeliveryStatus.PROCESSING,
                updated_at__lt=now
                - timedelta(seconds=REGISTRATION_CLEANUP_LEASE_SECONDS),
            )
        )
        for stale in stale_rows:
            mark_registration_cleanup_pending_locked(
                stale,
                reason="Recovered interrupted private registration cleanup",
                available_at=now,
            )
        row = (
            registration_rows_for_grant(grant_id, for_update=True)
            .filter(
                status=CommunityBridgeDeliveryStatus.PENDING,
                available_at__lte=now,
            )
            .first()
        )
        if row is None:
            update_registration_cleanup_summary_locked(grant)
            return None
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=row.conversation_id
        )
        save_registration_state_locked(
            row,
            REGISTRATION_STATE_CLEANUP_PROCESSING,
            channel_id=registration_channel_id(row),
            last_error="",
        )
        return {
            "grant_id": grant.pk,
            "row_id": row.pk,
            "conversation_id": conversation.pk,
            "channel_id": registration_channel_id(row),
            "request": registration_request(row),
        }


def _execute_registration_cleanup(claim: dict[str, Any]) -> str:
    channel_id = str(claim["channel_id"] or "")
    operation = "inspect"
    try:
        with transaction.atomic():
            # Device revocation takes the same grant->conversation->device
            # boundary. Keeping those rows locked across bounded adapter I/O
            # prevents a cleanup replay or authority restore from recreating a
            # registration after device revoke has returned.
            grant = SlackDmMirrorGrant.objects.select_for_update().get(
                pk=claim["grant_id"]
            )
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=claim["conversation_id"]
            )
            locked_devices = list(
                CommunityChatDevice.objects.select_for_update()
                .filter(user_id=grant.user_id)
                .order_by("public_key")
            )
            row = SlackDmMirrorDelivery.objects.select_for_update().get(
                pk=claim["row_id"]
            )
            if registration_state(row) != REGISTRATION_STATE_CLEANUP_PROCESSING:
                update_registration_cleanup_summary_locked(grant)
                return "retry"
            if not channel_id:
                operation = "resolve"
                request = claim["request"]
                provisioned = BuzzBridgeClient.provision_private_conversation(
                    request["participant_pubkeys"],
                    callback_author_pubkeys=request["callback_author_pubkeys"],
                    conversation_name=request["conversation_name"],
                )
                channel_id = str(provisioned["channel_id"])
            disposition, authority = _registration_cleanup_disposition_locked(
                grant,
                conversation,
                row,
                channel_id=channel_id,
            )
            if disposition == "defer":
                mark_registration_cleanup_pending_locked(
                    row,
                    reason="Waiting for the current private registration attempt",
                    channel_id=channel_id,
                    available_at=timezone.now() + timedelta(seconds=2),
                )
                update_registration_cleanup_summary_locked(grant)
                return "deferred"
            save_registration_state_locked(
                row,
                REGISTRATION_STATE_CLEANUP_PROCESSING,
                channel_id=channel_id,
            )
            if disposition == "owned":
                operation = "restore"
                authority = authority or {}
                authority_request = authority.get("request") or {}
                active_device_keys = {
                    device.public_key.lower()
                    for device in locked_devices
                    if device.status == DeviceBindingStatus.VERIFIED
                    and device.revoked_at is None
                }
                callback_author_pubkeys = sorted(
                    {
                        str(value or "").strip().lower()
                        for value in authority_request.get("callback_author_pubkeys")
                        or []
                        if str(value or "").strip()
                    }
                )
                current = _authoritative_registration_locked(
                    grant,
                    conversation,
                    channel_id=channel_id,
                )
                if (
                    current is None
                    or current.pk != authority.get("row_id")
                    or registration_generation(current) != authority.get("generation")
                    or registration_participant_hash(current)
                    != authority.get("participant_hash")
                    or not set(callback_author_pubkeys).issubset(active_device_keys)
                ):
                    if current is not None:
                        mark_registration_cleanup_pending_locked(
                            current,
                            reason="Private registration device authority changed",
                            channel_id=channel_id,
                            available_at=timezone.now(),
                        )
                    conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
                    conversation.mlai_channel_id = None
                    conversation.save(
                        update_fields=(
                            "status",
                            "mlai_channel_id",
                            "updated_at",
                        )
                    )
                    mark_registration_cleanup_pending_locked(
                        row,
                        reason="Private registration authority changed before restore",
                        channel_id=channel_id,
                        available_at=timezone.now(),
                    )
                    update_registration_cleanup_summary_locked(grant)
                    return "retry"
                provisioned = BuzzBridgeClient.provision_private_conversation(
                    authority_request["participant_pubkeys"],
                    callback_author_pubkeys=callback_author_pubkeys,
                    conversation_name=authority_request["conversation_name"],
                )
                restored_channel_id = str(provisioned["channel_id"])
                if restored_channel_id != channel_id:
                    raise RuntimeError(
                        "MLAI Chat adapter changed an authoritative private channel during cleanup"
                    )
                save_registration_state_locked(
                    row,
                    REGISTRATION_STATE_CLEANED,
                    channel_id=channel_id,
                )
                update_registration_cleanup_summary_locked(grant)
                return "owned"

            operation = "delete"
            BuzzBridgeClient.unregister_private_conversation(channel_id)
            disposition, authority = _registration_cleanup_disposition_locked(
                grant,
                conversation,
                row,
                channel_id=channel_id,
            )
            if disposition == "defer":
                mark_registration_cleanup_pending_locked(
                    row,
                    reason="Registration changed while adapter cleanup completed",
                    channel_id=channel_id,
                    available_at=timezone.now() + timedelta(seconds=2),
                )
                update_registration_cleanup_summary_locked(grant)
                return "deferred"
            if disposition != "owned":
                _mark_channel_registration_cleaned_locked(
                    grant,
                    channel_id=channel_id,
                    completed_row_id=row.pk,
                )
                update_registration_cleanup_summary_locked(grant)
                return "cleaned"
            raise RuntimeError(
                "Private registration authority changed under cleanup locks"
            )
    except Exception as exc:
        _record_registration_cleanup_failure(
            claim["grant_id"],
            claim["row_id"],
            exc,
            channel_id=channel_id,
            operation=operation,
        )
        raise


def reconcile_registration_cleanup(
    grant_id: int,
    *,
    raise_on_pending: bool,
    limit: int = 100,
) -> None:
    """Drain durable cleanup work while serializing each adapter mutation."""

    first_error: Exception | None = None
    for _ in range(max(1, min(int(limit), 500))):
        claim = _claim_registration_cleanup(grant_id)
        if claim is None:
            break
        try:
            outcome = _execute_registration_cleanup(claim)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            break
        if outcome == "deferred":
            break
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=grant_id)
        pending = registration_cleanup_pending_locked(grant_id)
        update_registration_cleanup_summary_locked(grant)
    if raise_on_pending and pending:
        if first_error is not None:
            raise first_error
        raise RegistrationCleanupPending(
            "Previous MLAI Chat private registration cleanup is still pending."
        )


def record_ambiguous_registration_attempt(attempt_id: int, exc: Exception) -> None:
    """Preserve a POST whose response may have been lost for later replay."""

    attempt_stub = SlackDmMirrorDelivery.objects.select_related("conversation").get(
        pk=attempt_id
    )
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(
            pk=attempt_stub.conversation.grant_id
        )
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=attempt_stub.conversation_id
        )
        attempt = SlackDmMirrorDelivery.objects.select_for_update().get(pk=attempt_id)
        if bool(getattr(exc, "permanent", False)):
            save_registration_state_locked(
                attempt,
                REGISTRATION_STATE_CLEANED,
                last_error=f"{exc.__class__.__name__}: adapter rejected registration",
            )
        elif registration_state(attempt) in {
            REGISTRATION_STATE_PROVISIONING,
            REGISTRATION_STATE_AMBIGUOUS,
        }:
            save_registration_state_locked(
                attempt,
                REGISTRATION_STATE_AMBIGUOUS,
                last_error=f"{exc.__class__.__name__}: adapter result is ambiguous",
            )
        if (
            grant.status == SlackDmMirrorGrantStatus.ACTIVE
            and grant.revoked_at is None
            and registration_generation(attempt) == grant_consent_generation(grant)
            and conversation.status == SlackDmMirrorConversationStatus.PROVISIONING
        ):
            conversation.status = SlackDmMirrorConversationStatus.ERROR
            conversation.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            conversation.save(update_fields=("status", "last_error", "updated_at"))


def finalize_registration_attempt(attempt_id: int, *, channel_id: str) -> bool:
    """Promote only the current consent/participant intent; queue every other ID."""

    attempt_stub = SlackDmMirrorDelivery.objects.select_related("conversation").get(
        pk=attempt_id
    )
    with transaction.atomic():
        grant = SlackDmMirrorGrant.objects.select_for_update().get(
            pk=attempt_stub.conversation.grant_id
        )
        conversation = SlackDmMirrorConversation.objects.select_for_update().get(
            pk=attempt_stub.conversation_id
        )
        rows = [
            row
            for row in registration_rows_for_grant(grant.pk, for_update=True)
            if row.conversation_id == conversation.pk
        ]
        attempt = next(row for row in rows if row.pk == attempt_id)
        attempt_hash = registration_participant_hash(attempt)
        attempt_generation = registration_generation(attempt)
        attempt_slack_ids = registration_slack_participant_ids(attempt)
        later_different_intent = any(
            row.pk > attempt.pk
            and registration_is_attempt(row)
            and (
                registration_participant_hash(row) != attempt_hash
                or registration_generation(row) != attempt_generation
                or registration_slack_participant_ids(row) != attempt_slack_ids
            )
            for row in rows
        )
        current_intent = (
            grant.status == SlackDmMirrorGrantStatus.ACTIVE
            and grant.revoked_at is None
            and attempt_generation == grant_consent_generation(grant)
            and attempt_hash == conversation.participant_hash
            and attempt_slack_ids == sorted(conversation.participant_slack_ids or [])
            and not later_different_intent
            and registration_state(attempt) == REGISTRATION_STATE_PROVISIONING
        )
        if current_intent:
            save_registration_state_locked(
                attempt,
                REGISTRATION_STATE_ACTIVE,
                channel_id=channel_id,
            )
            conversation.mlai_channel_id = channel_id
            conversation.status = SlackDmMirrorConversationStatus.LIVE
            conversation.last_error = ""
            conversation.save(
                update_fields=(
                    "mlai_channel_id",
                    "status",
                    "last_error",
                    "updated_at",
                )
            )
            for row in rows:
                if row.pk == attempt.pk:
                    continue
                state = registration_state(row)
                if state == REGISTRATION_STATE_CLEANED:
                    continue
                if state == REGISTRATION_STATE_CLEANUP_PROCESSING:
                    # A DELETE may already be outside the lock.  Its post-I/O
                    # recheck will restore this just-promoted registration.
                    continue
                existing_channel_id = registration_channel_id(row)
                same_semantic_attempt = (
                    registration_is_attempt(row)
                    and registration_participant_hash(row) == attempt_hash
                    and registration_generation(row) == attempt_generation
                    and registration_slack_participant_ids(row) == attempt_slack_ids
                )
                if existing_channel_id == channel_id or (
                    same_semantic_attempt and not existing_channel_id
                ):
                    save_registration_state_locked(
                        row,
                        REGISTRATION_STATE_CLEANED,
                        channel_id=channel_id,
                    )
                elif existing_channel_id or state in {
                    REGISTRATION_STATE_PROVISIONING,
                    REGISTRATION_STATE_AMBIGUOUS,
                    REGISTRATION_STATE_ACTIVE,
                    REGISTRATION_STATE_CLEANUP_PENDING,
                    REGISTRATION_STATE_CLEANUP_PROCESSING,
                }:
                    mark_registration_cleanup_pending_locked(
                        row,
                        reason="Private registration was superseded",
                    )
            update_registration_cleanup_summary_locked(grant)
        else:
            owned_channel = any(
                row.pk != attempt.pk
                and registration_state(row) == REGISTRATION_STATE_ACTIVE
                and registration_channel_id(row) == channel_id
                for row in rows
            ) or (
                conversation.status == SlackDmMirrorConversationStatus.LIVE
                and str(conversation.mlai_channel_id or "") == channel_id
            )
            if owned_channel:
                save_registration_state_locked(
                    attempt,
                    REGISTRATION_STATE_CLEANED,
                    channel_id=channel_id,
                )
            elif registration_state(attempt) == REGISTRATION_STATE_CLEANUP_PROCESSING:
                save_registration_state_locked(
                    attempt,
                    REGISTRATION_STATE_CLEANUP_PROCESSING,
                    channel_id=channel_id,
                    last_error="Stale private registration result is being cleaned",
                )
            else:
                mark_registration_cleanup_pending_locked(
                    attempt,
                    reason="Stale private registration result",
                    channel_id=channel_id,
                )
            if grant.status != SlackDmMirrorGrantStatus.ACTIVE:
                conversation.status = SlackDmMirrorConversationStatus.PAUSED
                conversation.last_error = "Slack DM mirroring is no longer active"
                conversation.save(update_fields=("status", "last_error", "updated_at"))
            update_registration_cleanup_summary_locked(grant)
    return current_intent
