import json
from datetime import timezone as datetime_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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
from integrations.services.community_bridge.slack import SlackBridgeClient

BACKFILL_VERSION = "slack-avatar-backfill-v1"


class Command(BaseCommand):
    help = (
        "Enqueue idempotent metadata-enriching edits for Slack messages mirrored "
        "to MLAI Chat before avatar support was deployed. Dry-run is the default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--before",
            required=True,
            help=(
                "Only inspect message links created before this ISO-8601 cutover "
                "timestamp, for example 2026-08-10T10:26:32Z."
            ),
        )
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
            help="Required with --apply to acknowledge that signed edit events will be published.",
        )

    def handle(self, *args, **options):
        cutoff = self._parse_cutoff(options["before"])
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
            cutoff=cutoff,
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

        profiles = {}
        report = {
            "after_link_id": after_link_id,
            "already_enqueued": 0,
            "before": cutoff.isoformat().replace("+00:00", "Z"),
            "candidate_count": candidate_count,
            "enqueued": 0,
            "invalid_payload": 0,
            "last_scanned_link_id": links[-1].id if links else after_link_id,
            "limit": limit,
            "mode": "apply" if apply_changes else "dry_run",
            "remaining_candidates": max(candidate_count - len(links), 0),
            "scanned": len(links),
            "skipped_no_avatar": 0,
            "slack_channel_ids": channel_ids,
            "unique_authors": 0,
            "would_enqueue": 0,
        }

        for link in links:
            receipt_key = receipt_keys[link.id]
            if receipt_key in existing_receipt_keys:
                report["already_enqueued"] += 1
                continue

            author_id = str(link.source_author_id or "").strip()
            if author_id not in profiles:
                profiles[author_id] = SlackBridgeClient.get_user_profile(author_id)
            profile = profiles[author_id]
            avatar_url = str(profile.get("avatar_url") or "").strip()
            if not avatar_url:
                report["skipped_no_avatar"] += 1
                continue

            try:
                source_payload = self._latest_successful_payload(link)
                payload = self._backfill_payload(
                    link=link,
                    receipt_key=receipt_key,
                    source_payload=source_payload,
                    display_name=str(profile.get("display_name") or "").strip(),
                    avatar_url=avatar_url,
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

        report["unique_authors"] = len(profiles)
        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _parse_cutoff(value):
        cutoff = parse_datetime(str(value or "").strip())
        if cutoff is None:
            raise CommandError("--before must be a valid ISO-8601 timestamp")
        if timezone.is_naive(cutoff):
            cutoff = cutoff.replace(tzinfo=datetime_timezone.utc)
        return cutoff.astimezone(datetime_timezone.utc)

    @staticmethod
    def _candidate_links(*, cutoff, after_link_id, channel_ids):
        post_cutover_edit = CommunityBridgeDelivery.objects.filter(
            channel_id=OuterRef("channel_id"),
            source_platform=CommunityBridgePlatform.SLACK,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_channel_id=OuterRef("source_channel_id"),
            source_message_id=OuterRef("source_message_id"),
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            completed_at__gte=cutoff,
        )
        queryset = (
            CommunityBridgeMessageLink.objects.select_related("channel")
            .filter(
                id__gt=after_link_id,
                created_at__lt=cutoff,
                source_platform=CommunityBridgePlatform.SLACK,
                destination_platform=CommunityBridgePlatform.BUZZ,
                source_deleted_at__isnull=True,
                destination_deleted_at__isnull=True,
                channel__enabled=True,
                channel__sync_edits=True,
                channel__destination_platform=CommunityBridgePlatform.BUZZ,
            )
            .exclude(source_author_id="")
            .annotate(has_post_cutover_edit=Exists(post_cutover_edit))
            .filter(has_post_cutover_edit=False)
            .order_by("id")
        )
        if channel_ids:
            queryset = queryset.filter(source_channel_id__in=channel_ids)
        return queryset

    @staticmethod
    def _receipt_key(link_id):
        return f"{BACKFILL_VERSION}:{int(link_id)}"

    @staticmethod
    def _latest_successful_payload(link):
        payload = (
            CommunityBridgeDelivery.objects.filter(
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
            .values_list("payload", flat=True)
            .first()
        )
        source_payload = payload if payload is not None else link.source_payload
        if not isinstance(source_payload, dict):
            raise ValueError("latest delivered payload is not an object")
        return dict(source_payload)

    @staticmethod
    def _backfill_payload(
        *, link, receipt_key, source_payload, display_name, avatar_url
    ):
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
            source_author_id=link.source_author_id,
            source_author_display_name=display_name or link.source_author_id,
            source_author_avatar_url=avatar_url,
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            text=str(source_payload.get("text") or ""),
            attachments=attachments,
            metadata=source_payload.get("metadata") or {},
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
