"""Roo's Slack welcome for introductions written in MLAI Chat.

Intros typed in Slack are rewarded by Public Roo, which replies in the thread.
Intros written in MLAI Chat reach Slack through the bridge bot, so Roo ignores
them; the Volunteer receipt pipeline awards their points instead and nothing
is said in Slack. This sweep closes that gap: for each newly approved Chat
intro whose bridged Slack post exists, it posts Roo's usual welcome as a thread
reply using Public Roo's bot token (``SlackService``), at most once.

The reward never depends on this message. Kill switch:
``COMMUNITY_CHAT_INTRO_WELCOME_ENABLED`` (default on).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Only recent intros are welcomed, so enabling this never replays old ones.
WELCOME_WINDOW = timedelta(hours=48)
MAX_ATTEMPTS = 5
MAX_PER_TICK = 20


def welcomes_enabled() -> bool:
    return bool(getattr(settings, "COMMUNITY_CHAT_INTRO_WELCOME_ENABLED", True))


def welcome_text(label: str, points: str) -> str:
    """Match Public Roo's wording for intros typed in Slack."""
    return (
        f"Welcome {label}! You've earned {points} Roo points "
        "for introducing yourself or your startup."
    )


def _candidates(now):
    from .volunteer.access import channels
    from .volunteer.models import VolunteerSourceReceipt

    start_here = channels().get("start_here")
    if not start_here:
        return []
    return list(
        VolunteerSourceReceipt.objects.select_related("recognition", "actor")
        .filter(
            kind="post",
            status="processed",
            source__channel_id=start_here,
            occurred_at__gte=now - WELCOME_WINDOW,
            recognition__action_key="introduce_yourself",
            recognition__status="approved",
            recognition__ledger__isnull=False,
        )
        .exclude(recognition__chat_intro_welcome__status="sent")
        .order_by("occurred_at")[:MAX_PER_TICK]
    )


def _slack_post(receipt):
    """The bridged top-level Slack copy of this Chat post, if it exists yet."""
    from integrations.models import CommunityBridgeMessageLink

    message_id = str(receipt.source.get("message_id") or "")
    # The award must come from this very post, not an earlier intro.
    if not message_id or receipt.recognition.source.get("message_id") != message_id:
        return None
    return (
        CommunityBridgeMessageLink.objects.select_related("channel")
        .filter(
            source_platform="buzz",
            destination_platform="slack",
            source_channel_id=receipt.source["channel_id"],
            source_message_id=message_id,
            destination_parent_message_id="",
            source_deleted_at__isnull=True,
            destination_deleted_at__isnull=True,
        )
        .first()
    )


def _welcome(receipt, link) -> str:
    """Claim, post and record one welcome. Returns sent, skipped or failed."""
    from integrations.services.slack import SlackService

    from .coworking_announcement import _member_label
    from .volunteer.models import ChatIntroWelcome
    from .volunteer.policy import roo

    recognition = receipt.recognition
    with transaction.atomic():
        ChatIntroWelcome.objects.get_or_create(
            recognition=recognition,
            defaults=dict(
                slack_channel_id=link.destination_channel_id,
                slack_thread_ts=link.destination_message_id,
            ),
        )
        # The row lock serialises overlapping sweeps for the whole post.
        row = ChatIntroWelcome.objects.select_for_update().get(recognition=recognition)
        if row.status == ChatIntroWelcome.STATUS_SENT or row.attempts >= MAX_ATTEMPTS:
            return "skipped"
        row.attempts += 1
        label = _member_label(receipt.actor, link.channel.slack_workspace_id)
        try:
            sent, message_ts = SlackService.send_message(
                row.slack_channel_id,
                welcome_text(label, roo(recognition.reward_microroo)),
                thread_ts=row.slack_thread_ts,
            )
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            sent, message_ts = False, None
            row.last_error = exc.__class__.__name__[:120]
        if sent:
            row.status = ChatIntroWelcome.STATUS_SENT
            row.slack_message_ts = str(message_ts or "")
            row.last_error = ""
        elif not row.last_error:
            row.last_error = "slack_error"
        row.save()
    if not sent:
        logger.warning(
            "chat_intro_welcome_failed recognition=%s attempt=%s reason=%s",
            recognition.pk, row.attempts, row.last_error,
        )
        return "failed"
    return "sent"


def run_chat_intro_welcome_sweep():
    """Scheduled runner: welcome recent approved Chat intros once each."""
    if not welcomes_enabled():
        return {"status": "disabled"}
    counts = {"sent": 0, "waiting_for_slack": 0, "failed": 0, "skipped": 0}
    for receipt in _candidates(timezone.now()):
        link = _slack_post(receipt)
        if link is None:
            # The bridge may not have posted to Slack yet; retry next tick.
            counts["waiting_for_slack"] += 1
            continue
        counts[_welcome(receipt, link)] += 1
    return {"status": "ok", **counts}
