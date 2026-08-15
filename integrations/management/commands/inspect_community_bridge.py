import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
)


class Command(BaseCommand):
    help = "Report payload-free operational metadata for one community bridge mapping."

    def add_arguments(self, parser):
        parser.add_argument("--slack-channel-id", required=True)
        parser.add_argument("--slack-message-id", default="")
        parser.add_argument("--recent-minutes", type=int, default=60)

    def handle(self, *args, **options):
        channel_id = str(options["slack_channel_id"] or "").strip()
        message_id = str(options["slack_message_id"] or "").strip()
        recent_minutes = int(options["recent_minutes"] or 0)
        if recent_minutes < 1 or recent_minutes > 1440:
            raise CommandError("recent-minutes must be between 1 and 1440")

        try:
            channel = CommunityBridgeChannel.objects.get(
                slack_channel_id=channel_id,
                destination_platform=CommunityBridgePlatform.BUZZ,
            )
        except CommunityBridgeChannel.DoesNotExist as exc:
            raise CommandError("MLAI Chat mapping was not found for that Slack channel") from exc

        receipts = CommunityBridgeReceipt.objects.filter(
            platform=CommunityBridgePlatform.SLACK,
            source_channel_id=channel.slack_channel_id,
        )
        deliveries = CommunityBridgeDelivery.objects.filter(channel=channel)
        links = CommunityBridgeMessageLink.objects.filter(channel=channel)
        if message_id:
            receipts = receipts.filter(source_message_id=message_id)
            deliveries = deliveries.filter(
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=message_id,
            )
            links = links.filter(
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=message_id,
            )

        recent_cutoff = timezone.now() - timedelta(minutes=recent_minutes)
        recent_receipts = CommunityBridgeReceipt.objects.filter(
            platform=CommunityBridgePlatform.SLACK,
            source_channel_id=channel.slack_channel_id,
            created_at__gte=recent_cutoff,
        )
        integrity_links = CommunityBridgeMessageLink.objects.filter(
            channel=channel,
            source_platform=CommunityBridgePlatform.SLACK,
            destination_platform=CommunityBridgePlatform.BUZZ,
            source_deleted_at__isnull=True,
            destination_deleted_at__isnull=True,
        )
        if message_id:
            integrity_links = integrity_links.filter(source_message_id=message_id)
        integrity = self._thread_integrity(list(integrity_links))

        result = {
            "status": "found" if receipts.exists() else "not_found",
            "mapping": {
                "id": channel.id,
                "enabled": channel.enabled,
                "slack_channel_id": channel.slack_channel_id,
                "destination_platform": channel.destination_platform,
                "destination_channel_id": channel.destination_channel_id,
                "sync_edits": channel.sync_edits,
                "sync_deletes": channel.sync_deletes,
                "sync_replies": channel.sync_replies,
            },
            "query": {
                "slack_message_id": message_id,
                "recent_minutes": recent_minutes,
            },
            "receipts": [
                {
                    "id": receipt.id,
                    "receipt_key": receipt.receipt_key,
                    "event_type": receipt.event_type,
                    "status": receipt.status,
                    "queued_delivery_count": receipt.queued_delivery_count,
                    "source_parent_message_id": receipt.source_parent_message_id,
                    "created_at": receipt.created_at.isoformat(),
                    "processed_at": receipt.processed_at.isoformat() if receipt.processed_at else None,
                    "error_code": self._error_code(receipt.error_text),
                }
                for receipt in receipts.order_by("-created_at")[:20]
            ],
            "deliveries": [
                {
                    "id": delivery.id,
                    "delivery_type": delivery.delivery_type,
                    "status": delivery.status,
                    "target_platform": delivery.target_platform,
                    "attempts": delivery.attempts,
                    "max_attempts": delivery.max_attempts,
                    "source_message_id": delivery.source_message_id,
                    "source_parent_message_id": delivery.source_parent_message_id,
                    "created_at": delivery.created_at.isoformat(),
                    "completed_at": delivery.completed_at.isoformat() if delivery.completed_at else None,
                    "error_code": self._error_code(delivery.last_error),
                }
                for delivery in deliveries.order_by("-created_at")[:20]
            ],
            "message_links": [
                {
                    "id": link.id,
                    "source_platform": link.source_platform,
                    "source_message_id": link.source_message_id,
                    "source_parent_message_id": link.source_parent_message_id,
                    "destination_platform": link.destination_platform,
                    "destination_message_id": link.destination_message_id,
                    "destination_parent_message_id": link.destination_parent_message_id,
                    "source_deleted": link.source_deleted_at is not None,
                    "destination_deleted": link.destination_deleted_at is not None,
                    "created_at": link.created_at.isoformat(),
                }
                for link in links.order_by("-created_at")[:20]
            ],
            "recent_receipts": {
                "count": recent_receipts.count(),
                "by_status": {
                    row["status"]: row["count"]
                    for row in recent_receipts.values("status")
                    .annotate(count=Count("id"))
                    .order_by("status")
                },
                "latest_created_at": (
                    recent_receipts.order_by("-created_at")
                    .values_list("created_at", flat=True)
                    .first()
                ),
            },
            "thread_integrity": integrity,
        }
        latest_created_at = result["recent_receipts"]["latest_created_at"]
        result["recent_receipts"]["latest_created_at"] = (
            latest_created_at.isoformat() if latest_created_at else None
        )
        self.stdout.write(json.dumps(result, sort_keys=True))

    @staticmethod
    def _error_code(value):
        message = str(value or "")
        if not message:
            return ""
        for code, marker in (
            ("parent_not_ready", "CommunityBridgeParentNotReady"),
            ("relay_timestamp_rejected", "timestamp too far from server time"),
            ("adapter_rejected", "adapter rejected"),
            ("adapter_unavailable", "adapter returned HTTP"),
            ("slack_api_error", "SlackApiError"),
        ):
            if marker in message:
                return code
        return "other"

    @staticmethod
    def _thread_integrity(links):
        root_destinations = {
            link.source_message_id: link.destination_message_id
            for link in links
            if not str(link.source_parent_message_id or "").strip()
        }
        replies = [
            link for link in links if str(link.source_parent_message_id or "").strip()
        ]
        missing_destination_parent = 0
        wrong_destination_parent = 0
        missing_root_link = 0
        for link in replies:
            expected_parent = root_destinations.get(link.source_parent_message_id)
            if not expected_parent:
                missing_root_link += 1
            if not str(link.destination_parent_message_id or "").strip():
                missing_destination_parent += 1
            elif expected_parent and link.destination_parent_message_id != expected_parent:
                wrong_destination_parent += 1
        return {
            "active_links": len(links),
            "reply_links": len(replies),
            "missing_root_link": missing_root_link,
            "missing_destination_parent": missing_destination_parent,
            "wrong_destination_parent": wrong_destination_parent,
        }
