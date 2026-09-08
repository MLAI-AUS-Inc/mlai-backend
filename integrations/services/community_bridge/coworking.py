"""Forward explicit MLAI Chat coworking requests to the existing Public Roo flow."""

from datetime import datetime
import re
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

from integrations.models import CommunityBridgeChannel, CommunityBridgeIdentityLink
from .identity import verified_identity_for_buzz

COWORKING_CHANNEL_ID = "b2566a10-c26c-5bae-a362-051254af85ea"
BOOK_TODAY_MESSAGE = "@Roo please book me a coworking desk for today."


class CoworkingHandoffError(RuntimeError):
    """Retryable failure without credentials or private response bodies."""


def _configuration():
    url = str(getattr(settings, "ROO_SERVICE_URL", "") or "").rstrip("/")
    key = str(getattr(settings, "ROO_INTERNAL_MENTION_API_KEY", "") or "").strip()
    parsed = urlparse(url)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not key):
        return None
    return url + "/api/mention", key


def coworking_booking_available(user):
    """Report availability only for the member's verified Slack identity."""
    if not _configuration():
        return False
    workspaces = CommunityBridgeChannel.objects.filter(
        enabled=True, destination_platform="buzz",
        destination_channel_id=COWORKING_CHANNEL_ID,
    ).values_list("slack_workspace_id", flat=True)
    return CommunityBridgeIdentityLink.objects.filter(
        user_id=user.pk, revoked_at__isnull=True,
        slack_workspace_id__in=workspaces,
    ).exclude(slack_user_id="").exists()


def is_coworking_request(delivery):
    """Only the explicit booking command in the mapped public channel qualifies."""
    return (
        not re.fullmatch(r"[0-9a-fA-F]{64}", str(getattr(settings, "COMMUNITY_CHAT_ROO_PUBLIC_KEY", "") or "").strip())
        and delivery.get("source_platform") == "buzz"
        and delivery.get("delivery_type") == "create"
        and not delivery.get("source_parent_message_id")
        and delivery.get("source_channel_id") == COWORKING_CHANNEL_ID
        and (delivery.get("channel") or {}).get("destination_channel_id") == COWORKING_CHANNEL_ID
        and (delivery.get("payload") or {}).get("text", "").strip() == BOOK_TODAY_MESSAGE
    )


def prepare_coworking_request(delivery):
    """Resolve the signed sender server-side and freeze today's Melbourne date."""
    if not _configuration():
        raise CoworkingHandoffError("Public Roo booking handoff is not configured")
    identity = verified_identity_for_buzz(
        slack_workspace_id=delivery["channel"]["slack_workspace_id"],
        buzz_pubkey=str(delivery["payload"].get("source_author_id") or ""),
    )
    if not identity or not identity.get("slack_user_id"):
        raise CoworkingHandoffError("Booking requires a verified Slack identity")
    day = datetime.fromtimestamp(delivery["created_at"], ZoneInfo("Australia/Melbourne")).date()
    return {
        "text": f"Please book me in on {day.isoformat()}.",
        "user_id": identity["slack_user_id"],
        "channel_id": delivery["target_channel_id"],
        "post_reply": True,
        "request_id": str(uuid5(NAMESPACE_URL, f"mlai-coworking:{delivery['source_message_id']}")),
    }


def dispatch_coworking_request(payload, thread_ts):
    """Use Public Roo's service credential; Roo posts its own reply to Slack."""
    configuration = _configuration()
    if not configuration:
        raise CoworkingHandoffError("Public Roo booking handoff is not configured")
    url, key = configuration
    try:
        response = requests.post(
            url, headers={"Authorization": f"Bearer {key}"},
            json={**payload, "thread_ts": thread_ts},
            timeout=(5, 120), allow_redirects=False,
        )
        if response.status_code != 200:
            raise CoworkingHandoffError(f"Public Roo handoff returned HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict) or result.get("reply_delivered") is not True:
            raise CoworkingHandoffError("Public Roo did not confirm its reply")
    except (requests.RequestException, ValueError) as exc:
        raise CoworkingHandoffError("Public Roo booking handoff could not complete") from exc


async def deliver_coworking_request(delivery, text, parent_ts):
    """Checkpoint the Slack root so retries reuse the same booking conversation."""
    import asyncio
    from .slack import SlackBridgeClient
    from .store import complete_create_delivery, complete_delivery, resolve_message_link

    payload = await asyncio.to_thread(prepare_coworking_request, delivery)
    link = await asyncio.to_thread(
        resolve_message_link, source_platform="buzz",
        source_channel_id=delivery["source_channel_id"],
        source_message_id=delivery["source_message_id"], destination_platform="slack",
    )
    if link:
        root_ts = link["destination_message_id"]
    else:
        response = await asyncio.to_thread(
            SlackBridgeClient.post_message, channel_id=delivery["target_channel_id"],
            text=text, thread_ts=parent_ts,
            client_msg_id=str(uuid5(NAMESPACE_URL, f"mlai-community-bridge:{delivery['id']}")),
        )
        root_ts = str(response.get("message_id") or "")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", root_ts):
            raise CoworkingHandoffError("Slack did not confirm the booking message")
        await asyncio.to_thread(
            complete_create_delivery, delivery_id=delivery["id"],
            destination_message_id=root_ts, destination_channel_id=delivery["target_channel_id"],
            destination_parent_message_id=parent_ts or "", destination_payload=response,
            mark_completed=False,
        )
    await asyncio.to_thread(dispatch_coworking_request, payload, root_ts)
    await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"], wake_waiting_children=True)
