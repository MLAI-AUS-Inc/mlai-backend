"""Consume verified native events and deliver Roo's queued messages."""

import base64
from datetime import datetime, timedelta, timezone as utc_timezone
import hashlib
import logging
import re
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone
import requests

from agent_runtime import native_crypto
from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeMessageLink
from roo.models import PointsRequest, PointsRequestDelivery, PointsRequestScan
from roo.permissions import is_points_admin_user
from roo.points_request_workflow import (
    CELEBRATION, CONFIRMATION, EVENT_ID, REPOST, ROO_POINTS_CHANNEL_ID, SLACK_CONFIRMATION, SUMMARY,
    approve_locked_request, bridge_channel, enqueue, mention, slack_confirmation_text,
)

logger = logging.getLogger(__name__)
MESSAGE_KINDS = [9, 40002]
REACTION = 7
PAGE_SIZE = 200
REQUEST = re.compile(
    r"^@Roo\s+(?:please\s+)?(?:give\s+me|i\s+request|request)\s+"
    r"([1-9][0-9]{0,5})\s+(?:roo\s+)?points?\s+for\s+(.{1,1000})$",
    re.IGNORECASE | re.DOTALL,
)


def tags(event, name):
    return [tag for tag in event.get("tags", []) if len(tag) > 1 and tag[0] == name]


def verified_actor(event):
    since = datetime.fromtimestamp(event["created_at"], tz=utc_timezone.utc)
    device = CommunityChatDevice.objects.select_related("user").filter(
        public_key=event["pubkey"], status="verified", revoked_at__isnull=True,
        verified_at__lte=since, user__is_active=True,
    ).first()
    return device.user if device else None


class RooRelay:
    def __init__(self, private_key):
        self.key = native_crypto.signing_key(private_key)
        self.pubkey = native_crypto.public_key(self.key)
        relay = settings.COMMUNITY_CHAT_RELAY_URL.rstrip("/")
        self.url = "https://" + relay[6:] if relay.startswith("wss://") else relay
        if not self.url.startswith("https://"):
            raise ValueError("Roo requires the configured HTTPS relay")
        self.session = requests.Session()

    def post(self, path, value):
        url = self.url + path
        body = native_crypto.canonical_json(value).encode()
        proof = native_crypto.sign_event(
            self.key, kind=27235, created_at=timezone.now().timestamp(), content="",
            tags=[["u", url], ["method", "POST"], ["payload", hashlib.sha256(body).hexdigest()],
                  ["nonce", secrets.token_hex(16)]],
        )
        result = self.session.post(
            url, data=body, timeout=8, allow_redirects=False,
            headers={"Content-Type": "application/json", "Authorization": "Nostr " +
                     base64.b64encode(native_crypto.canonical_json(proof).encode()).decode()},
        )
        result.raise_for_status()
        return result.json()

    def query(self, channel, kinds, **filters):
        if channel is not None:
            filters["#h"] = [str(channel)]
        result = self.post("/query", [{"kinds": kinds, **filters}])
        if not isinstance(result, list):
            raise ValueError("Unexpected Roo relay query response")
        return result

    def publish(self, channel, event):
        result = self.post("/events", event)
        if result.get("accepted") and result.get("event_id") == event["id"]:
            return
        existing = self.query(channel, [event["kind"]], ids=[event["id"]], limit=1)
        if not any(row.get("id") == event["id"] for row in existing):
            raise ValueError("Roo message was not accepted by the relay")

    def scan(self, start, end):
        rows = self.query(ROO_POINTS_CHANNEL_ID, [*MESSAGE_KINDS, REACTION],
                          since=start, until=end, limit=PAGE_SIZE)
        if len(rows) < PAGE_SIZE:
            yield from sorted(rows, key=lambda e: (e["created_at"], e["id"]))
            return
        if start >= end:
            raise ValueError("Roo points event scan reached its per-second limit")
        mid = (start + end) // 2
        yield from self.scan(start, mid)
        yield from self.scan(mid + 1, end)


