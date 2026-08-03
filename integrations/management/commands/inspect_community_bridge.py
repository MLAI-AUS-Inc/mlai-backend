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
                    "created_at": receipt.created_at.isoformat(),
                    "processed_at": receipt.processed_at.isoformat() if receipt.processed_at else None,
                    "error_present": bool(receipt.error_text),
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
                    "created_at": delivery.created_at.isoformat(),
                    "completed_at": delivery.completed_at.isoformat() if delivery.completed_at else None,
                    "error_present": bool(delivery.last_error),
                }
                for delivery in deliveries.order_by("-created_at")[:20]
            ],
            "message_links": [
                {
                    "id": link.id,
                    "source_platform": link.source_platform,
                    "source_message_id": link.source_message_id,
                    "destination_platform": link.destination_platform,
                    "destination_message_id": link.destination_message_id,
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
        }
        latest_created_at = result["recent_receipts"]["latest_created_at"]
        result["recent_receipts"]["latest_created_at"] = (
            latest_created_at.isoformat() if latest_created_at else None
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
