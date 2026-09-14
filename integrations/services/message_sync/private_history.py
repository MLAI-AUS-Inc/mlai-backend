"""Independent recent-head and known-thread repair within existing consent fences."""
import time
import uuid

from django.db import transaction
from django.utils import timezone

from .history import next_checkpoint, timestamp
from .coverage import record_page
from .scheduler import LeaseLost, finish_job, locked_job, schedule_job


def read_boundary(dm, conversation, grant, *, epoch, oldest):
    registration = dm._ensure_current_registration_row_locked(conversation, grant)
    if registration is None or dm._registration_state(registration) != dm.REGISTRATION_STATE_ACTIVE:
        raise dm.SlackDmMirrorAuthorizationError("sync_registration_not_current")
    return dm._SlackHistoryScanAuthority(
        epoch=epoch, participant_hash=dm._history_participant_boundary(conversation),
        mlai_channel_id=str(conversation.mlai_channel_id),
        registration_id=registration.source_message_id,
        registration_generation=dm._registration_generation(registration),
        history_days=int(grant.history_days), oldest=oldest,
    )


def private_page(lease, state):
    from integrations.services import slack_dm_mirror as dm
    conversation = state.private_conversation
    grant = conversation.grant
    if lease.kind == "archive":
        if conversation.history_backfilled_at is None:
            dm._enqueue_history_page(conversation, grant)
        conversation.refresh_from_db()
        finish_job(lease, checkpoint={}, complete=conversation.history_backfilled_at is not None,
                   delay_seconds=3600 if conversation.history_backfilled_at is not None else 0)
        return
    authority = dm._capture_slack_grant_api_authority(grant)
    scopes = dm._history_required_scopes(conversation.slack_conversation_id, kind=dm.conversation_kind(conversation))
    checkpoint = dict(lease.checkpoint)
    checkpoint.setdefault("scan_id", uuid.uuid4().hex)
    checkpoint.setdefault("upper_bound", f"{int(time.time())}.999999")
    upper_seconds = timestamp(checkpoint["upper_bound"])[0]
    floor = max(0, upper_seconds - grant.history_days * 86400) if grant.history_days > 0 else 0
    if lease.kind == "head":
        floor = max(floor, upper_seconds - 86400)
    checkpoint.setdefault("oldest", f"{floor}.000000")
    # A reduced consent window wins over a cursor saved under older consent.
    if timestamp(checkpoint["oldest"])[0] < floor and lease.kind != "head":
        checkpoint = {"scan_id": uuid.uuid4().hex, "oldest": f"{floor}.000000",
                      "upper_bound": f"{int(time.time())}.999999"}
    with transaction.atomic():
        current, owner = dm._locked_history_write_context(conversation.pk, grant.pk, authority, scopes)
        locked_job(lease)
        expected = read_boundary(dm, current, owner, epoch=checkpoint["scan_id"], oldest=checkpoint["oldest"])
    kwargs = dict(channel=conversation.slack_conversation_id, limit=15, inclusive=False,
                  oldest=checkpoint["oldest"], latest=checkpoint.get("latest", checkpoint["upper_bound"]))
    if checkpoint.get("cursor"):
        kwargs["cursor"] = checkpoint["cursor"]
    method = "conversations_history"
    if lease.kind == "thread":
        # Consent does not permit importing an old root merely because it has
        # newer replies. Existing main-history handling preserves allowed rows.
        if grant.history_days > 0 and timestamp(lease.source_object_key)[0] < floor:
            finish_job(lease, checkpoint={}, delay_seconds=86400, complete=True)
            return
        method = "conversations_replies"
        kwargs["ts"] = lease.source_object_key
    response = dm._call_slack_with_grant_authority(authority, method, required_scopes=scopes, **kwargs)
    if not response.get("ok"):
        raise RuntimeError("slack_private_history_failed")
    updated, complete = next_checkpoint(checkpoint, response, thread=lease.kind == "thread")
    with transaction.atomic():
        current, owner = dm._locked_history_write_context(conversation.pk, grant.pk, authority, scopes)
        sync_state, _ = locked_job(lease)
        if read_boundary(dm, current, owner, epoch=expected.epoch, oldest=expected.oldest) != expected:
            raise LeaseLost("sync_private_authority_changed")
        messages = [item for item in response.get("messages") or [] if isinstance(item, dict) and item.get("ts")]
        for message in sorted(messages, key=lambda item: timestamp(item["ts"])):
            if not dm._history_message_author_allowed(current, message):
                continue
            if timestamp(message["ts"]) < timestamp(checkpoint["oldest"]):
                continue
            message = dm._normalize_history_author(current, message)
            if lease.kind == "thread":
                message["thread_ts"] = str(message.get("thread_ts") or lease.source_object_key)
            dm._enqueue_history_message(current, message, scan_authority=expected, held_until=timezone.now())
            root = str(message.get("thread_ts") or message["ts"])
            if message.get("reply_count") or message.get("latest_reply") or root != message["ts"]:
                schedule_job(sync_state, "thread", source_object_key=root)
        updated = record_page(sync_state, lease.kind, updated, response, complete=complete)
        finish_job(lease, checkpoint={} if complete else updated,
                   delay_seconds=(60 if lease.kind == "head" else 3600) if complete else 0, complete=complete)
