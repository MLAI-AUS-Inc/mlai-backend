"""Preserve canonical private rooms and source checkpoints across device changes."""
from django.conf import settings


def enabled():
    """Require the relay audience protocol before opting into stable rooms."""
    return bool(getattr(settings, "MESSAGE_SYNC_STABLE_PRIVATE_ROOMS", False))


def rebind_history(conversation, registration):
    """Rebind local authority, retaining source cursors and delivered event IDs.

    The caller holds the grant and conversation locks after the relay CAS has
    succeeded. Old workers still carry the former registration/hash and fail
    the existing post-I/O fence; new workers resume the exact same page.
    """
    from integrations.models import SlackDmMirrorDelivery
    from integrations.services.slack_dm_registration_ledger import REGISTRATION_STATE_PREFIX, registration_generation
    from integrations.services.community_bridge.buzz import BuzzBridgeClient
    from django.utils import timezone

    def save_batch(rows):
        ambiguous = [row for row in rows if row.source_platform == "slack" and row.attempts > 0
                     and row.status in {"pending", "processing", "failed"}
                     and row.operation in {"create", "reaction_add"}]
        receipts = {}
        for offset in range(0, len(ambiguous), 100):
            receipts.update(BuzzBridgeClient.private_delivery_receipts(
                str(conversation.mlai_channel_id), [str(row.pk) for row in ambiguous[offset:offset + 100]],
            ))
        for row in ambiguous:
            receipt = receipts.get(str(row.pk))
            if receipt:
                row.metadata.update(destination_message_id=receipt["message_id"],
                                    destination_parent_message_id=receipt.get("parent_message_id", ""))
                row.status = "completed"
                row.completed_at = timezone.now()
                row.encrypted_text = ""
                row.last_error = ""
        SlackDmMirrorDelivery.objects.bulk_update(
            rows, ["metadata", "status", "completed_at", "encrypted_text", "last_error"], batch_size=200,
        )

    pending = []
    for row in SlackDmMirrorDelivery.objects.select_for_update().filter(
        conversation=conversation,
    ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX).iterator(chunk_size=200):
        metadata = dict(row.metadata or {})
        metadata["participant_hash"] = conversation.participant_hash
        if metadata.get("history_scan_state"):
            metadata.update(mlai_channel_id=str(conversation.mlai_channel_id),
                            registration_id=registration.source_message_id,
                            registration_generation=registration_generation(registration))
        row.metadata = metadata
        pending.append(row)
        if len(pending) == 200:
            save_batch(pending)
            pending.clear()
    if pending:
        save_batch(pending)