def ensure_roo_profile(relay):
    if relay.query(None, [0], authors=[relay.pubkey], limit=1):
        return
    event = native_crypto.sign_event(
        relay.key, kind=0, created_at=timezone.now().timestamp(), tags=[],
        content=native_crypto.canonical_json({
            "name": "Roo", "display_name": "Roo",
            "about": "I help MLAI Chat members request and celebrate Roo Points. 🦘",
        }),
    )
    relay.publish(None, event)


def thread_root(event, relay):
    marked_root = next((tag[1] for tag in tags(event, "e")
                        if len(tag) >= 4 and tag[3] == "root"), None)
    marked_reply = next((tag[1] for tag in tags(event, "e")
                         if len(tag) >= 4 and tag[3] == "reply"), None)
    root = marked_root or marked_reply
    if root is None:
        return event["id"]
    if not EVENT_ID.fullmatch(root):
        return None
    candidates = relay.query(ROO_POINTS_CHANNEL_ID, MESSAGE_KINDS, ids=[root], limit=1)
    return root if any(row.get("id") == root and not any(
        len(tag) >= 4 and tag[3] == "reply" for tag in tags(row, "e")
    ) for row in candidates) else None


def handle_event(relay, event):
    if event["pubkey"] == relay.pubkey or tags(event, "slack-bridge"):
        return
    channel_tags = tags(event, "h")
    # Chat reactions omit the h tag. The relay scopes them to the target
    # message, and the approval lookup below requires this channel's summary.
    if channel_tags != [["h", str(ROO_POINTS_CHANNEL_ID)]] and (
        event["kind"] != REACTION or channel_tags
    ):
        return
    content = event["content"].strip()
    if event["kind"] in MESSAGE_KINDS:
        match = REQUEST.fullmatch(content)
        if not match or relay.pubkey not in {tag[1] for tag in tags(event, "p")}:
            return
        author = verified_actor(event)
        root = thread_root(event, relay) if author else None
        if not root or not match[2].strip():
            return
        with transaction.atomic():
            row, created = PointsRequest.objects.get_or_create(
                native_event_id=event["id"], defaults=dict(
                    native_channel_id=ROO_POINTS_CHANNEL_ID, native_root_id=root,
                    native_recipient=author, requester_slack_id="", target_slack_id="",
                    points=int(match[1]), reason=match[2].strip(),
                ),
            )
            if created:
                label, keys = mention(author)
                enqueue(row, SUMMARY, ROO_POINTS_CHANNEL_ID, root,
                        f"📝 **Points request pending**\n\n{label} requested **{row.points} points** "
                        f"for: {row.reason}\n\nPoints Admins can approve this with ✅ or ✔️.", keys)
        return
    if event["kind"] != REACTION or content.replace("\ufe0f", "") not in {"✅", "✔"}:
        return
    actor = verified_actor(event)
    if not actor or not is_points_admin_user(actor):
        return
    target = next((tag[1] for tag in reversed(tags(event, "e"))), "")
    if not EVENT_ID.fullmatch(target):
        return
    with transaction.atomic():
        row = PointsRequest.objects.select_for_update().filter(
            native_summary_id=target, native_channel_id=ROO_POINTS_CHANNEL_ID,
            native_recipient__is_active=True, status="pending",
        ).first()
        if row:
            approve_locked_request(row, row.native_recipient, admin=actor)


def scan_requests(relay, start):
    checkpoint, _ = PointsRequestScan.objects.get_or_create(
        channel_id=ROO_POINTS_CHANNEL_ID, defaults={"through": start - 1},
    )
    end = min(checkpoint.through + 900, int(timezone.now().timestamp()) - 5)
    if end < max(start, checkpoint.through - 59):
        return
    for event in relay.scan(max(start, checkpoint.through - 59), end):
        handle_event(relay, event)
    PointsRequestScan.objects.filter(channel_id=ROO_POINTS_CHANNEL_ID, through__lt=end).update(through=end)


