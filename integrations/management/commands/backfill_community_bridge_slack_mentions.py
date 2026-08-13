import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.contracts import (
    BridgeAttachment,
    CanonicalBridgeEvent,
)
from integrations.services.community_bridge.formatting import (
    has_slack_entity_references,
)
from integrations.services.community_bridge.slack import SlackBridgeClient


BACKFILL_VERSION = "slack-mention-backfill-v1"
REACTION_MESSAGE_ID_PREFIX = "reaction:"


class Command(BaseCommand):
    help = (
        "Resolve Slack user and channel references in existing MLAI Chat mirrors. "
        "Dry-run is the default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--after-link-id",
            type=int,
            default=0,
            help="Resume after this message-link ID (default: 0).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Maximum message links to inspect in this pass (default: 500).",
        )
        parser.add_argument(
            "--slack-channel-id",
            action="append",
            default=[],
            help="Restrict to one Slack channel ID; repeat to select several channels.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist receipts and enqueue bridge edit deliveries.",
        )
        parser.add_argument(
            "--confirm-historical-edits",
            action="store_true",
            help="Required with --apply to acknowledge that signed edits will be published.",
        )

    def handle(self, *args, **options):
        after_link_id = int(options["after_link_id"])
        limit = int(options["limit"])
        apply_changes = bool(options["apply"])
        if after_link_id < 0:
            raise CommandError("--after-link-id must be zero or greater")
        if limit < 1 or limit > 5000:
            raise CommandError("--limit must be between 1 and 5000")
        if apply_changes and not options["confirm_historical_edits"]:
            raise CommandError("--confirm-historical-edits is required with --apply")
        if not SlackBridgeClient.is_configured():
            raise CommandError("SLACK_BRIDGE_BOT_TOKEN is required")

        channel_ids = sorted(
            {
                str(channel_id or "").strip()
                for channel_id in options["slack_channel_id"]
                if str(channel_id or "").strip()
            }
        )
        queryset = self._candidate_links(
            after_link_id=after_link_id,
            channel_ids=channel_ids,
        )
        candidate_count = queryset.count()
        links = list(queryset[:limit])
        receipt_keys = {link.id: self._receipt_key(link.id) for link in links}
        existing_receipt_keys = set(
            CommunityBridgeReceipt.objects.filter(
                platform=CommunityBridgePlatform.SLACK,
                receipt_key__in=receipt_keys.values(),
            ).values_list("receipt_key", flat=True)
        )
        report = {
            "after_link_id": after_link_id,
            "already_enqueued": 0,
            "candidate_count": candidate_count,
            "enqueued": 0,
            "invalid_payload": 0,
            "last_scanned_link_id": links[-1].id if links else after_link_id,
            "limit": limit,
            "mode": "apply" if apply_changes else "dry_run",
            "no_retained_slack_markup": 0,
            "remaining_candidates": max(candidate_count - len(links), 0),
            "resolved_to_existing_text": 0,
            "scanned": len(links),
            "slack_channel_ids": channel_ids,
            "would_enqueue": 0,
        }

        for link in links:
            receipt_key = receipt_keys[link.id]
            if receipt_key in existing_receipt_keys:
                report["already_enqueued"] += 1
                continue
            source_payload, raw_text = self._latest_payload_and_raw_text(link)
            if not has_slack_entity_references(raw_text):
                report["no_retained_slack_markup"] += 1
                continue
            resolved_text = SlackBridgeClient.resolve_message_text(raw_text)
            if resolved_text == str(source_payload.get("text") or ""):
                report["resolved_to_existing_text"] += 1
                continue
            try:
                payload = self._backfill_payload(
                    link=link,
                    receipt_key=receipt_key,
                    source_payload=source_payload,
                    resolved_text=resolved_text,
                )
            except (TypeError, ValueError) as exc:
                report["invalid_payload"] += 1
                self.stderr.write(
                    self.style.WARNING(f"Skipping message link {link.id}: {exc}")
                )
                continue
            if not apply_changes:
                report["would_enqueue"] += 1
                continue
            if self._enqueue(link=link, receipt_key=receipt_key, payload=payload):
                report["enqueued"] += 1
            else:
                report["already_enqueued"] += 1

        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _candidate_links(*, after_link_id, channel_ids):
        queryset = (
            CommunityBridgeMessageLink.objects.select_related("channel")
            .filter(
                id__gt=after_link_id,
                source_platform=CommunityBridgePlatform.SLACK,
                destination_platform=CommunityBridgePlatform.BUZZ,
                source_deleted_at__isnull=True,
                destination_deleted_at__isnull=True,
                channel__enabled=True,
                channel__sync_edits=True,
                channel__destination_platform=CommunityBridgePlatform.BUZZ,
            )
            .exclude(source_message_id__startswith=REACTION_MESSAGE_ID_PREFIX)
            .order_by("id")
        )
        if channel_ids:
            queryset = queryset.filter(source_channel_id__in=channel_ids)
        return queryset

    @staticmethod
    def _receipt_key(link_id):
        return f"{BACKFILL_VERSION}:{int(link_id)}"

    @classmethod
    def _latest_payload_and_raw_text(cls, link):
        deliveries = (
            CommunityBridgeDelivery.objects.select_related("receipt")
            .filter(
                channel_id=link.channel_id,
                source_platform=CommunityBridgePlatform.SLACK,
                target_platform=CommunityBridgePlatform.BUZZ,
                source_channel_id=link.source_channel_id,
                source_message_id=link.source_message_id,
                delivery_type__in=[
                    CommunityBridgeDeliveryType.CREATE,
                    CommunityBridgeDeliveryType.EDIT,
                ],
                status=CommunityBridgeDeliveryStatus.COMPLETED,
            )
            .order_by("-completed_at", "-id")
        )
        latest = deliveries.first()
        source_payload = cls._mapping(latest.payload if latest else link.source_payload)
        metadata = cls._mapping(source_payload.get("metadata"))
        raw_text = str(metadata.get("slack_raw_text") or "")
        if raw_text:
            return source_payload, raw_text
        for delivery in deliveries:
            raw_text = cls._raw_text_from_receipt(
                cls._mapping(delivery.receipt.payload if delivery.receipt else {})
            )
            if raw_text:
                return source_payload, raw_text
        return source_payload, ""

    @staticmethod
    def _raw_text_from_receipt(payload):
        event = Command._mapping(payload.get("event"))
        if str(event.get("subtype") or "").strip() == "message_changed":
            return str(Command._mapping(event.get("message")).get("text") or "")
        return str(event.get("text") or "")

    @staticmethod
    def _mapping(value):
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _backfill_payload(*, link, receipt_key, source_payload, resolved_text):
        attachments = tuple(
            BridgeAttachment(
                title=str(item.get("title") or item.get("url") or "Attachment"),
                url=str(item.get("url") or ""),
            )
            for item in (source_payload.get("attachments") or [])
            if isinstance(item, dict)
        )
        return CanonicalBridgeEvent(
            receipt_key=receipt_key,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=link.source_channel_id,
            source_message_id=link.source_message_id,
            source_parent_message_id=(
                str(source_payload.get("source_parent_message_id") or "").strip()
                or link.source_parent_message_id
            ),
            source_author_id=(
                str(source_payload.get("source_author_id") or "").strip()
                or link.source_author_id
            ),
            source_author_display_name=str(
                source_payload.get("source_author_display_name") or ""
            ).strip(),
            source_author_avatar_url=str(
                source_payload.get("source_author_avatar_url") or ""
            ).strip(),
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            text=resolved_text,
            attachments=attachments,
            metadata={"backfill_version": BACKFILL_VERSION},
        ).normalized_payload()

    @staticmethod
    def _enqueue(*, link, receipt_key, payload):
        now = timezone.now()
        source_parent_message_id = str(
            payload.get("source_parent_message_id") or ""
        ).strip()
        with transaction.atomic():
            receipt, created = CommunityBridgeReceipt.objects.get_or_create(
                platform=CommunityBridgePlatform.SLACK,
                receipt_key=receipt_key,
                defaults={
                    "channel": link.channel,
                    "event_type": BACKFILL_VERSION,
                    "source_channel_id": link.source_channel_id,
                    "source_message_id": link.source_message_id,
                    "source_parent_message_id": source_parent_message_id,
                    "status": CommunityBridgeReceiptStatus.ENQUEUED,
                    "queued_delivery_count": 1,
                    "payload": {},
                    "processed_at": now,
                },
            )
            if not created:
                return False
            CommunityBridgeDelivery.objects.create(
                channel=link.channel,
                receipt=receipt,
                target_platform=CommunityBridgePlatform.BUZZ,
                source_platform=CommunityBridgePlatform.SLACK,
                delivery_type=CommunityBridgeDeliveryType.EDIT,
                status=CommunityBridgeDeliveryStatus.PENDING,
                source_event_key=receipt_key,
                source_channel_id=link.source_channel_id,
                source_message_id=link.source_message_id,
                source_parent_message_id=source_parent_message_id,
                target_channel_id=link.destination_channel_id,
                payload=payload,
                available_at=now,
            )
        return True
