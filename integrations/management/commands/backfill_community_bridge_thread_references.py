"""Restore dropped Slack thread-reference attachments via the existing edit outbox."""

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.formatting import (
    normalize_slack_thread_references,
)
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.store import (
    _normalize_slack_event,
    _canonicalize_event,
)

VERSION = "slack-thread-references-v1"


class Command(BaseCommand):
    help = "Restore missing forwarded-thread links in mapped public channels. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--slack-channel-id", required=True)
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--oldest", default="")
        parser.add_argument("--latest", default="")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm-historical-edits", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 5000:
            raise CommandError("--limit must be between 1 and 5000")
        if options["apply"] and not options["confirm_historical_edits"]:
            raise CommandError("--confirm-historical-edits is required with --apply")
        channel = CommunityBridgeChannel.objects.filter(
            slack_channel_id=options["slack_channel_id"],
            enabled=True,
            sync_edits=True,
            destination_platform=CommunityBridgePlatform.BUZZ,
        ).first()
        if channel is None:
            raise CommandError(
                "An enabled, mapped public channel with edit synchronization is required"
            )
        started = timezone.now()
        messages = SlackBridgeClient.get_channel_history(
            channel_id=channel.slack_channel_id,
            maximum_messages=options["limit"],
            oldest=options["oldest"],
            latest=options["latest"],
        )
        report = dict(
            scanned=len(messages),
            references=0,
            already_present=0,
            unavailable=0,
            would_enqueue=0,
            enqueued=0,
            raced=0,
            mode="apply" if options["apply"] else "dry_run",
        )
        for message in messages:
            references = normalize_slack_thread_references(
                message.get("attachments") or []
            )
            if not references:
                continue
            report["references"] += 1
            link = CommunityBridgeMessageLink.objects.filter(
                channel=channel,
                source_platform=CommunityBridgePlatform.SLACK,
                source_channel_id=channel.slack_channel_id,
                source_message_id=message.get("ts"),
                destination_platform=CommunityBridgePlatform.BUZZ,
                source_deleted_at__isnull=True,
                destination_deleted_at__isnull=True,
            ).first()
            if link is None:
                report["unavailable"] += 1
                continue
            latest = (
                CommunityBridgeDelivery.objects.filter(
                    channel=channel,
                    source_platform=CommunityBridgePlatform.SLACK,
                    source_message_id=link.source_message_id,
                    target_platform=CommunityBridgePlatform.BUZZ,
                    status=CommunityBridgeDeliveryStatus.COMPLETED,
                    delivery_type__in=(
                        CommunityBridgeDeliveryType.CREATE,
                        CommunityBridgeDeliveryType.EDIT,
                    ),
                )
                .order_by("-completed_at", "-id")
                .first()
            )
            previous = (latest.payload if latest else link.source_payload) or {}
            existing_urls = {
                item.get("url")
                for item in previous.get("attachments", [])
                if isinstance(item, dict)
            }
            if all(item["url"] in existing_urls for item in references):
                report["already_present"] += 1
                continue
            normalized = _normalize_slack_event(
                {
                    "event": {
                        **message,
                        "type": "message",
                        "channel_type": "channel",
                        "channel": channel.slack_channel_id,
                    }
                }
            )
            if not normalized:
                report["unavailable"] += 1
                continue
            normalized["delivery_type"] = CommunityBridgeDeliveryType.EDIT
            normalized["text"] = SlackBridgeClient.resolve_message_text(
                str(message.get("text") or "")
            )
            normalized["metadata"]["backfill_version"] = VERSION
            digest = hashlib.sha256(
                json.dumps(normalized, sort_keys=True).encode()
            ).hexdigest()[:32]
            key = f"{VERSION}:{link.pk}:{digest}"
            payload = _canonicalize_event(
                receipt_key=key,
                source_platform=CommunityBridgePlatform.SLACK,
                source_channel_id=channel.slack_channel_id,
                normalized_event=normalized,
            )
            if not options["apply"]:
                report["would_enqueue"] += 1
            else:
                outcome = self._enqueue(link, key, payload, started)
                report[outcome] += 1
        if messages:
            report["oldest_scanned_ts"] = str(messages[-1].get("ts") or "")
        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _enqueue(link, key, payload, started):
        with transaction.atomic():
            current = CommunityBridgeMessageLink.objects.select_for_update().get(
                pk=link.pk
            )
            pending = (
                CommunityBridgeDelivery.objects.filter(
                    channel=link.channel,
                    source_platform=CommunityBridgePlatform.SLACK,
                    source_message_id=link.source_message_id,
                    target_platform=CommunityBridgePlatform.BUZZ,
                )
                .exclude(status=CommunityBridgeDeliveryStatus.COMPLETED)
                .exists()
            )
            if (
                current.updated_at > started
                or current.source_deleted_at
                or current.destination_deleted_at
                or pending
            ):
                return "raced"
            receipt, created = CommunityBridgeReceipt.objects.get_or_create(
                platform=CommunityBridgePlatform.SLACK,
                receipt_key=key,
                defaults=dict(
                    channel=link.channel,
                    event_type=VERSION,
                    source_channel_id=link.source_channel_id,
                    source_message_id=link.source_message_id,
                    source_parent_message_id=payload["source_parent_message_id"],
                    status=CommunityBridgeReceiptStatus.ENQUEUED,
                    queued_delivery_count=1,
                    payload={},
                    processed_at=timezone.now(),
                ),
            )
            if not created:
                return "already_present"
            CommunityBridgeDelivery.objects.create(
                channel=link.channel,
                receipt=receipt,
                source_platform=CommunityBridgePlatform.SLACK,
                target_platform=CommunityBridgePlatform.BUZZ,
                delivery_type=CommunityBridgeDeliveryType.EDIT,
                status=CommunityBridgeDeliveryStatus.PENDING,
                source_event_key=key,
                source_channel_id=link.source_channel_id,
                source_message_id=link.source_message_id,
                source_parent_message_id=payload["source_parent_message_id"],
                target_channel_id=link.destination_channel_id,
                payload=payload,
                available_at=timezone.now(),
            )
            return "enqueued"
