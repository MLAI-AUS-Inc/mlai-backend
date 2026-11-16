"""Best-effort public notice after a member books coworking from MLAI Chat.

The booking itself never depends on this notice. It is posted by Public Roo's
Slack bot (``SlackService``) into the coworking channel that the bridge mirrors
to MLAI Chat, without a Roo mention, so Roo is never asked to act on it.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

NO_FOOD_REMINDER = "Reminder: no food is permitted in the coworking space."


def announcements_enabled() -> bool:
    """Kill switch: ``COMMUNITY_CHAT_COWORKING_ANNOUNCE_ENABLED`` (default on)."""
    return bool(getattr(settings, "COMMUNITY_CHAT_COWORKING_ANNOUNCE_ENABLED", True))


def _coworking_slack_channel():
    """The enabled Slack channel mirrored to Chat's coworking channel, if any."""
    from integrations.models import CommunityBridgeChannel
    from integrations.services.community_bridge.coworking import COWORKING_CHANNEL_ID
    from integrations.services.slack_roo import public_roo_target

    target = public_roo_target()
    if not target:
        return None
    channel = CommunityBridgeChannel.objects.filter(
        enabled=True,
        destination_platform="buzz",
        destination_channel_id=COWORKING_CHANNEL_ID,
        slack_workspace_id=target[0],
    ).values("slack_workspace_id", "slack_channel_id").first()
    return channel


def _member_label(user, workspace_id: str) -> str:
    """A Slack mention for linked members, otherwise their account name."""
    from integrations.models import CommunityBridgeIdentityLink

    slack_user_id = (
        CommunityBridgeIdentityLink.objects.filter(
            user_id=user.pk,
            slack_workspace_id=workspace_id,
            revoked_at__isnull=True,
        )
        .exclude(slack_user_id="")
        .values_list("slack_user_id", flat=True)
        .first()
    )
    if slack_user_id:
        return f"<@{slack_user_id}>"
    name = " ".join(str(getattr(user, "full_name", "") or "").split())
    # Keep Slack control characters out of a member-chosen display name.
    name = name.replace("<", "").replace(">", "").replace("&", "and")[:80]
    return name or "An MLAI member"


def announcement_text(label: str) -> str:
    return f"{label} is booked in for coworking today. {NO_FOOD_REMINDER}"


def announce_booking(user, booking) -> bool:
    """Post one notice for a newly created booking. Never raises."""
    if not announcements_enabled():
        return False
    try:
        channel = _coworking_slack_channel()
        if not channel:
            logger.info("coworking_announce_skipped reason=no_channel")
            return False
        from integrations.services.slack import SlackService

        sent, _ts = SlackService.send_message(
            channel["slack_channel_id"],
            announcement_text(_member_label(user, channel["slack_workspace_id"])),
        )
        if not sent:
            logger.warning(
                "coworking_announce_failed booking=%s reason=slack_error",
                getattr(booking, "pk", ""),
            )
        return bool(sent)
    except Exception as exc:  # noqa: BLE001 - the booking must never fail here
        logger.warning(
            "coworking_announce_failed booking=%s reason=%s",
            getattr(booking, "pk", ""),
            exc.__class__.__name__,
        )
        return False
