"""Fresh-source repair for the adapter's retired seven-emoji restriction."""
from datetime import datetime, timezone

from django.db.models import Q
from django.utils import timezone as django_timezone

from integrations.services.slack_emoji import slack_reaction_to_emoji

CONTRACT_KEY = "legacy_unicode_reaction_recovery_v1"
# Successful production deploy of mlai-chat PR #145 / 3e7cdc54.
FIX_DEPLOYED_AT = datetime(2026, 9, 10, 16, 52, 6, tzinfo=timezone.utc)
LEGACY_EMOJI = frozenset({"👍", "❤️", "❤", "🎉", "👀", "🚀", "✅"})
LEGACY_ERRORS = (
    "BuzzBridgePermanentError: MLAI Chat adapter rejected delivery with HTTP 400",
    "BuzzBridgePermanentError: MLAI Chat adapter rejected request with HTTP 400",
)


def _valid_audit(audit):
    if not isinstance(audit, dict) or audit.get("contract") != "pre_145_unicode_reaction" or audit.get("error_code") != "adapter_http_400":
        return False
    try:
        failed_at = datetime.fromisoformat(audit.get("failed_at", ""))
        return failed_at.tzinfo is not None and failed_at < FIX_DEPLOYED_AT
    except (TypeError, ValueError):
        return False


def _owner_has_device(conversation):
    from community_chat.models import CommunityChatDevice
    return CommunityChatDevice.objects.filter(
        user_id=conversation.grant.user_id, public_key__in=conversation.participant_buzz_pubkeys or [],
        status="verified", revoked_at__isnull=True,
    ).exists()


def legacy_failure_query():
    """Narrow discovery to failures predating the known adapter compatibility fix."""
    return Q(operation="reaction_add", status="dead", metadata__permanent_failure=True,
             updated_at__lt=FIX_DEPLOYED_AT, last_error__in=LEGACY_ERRORS)


def legacy_failure_eligible(row, conversation):
    """Require a current owner/device boundary and a formerly unsupported emoji."""
    from integrations.services import slack_dm_mirror as dm

    metadata = row.metadata or {}
    emoji = slack_reaction_to_emoji(metadata.get("slack_reaction", ""))
    return bool(
        row.source_platform == "slack" and row.operation == "reaction_add"
        and row.status == "dead" and metadata.get("backfill")
        and metadata.get("permanent_failure") and row.last_error in LEGACY_ERRORS
        and row.updated_at < FIX_DEPLOYED_AT
        and emoji and not emoji.startswith(":") and emoji not in LEGACY_EMOJI
        and metadata.get("participant_hash") == conversation.participant_hash
        and not dm._backfill_delivery_is_outside_history_window(row)
        and _owner_has_device(conversation)
    )


def stage_observed_reaction(row, *, author_id, metadata):
    """Store fresh source metadata while the permanent row stays erased and DEAD."""
    original = row.metadata or {}
    audit = original.get(CONTRACT_KEY)
    if not _valid_audit(audit) or not original.get("history_recovery_scheduled") or row.last_error not in LEGACY_ERRORS:
        return
    if (row.source_platform != "slack" or row.operation != "reaction_add"
            or not metadata.get("history_scan_epoch")
            or metadata.get("slack_reaction") != original.get("slack_reaction")):
        return
    from integrations.services import slack_dm_mirror as dm
    if not row.conversation.deliveries.filter(
        source_platform="slack", source_message_id=dm.HISTORY_MAIN_STATE_ID,
        metadata__scan_epoch=metadata["history_scan_epoch"],
    ).exists():
        # Interleaved head/thread jobs cannot overwrite the archive's evidence.
        return
    # Reactions are reconstructible from source metadata; never save/replay the
    # rejected encrypted payload. The exact scan must qualify before release.
    row.source_author_id = author_id
    row.metadata = {**metadata, "permanent_failure": True,
                    "history_recovery_scheduled": True, CONTRACT_KEY: audit}
    row.encrypted_text = ""
    row.save(update_fields=["source_author_id", "metadata", "encrypted_text", "updated_at"])


def finish_reaction_recovery(conversation, *, scan_epoch, source_limited):
    """Release only fresh observations from a complete unrestricted source scan."""
    from integrations.services import slack_dm_mirror as dm

    if source_limited or not scan_epoch:
        return
    rows = list(conversation.deliveries.select_for_update().filter(
        source_platform="slack", operation="reaction_add", status="dead",
        metadata__permanent_failure=True, metadata__history_recovery_scheduled=True,
        **{f"metadata__{CONTRACT_KEY}__isnull": False},
    ).defer("encrypted_text"))
    if not rows:
        return
    registration = dm._ensure_current_registration_row_locked(conversation, conversation.grant)
    if (not _owner_has_device(conversation) or registration is None
            or dm._registration_state(registration) != dm.REGISTRATION_STATE_ACTIVE):
        return
    now = django_timezone.now()
    for row in rows:
        row.conversation = conversation
        metadata = dict(row.metadata or {})
        audit = metadata.get(CONTRACT_KEY)
        if not _valid_audit(audit) or row.last_error not in LEGACY_ERRORS or metadata.get("participant_hash") != conversation.participant_hash:
            continue
        if dm._backfill_delivery_is_outside_history_window(row, now=now):
            dm._complete_outside_history_window_delivery_locked(row, now=now)
            continue
        observed = metadata.get("history_scan_epoch") == scan_epoch
        emoji = slack_reaction_to_emoji(metadata.get("slack_reaction", ""))
        if observed and (not emoji or emoji.startswith(":") or emoji in LEGACY_EMOJI):
            continue
        metadata.pop("permanent_failure", None)
        metadata.pop("history_recovery_scheduled", None)
        metadata[CONTRACT_KEY] = {**audit, "qualified_at": now.isoformat(),
                                 "outcome": "fresh_source_observed" if observed else "source_absent"}
        row.metadata = metadata
        row.encrypted_text = emoji if observed else ""
        row.status = "pending" if observed else "dead"
        if not observed:
            row.metadata["history_recovery_superseded"] = True
        row.last_error = "" if observed else "Superseded by qualified source recovery"
        row.attempts = 0
        row.available_at = now
        row.save(update_fields=["metadata", "encrypted_text", "status", "last_error", "attempts", "available_at", "updated_at"])