def bridged_confirmation_link(delivery):
    req = delivery.request
    mapping = bridge_channel(req.slack_channel_id)
    if not mapping or mapping.destination_channel_id != str(delivery.channel_id):
        return None
    root_ts = req.slack_thread_ts or req.slack_summary_message_ts
    if not root_ts:
        return None
    candidates = CommunityBridgeMessageLink.objects.filter(
        channel=mapping, source_platform="slack", source_channel_id=req.slack_channel_id,
        source_author_id=settings.COMMUNITY_CHAT_ROO_SLACK_USER_ID, source_parent_message_id=root_ts,
        destination_platform="buzz", destination_channel_id=str(delivery.channel_id),
        source_deleted_at__isnull=True, destination_deleted_at__isnull=True,
    )
    if delivery.external_confirmation_id:
        mapped = candidates.filter(source_message_id=delivery.external_confirmation_id).first()
    else:
        matches = list(candidates.filter(
            source_payload__text=slack_confirmation_text(req), created_at__gte=req.approved_at,
        )[:2])
        mapped = matches[0] if len(matches) == 1 else None
    return mapped


def prepare_event(relay, delivery):
    if delivery.signed_event:
        return True
    request = delivery.request
    parent = delivery.parent_id
    created_at = int(timezone.now().timestamp())
    if delivery.stage == CELEBRATION:
        if request.native_channel_id:
            confirmation = request.deliveries.filter(
                stage=CONFIRMATION, delivered_at__isnull=False,
            ).first()
            if not confirmation:
                return False
            created_at = max(created_at, confirmation.signed_event["created_at"] + 1)
        else:
            mapped = bridged_confirmation_link(delivery)
            if not mapped or not EVENT_ID.fullmatch(mapped.destination_parent_message_id):
                return False
            parent = mapped.destination_parent_message_id
            created_at = max(created_at, int(mapped.created_at.timestamp()) + 1)
    elif delivery.stage == REPOST:
        if not request.deliveries.filter(stage=CELEBRATION, delivered_at__isnull=False).exists():
            return False

    event_tags = [["h", str(delivery.channel_id)], ["client_nonce", f"roo:{request.pk}:{delivery.stage}"]]
    if parent:
        event_tags.extend([["e", parent, "", "root"], ["e", parent, "", "reply"]])
    event_tags.extend(["p", key] for key in delivery.mention_keys)
    event = native_crypto.sign_event(
        relay.key, kind=9, created_at=created_at, tags=event_tags,
        content=delivery.content,
    )
    delivery.signed_event = event
    delivery.save(update_fields=["signed_event"])
    if delivery.stage == SUMMARY:
        request.native_summary_id = event["id"]
        request.save(update_fields=["native_summary_id"])
    return True


def deliver_ready(relay):
    pending = PointsRequestDelivery.objects.filter(
        delivered_at__isnull=True, available_at__lte=timezone.now(),
    ).filter(stage__in=(SUMMARY, CONFIRMATION, CELEBRATION, REPOST)).order_by("available_at", "pk").values_list("pk", flat=True)[:50]
    for delivery_id in list(pending):
        with transaction.atomic():
            row = PointsRequestDelivery.objects.select_for_update().select_related("request").get(pk=delivery_id)
            if row.delivered_at or not prepare_event(relay, row):
                if not row.delivered_at:
                    row.available_at = timezone.now() + timedelta(seconds=5)
                    row.save(update_fields=["available_at"])
                continue
        with transaction.atomic():
            row = PointsRequestDelivery.objects.select_for_update().get(pk=delivery_id)
            if row.delivered_at:
                continue
            try:
                relay.publish(row.channel_id, row.signed_event)
            except (requests.RequestException, ValueError) as exc:
                row.attempts += 1
                logger.warning("Roo delivery retry id=%s stage=%s attempt=%s error=%s",
                               row.pk, row.stage, row.attempts, type(exc).__name__)
                row.available_at = timezone.now() + timedelta(seconds=min(2 ** min(row.attempts, 8), 300))
                row.save(update_fields=["available_at", "attempts"])
            else:
                row.delivered_at = timezone.now()
                row.save(update_fields=["delivered_at"])
