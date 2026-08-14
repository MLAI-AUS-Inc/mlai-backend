import json
import time
from datetime import datetime, timezone as datetime_timezone

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
from integrations.services.community_bridge.contracts import (
    BridgeAttachment,
    CanonicalBridgeEvent,
)
from integrations.services.community_bridge.formatting import normalize_slack_files
from integrations.services.community_bridge.slack import SlackBridgeClient


REPAIR_VERSION = "slack-thread-repair-v1"
PHASES = ("root", "delete_orphans", "recreate_orphans")
TERMINAL_STATUSES = {
    CommunityBridgeDeliveryStatus.COMPLETED,
    CommunityBridgeDeliveryStatus.DEAD,
}


class Command(BaseCommand):
    help = (
        "Repair one Slack thread whose app-authored root was not mirrored. "
        "Run root, delete_orphans, then recreate_orphans. Dry-run is the default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--slack-channel-id", required=True)
        parser.add_argument("--root-message-id", required=True)
        parser.add_argument("--phase", required=True, choices=PHASES)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--confirm-historical-repair",
            action="store_true",
            help="Required with --apply because relay events will be published.",
        )
        parser.add_argument(
            "--wait-seconds",
            type=int,
            default=0,
            help="Wait for this phase's deliveries to finish (0-300 seconds).",
        )

    def handle(self, *args, **options):
        channel_id = str(options["slack_channel_id"] or "").strip()
        root_message_id = str(options["root_message_id"] or "").strip()
        phase = str(options["phase"] or "").strip()
        apply_changes = bool(options["apply"])
        wait_seconds = int(options["wait_seconds"] or 0)
        if not channel_id or not root_message_id:
            raise CommandError("Slack channel and root message IDs are required")
        if wait_seconds < 0 or wait_seconds > 300:
            raise CommandError("--wait-seconds must be between 0 and 300")
        if apply_changes and not options["confirm_historical_repair"]:
            raise CommandError("--confirm-historical-repair is required with --apply")
        if not SlackBridgeClient.is_configured():
            raise CommandError("SLACK_BRIDGE_BOT_TOKEN is required")

        try:
            channel = CommunityBridgeChannel.objects.get(
                slack_channel_id=channel_id,
                destination_platform=CommunityBridgePlatform.BUZZ,
                enabled=True,
            )
        except CommunityBridgeChannel.DoesNotExist as exc:
            raise CommandError("An enabled MLAI Chat mapping was not found") from exc
        if not channel.sync_replies or not channel.sync_deletes:
            raise CommandError("The mapping must enable reply and delete synchronization")

        messages = SlackBridgeClient.get_thread_messages(
            channel_id=channel_id,
            root_message_id=root_message_id,
        )
        root, replies = self._validated_thread(messages, root_message_id=root_message_id)
        root_link = self._active_link(channel=channel, source_message_id=root_message_id)
        root_destination_id = str(
            getattr(root_link, "destination_message_id", "") or ""
        ).strip()
        report = {
            "apply": apply_changes,
            "channel_id": channel_id,
            "enqueued": 0,
            "existing": 0,
            "phase": phase,
            "reply_count": len(replies),
            "root_destination_present": bool(root_destination_id),
            "root_message_id": root_message_id,
            "skipped": 0,
            "would_enqueue": 0,
        }
        delivery_ids = []

        if phase == "root":
            if root_destination_id:
                report["existing"] += 1
            else:
                self._plan_or_enqueue(
                    report=report,
                    delivery_ids=delivery_ids,
                    apply_changes=apply_changes,
                    channel=channel,
                    message=root,
                    root_message_id=root_message_id,
                    delivery_type=CommunityBridgeDeliveryType.CREATE,
                    receipt_suffix="root",
                    preserve_source_time=True,
                )
        else:
            if not root_destination_id:
                report["blocked"] = "root_mapping_missing"
                self.stdout.write(json.dumps(report, sort_keys=True))
                if apply_changes:
                    raise CommandError("Run and complete the root phase first")
                return
            for reply in replies:
                source_message_id = str(reply.get("ts") or "").strip()
                link = self._link(channel=channel, source_message_id=source_message_id)
                active = bool(
                    link
                    and link.source_deleted_at is None
                    and link.destination_deleted_at is None
                )
                correctly_threaded = bool(
                    active
                    and str(link.destination_parent_message_id or "").strip()
                    == root_destination_id
                )
                if phase == "delete_orphans":
                    if correctly_threaded or not active:
                        report["skipped"] += 1
                        continue
                    self._plan_or_enqueue(
                        report=report,
                        delivery_ids=delivery_ids,
                        apply_changes=apply_changes,
                        channel=channel,
                        message=reply,
                        root_message_id=root_message_id,
                        delivery_type=CommunityBridgeDeliveryType.DELETE,
                        receipt_suffix="delete",
                        preserve_source_time=False,
                    )
                    continue
                if correctly_threaded:
                    report["existing"] += 1
                    continue
                if active:
                    report["skipped"] += 1
                    continue
                self._plan_or_enqueue(
                    report=report,
                    delivery_ids=delivery_ids,
                    apply_changes=apply_changes,
                    channel=channel,
                    message=reply,
                    root_message_id=root_message_id,
                    delivery_type=CommunityBridgeDeliveryType.CREATE,
                    receipt_suffix="recreate",
                    preserve_source_time=True,
                )

        if apply_changes and delivery_ids and wait_seconds:
            report["delivery_statuses"] = self._wait_for_deliveries(
                delivery_ids=delivery_ids,
                wait_seconds=wait_seconds,
            )
        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _validated_thread(messages, *, root_message_id):
        if not messages:
            raise CommandError("Slack returned no messages for this thread")
        root = dict(messages[0])
        if str(root.get("ts") or "").strip() != root_message_id:
            raise CommandError("Slack returned a different thread root")
        result = []
        for message in messages:
            normalized = dict(message)
            message_id = str(normalized.get("ts") or "").strip()
            user_id = str(normalized.get("user") or "").strip()
            if not message_id or not user_id or user_id[0:1] not in {"U", "W"}:
                raise CommandError("Every repaired Slack message must have a stable user ID")
            if message_id != root_message_id:
                if str(normalized.get("thread_ts") or "").strip() != root_message_id:
                    raise CommandError("Slack returned a reply from another thread")
                result.append(normalized)
        return root, result

    @staticmethod
    def _link(*, channel, source_message_id):
        return CommunityBridgeMessageLink.objects.filter(
            channel=channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=channel.slack_channel_id,
            source_message_id=source_message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
        ).first()

    @classmethod
    def _active_link(cls, *, channel, source_message_id):
        link = cls._link(channel=channel, source_message_id=source_message_id)
        if not link or link.source_deleted_at or link.destination_deleted_at:
            return None
        return link

    @classmethod
    def _plan_or_enqueue(
        cls,
        *,
        report,
        delivery_ids,
        apply_changes,
        channel,
        message,
        root_message_id,
        delivery_type,
        receipt_suffix,
        preserve_source_time,
    ):
        if not apply_changes:
            report["would_enqueue"] += 1
            return
        delivery, created = cls._enqueue(
            channel=channel,
            message=message,
            root_message_id=root_message_id,
            delivery_type=delivery_type,
            receipt_suffix=receipt_suffix,
            preserve_source_time=preserve_source_time,
        )
        if created:
            report["enqueued"] += 1
        else:
            report["existing"] += 1
        if delivery and delivery.status == CommunityBridgeDeliveryStatus.DEAD:
            raise CommandError(
                f"Repair delivery {delivery.id} is dead and requires operator review"
            )
        if delivery and delivery.status not in TERMINAL_STATUSES:
            delivery_ids.append(delivery.id)

    @classmethod
    def _enqueue(
        cls,
        *,
        channel,
        message,
        root_message_id,
        delivery_type,
        receipt_suffix,
        preserve_source_time,
    ):
        message_id = str(message.get("ts") or "").strip()
        is_reply = message_id != root_message_id
        parent_message_id = root_message_id if is_reply else ""
        receipt_key = (
            f"{REPAIR_VERSION}:{receipt_suffix}:{channel.slack_channel_id}:{message_id}"
        )
        payload = cls._payload(
            message=message,
            channel=channel,
            receipt_key=receipt_key,
            parent_message_id=parent_message_id,
            delivery_type=delivery_type,
        )
        now = timezone.now()
        with transaction.atomic():
            receipt, created = CommunityBridgeReceipt.objects.get_or_create(
                platform=CommunityBridgePlatform.SLACK,
                receipt_key=receipt_key,
                defaults={
                    "channel": channel,
                    "event_type": REPAIR_VERSION,
                    "source_channel_id": channel.slack_channel_id,
                    "source_message_id": message_id,
                    "source_parent_message_id": parent_message_id,
                    "status": CommunityBridgeReceiptStatus.ENQUEUED,
                    "queued_delivery_count": 1,
                    "payload": {},
                    "processed_at": now,
                },
            )
            if not created:
                return receipt.deliveries.order_by("id").first(), False
            delivery = CommunityBridgeDelivery.objects.create(
                channel=channel,
                receipt=receipt,
                target_platform=CommunityBridgePlatform.BUZZ,
                source_platform=CommunityBridgePlatform.SLACK,
                delivery_type=delivery_type,
                status=CommunityBridgeDeliveryStatus.PENDING,
                source_event_key=receipt_key,
                source_channel_id=channel.slack_channel_id,
                source_message_id=message_id,
                source_parent_message_id=parent_message_id,
                target_channel_id=channel.destination_channel_id,
                payload=payload,
                available_at=now,
            )
            if preserve_source_time:
                source_time = datetime.fromtimestamp(
                    int(message_id.split(".", 1)[0]),
                    tz=datetime_timezone.utc,
                )
                CommunityBridgeDelivery.objects.filter(id=delivery.id).update(
                    created_at=source_time
                )
                delivery.created_at = source_time
        return delivery, True

    @staticmethod
    def _payload(*, message, channel, receipt_key, parent_message_id, delivery_type):
        raw_text = str(message.get("text") or "")
        attachments = tuple(
            BridgeAttachment(title=item["title"], url=item["url"])
            for item in normalize_slack_files(message.get("files") or [])
        )
        if delivery_type == CommunityBridgeDeliveryType.DELETE:
            raw_text = ""
            attachments = ()
        return CanonicalBridgeEvent(
            receipt_key=receipt_key,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=channel.slack_channel_id,
            source_message_id=str(message.get("ts") or "").strip(),
            source_parent_message_id=parent_message_id,
            source_author_id=str(message.get("user") or "").strip(),
            source_author_display_name="",
            delivery_type=delivery_type,
            text=raw_text,
            attachments=attachments,
            metadata={
                "backfill_version": REPAIR_VERSION,
                "slack_raw_text": raw_text,
            },
        ).normalized_payload()

    @staticmethod
    def _wait_for_deliveries(*, delivery_ids, wait_seconds):
        deadline = time.monotonic() + wait_seconds
        while True:
            statuses = dict(
                CommunityBridgeDelivery.objects.filter(id__in=delivery_ids).values_list(
                    "id", "status"
                )
            )
            if len(statuses) == len(set(delivery_ids)) and all(
                status in TERMINAL_STATUSES for status in statuses.values()
            ):
                if any(
                    status == CommunityBridgeDeliveryStatus.DEAD
                    for status in statuses.values()
                ):
                    raise CommandError("At least one repair delivery is dead")
                return statuses
            if time.monotonic() >= deadline:
                raise CommandError("Timed out waiting for repair deliveries")
            time.sleep(1)
