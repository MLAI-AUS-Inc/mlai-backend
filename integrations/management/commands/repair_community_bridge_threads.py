import json
from datetime import timezone as datetime_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
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
from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgeError,
)
from integrations.services.community_bridge.formatting import build_mirrored_text
from integrations.services.community_bridge.identity import verified_identity_for_slack

REPAIR_VERSION = "slack-buzz-thread-repair-v1"


class Command(BaseCommand):
    help = (
        "Audit historical Slack-to-MLAI Chat reply mappings and deletion state, "
        "and optionally repair safe cases. Dry-run is the default."
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
            help="Restrict to one Slack channel ID; repeat for several channels.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Publish deterministic repairs and update message mappings.",
        )
        parser.add_argument(
            "--confirm-republish",
            action="store_true",
            help=(
                "Required with --apply. Acknowledges that corrected reply events "
                "and deletion events will be published to MLAI Chat."
            ),
        )

    def handle(self, *args, **options):
        after_link_id = int(options["after_link_id"])
        limit = int(options["limit"])
        apply_changes = bool(options["apply"])
        if after_link_id < 0:
            raise CommandError("--after-link-id must be zero or greater")
        if limit < 1 or limit > 5000:
            raise CommandError("--limit must be between 1 and 5000")
        if apply_changes and not options["confirm_republish"]:
            raise CommandError("--confirm-republish is required with --apply")
        if apply_changes and not BuzzBridgeClient.is_configured():
            raise CommandError("The MLAI Chat bridge adapter is not configured")

        channel_ids = sorted(
            {
                str(channel_id or "").strip()
                for channel_id in options["slack_channel_id"]
                if str(channel_id or "").strip()
            }
        )
        queryset = self._links(
            after_link_id=after_link_id,
            channel_ids=channel_ids,
        )
        candidate_count = queryset.count()
        links = list(queryset[:limit])
        parent_destinations = self._parent_destinations(links)
        report = {
            "after_link_id": after_link_id,
            "candidate_count": candidate_count,
            "failed": 0,
            "last_scanned_link_id": links[-1].id if links else after_link_id,
            "limit": limit,
            "manual_review_destination_only_deletes": 0,
            "missing_parent_mappings": 0,
            "mode": "apply" if apply_changes else "dry_run",
            "remaining_candidates": max(candidate_count - len(links), 0),
            "repaired_deleted_destinations": 0,
            "repaired_thread_parents": 0,
            "scanned": len(links),
            "slack_channel_ids": channel_ids,
            "would_repair_deleted_destinations": 0,
            "would_repair_thread_parents": 0,
        }

        for link in links:
            source_deleted = link.source_deleted_at is not None
            destination_deleted = link.destination_deleted_at is not None
            if destination_deleted and not source_deleted:
                report["manual_review_destination_only_deletes"] += 1
                continue
            if source_deleted and not destination_deleted:
                if not apply_changes:
                    report["would_repair_deleted_destinations"] += 1
                    continue
                try:
                    self._repair_stale_delete(link)
                except (BuzzBridgeError, RuntimeError, TypeError, ValueError) as exc:
                    report["failed"] += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"Delete-state repair failed for link {link.id}: {exc}"
                        )
                    )
                else:
                    report["repaired_deleted_destinations"] += 1
                continue
            if source_deleted or destination_deleted or not link.source_parent_message_id:
                continue

            parent_destination = parent_destinations.get(
                (link.source_channel_id, link.source_parent_message_id),
                "",
            )
            if not parent_destination:
                report["missing_parent_mappings"] += 1
                continue
            if link.destination_parent_message_id == parent_destination:
                continue
            if not apply_changes:
                report["would_repair_thread_parents"] += 1
                continue
            try:
                self._repair_thread_parent(link, parent_destination)
            except (BuzzBridgeError, RuntimeError, TypeError, ValueError) as exc:
                report["failed"] += 1
                self.stderr.write(
                    self.style.WARNING(
                        f"Thread-parent repair failed for link {link.id}: {exc}"
                    )
                )
            else:
                report["repaired_thread_parents"] += 1

        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _links(*, after_link_id, channel_ids):
        queryset = (
            CommunityBridgeMessageLink.objects.select_related("channel")
            .filter(
                id__gt=after_link_id,
                source_platform=CommunityBridgePlatform.SLACK,
                destination_platform=CommunityBridgePlatform.BUZZ,
                channel__destination_platform=CommunityBridgePlatform.BUZZ,
            )
            .filter(
                Q(source_parent_message_id__gt="")
                | Q(source_deleted_at__isnull=False, destination_deleted_at__isnull=True)
                | Q(source_deleted_at__isnull=True, destination_deleted_at__isnull=False)
            )
            .order_by("id")
        )
        if channel_ids:
            queryset = queryset.filter(source_channel_id__in=channel_ids)
        return queryset

    @staticmethod
    def _parent_destinations(links):
        parent_keys = {
            (link.source_channel_id, link.source_parent_message_id)
            for link in links
            if link.source_parent_message_id
        }
        if not parent_keys:
            return {}
        channel_ids = {item[0] for item in parent_keys}
        message_ids = {item[1] for item in parent_keys}
        parents = CommunityBridgeMessageLink.objects.filter(
            source_platform=CommunityBridgePlatform.SLACK,
            destination_platform=CommunityBridgePlatform.BUZZ,
            source_channel_id__in=channel_ids,
            source_message_id__in=message_ids,
        )
        return {
            (parent.source_channel_id, parent.source_message_id): (
                parent.destination_message_id
            )
            for parent in parents
            if (parent.source_channel_id, parent.source_message_id) in parent_keys
        }

    def _repair_thread_parent(self, link, parent_destination):
        receipt = self._repair_receipt(link, "parent")
        payload = self._latest_payload(link)
        provenance = self._provenance(link, payload)
        original_created_at = self._original_created_at(link)
        old_destination = link.destination_message_id
        try:
            response = BuzzBridgeClient.deliver(
                delivery_id=f"{REPAIR_VERSION}:{link.id}:create",
                created_at=int(original_created_at.timestamp()),
                operation=CommunityBridgeDeliveryType.CREATE,
                channel_id=link.destination_channel_id,
                text=build_mirrored_text(
                    destination_platform=CommunityBridgePlatform.BUZZ,
                    source_platform=CommunityBridgePlatform.SLACK,
                    author_display_name=(
                        provenance["source_author_display_name"]
                        or link.source_author_id
                    ),
                    body=str(payload.get("text") or ""),
                    attachments=payload.get("attachments") or [],
                ),
                parent_message_id=parent_destination,
                **provenance,
            )
            if response["parent_message_id"] != parent_destination:
                raise RuntimeError("MLAI Chat adapter returned the wrong reply parent")
            BuzzBridgeClient.deliver(
                delivery_id=f"{REPAIR_VERSION}:{link.id}:delete-old",
                created_at=max(
                    int(receipt.created_at.timestamp()),
                    int(original_created_at.timestamp()) + 1,
                ),
                operation=CommunityBridgeDeliveryType.DELETE,
                channel_id=link.destination_channel_id,
                text="",
                target_message_id=old_destination,
                **provenance,
            )
            with transaction.atomic():
                locked = CommunityBridgeMessageLink.objects.select_for_update().get(
                    id=link.id
                )
                if locked.destination_message_id != old_destination:
                    raise RuntimeError("message mapping changed during repair")
                locked.destination_message_id = response["message_id"]
                locked.destination_parent_message_id = parent_destination
                locked.destination_payload = response
                locked.save(
                    update_fields=[
                        "destination_message_id",
                        "destination_parent_message_id",
                        "destination_payload",
                        "updated_at",
                    ]
                )
                self._complete_receipt(receipt)
        except Exception as exc:
            self._fail_receipt(receipt, exc)
            raise

    def _repair_stale_delete(self, link):
        receipt = self._repair_receipt(link, "delete")
        payload = self._latest_payload(link)
        provenance = self._provenance(link, payload)
        try:
            BuzzBridgeClient.deliver(
                delivery_id=f"{REPAIR_VERSION}:{link.id}:delete",
                created_at=int(receipt.created_at.timestamp()),
                operation=CommunityBridgeDeliveryType.DELETE,
                channel_id=link.destination_channel_id,
                text="",
                target_message_id=link.destination_message_id,
                **provenance,
            )
            now = timezone.now()
            with transaction.atomic():
                locked = CommunityBridgeMessageLink.objects.select_for_update().get(
                    id=link.id
                )
                if locked.destination_message_id != link.destination_message_id:
                    raise RuntimeError("message mapping changed during repair")
                locked.destination_deleted_at = locked.source_deleted_at or now
                locked.save(update_fields=["destination_deleted_at", "updated_at"])
                self._complete_receipt(receipt)
        except Exception as exc:
            self._fail_receipt(receipt, exc)
            raise

    @staticmethod
    def _repair_receipt(link, phase):
        key = f"{REPAIR_VERSION}:{phase}:{link.id}"
        receipt, _ = CommunityBridgeReceipt.objects.get_or_create(
            platform=CommunityBridgePlatform.SLACK,
            receipt_key=key,
            defaults={
                "channel": link.channel,
                "event_type": REPAIR_VERSION,
                "source_channel_id": link.source_channel_id,
                "source_message_id": link.source_message_id,
                "source_parent_message_id": link.source_parent_message_id,
                "status": CommunityBridgeReceiptStatus.ACCEPTED,
                "queued_delivery_count": 0,
                "payload": {},
            },
        )
        return receipt

    @staticmethod
    def _complete_receipt(receipt):
        now = timezone.now()
        CommunityBridgeReceipt.objects.filter(id=receipt.id).update(
            status=CommunityBridgeReceiptStatus.ACCEPTED,
            error_text="",
            processed_at=now,
            updated_at=now,
        )

    @staticmethod
    def _fail_receipt(receipt, exc):
        now = timezone.now()
        CommunityBridgeReceipt.objects.filter(id=receipt.id).update(
            status=CommunityBridgeReceiptStatus.FAILED,
            error_text=f"{exc.__class__.__name__}: {exc}"[:2000],
            processed_at=now,
            updated_at=now,
        )

    @staticmethod
    def _latest_payload(link):
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
        result = payload if payload is not None else link.source_payload
        if not isinstance(result, dict):
            raise TypeError("latest delivered payload is not an object")
        return dict(result)

    @staticmethod
    def _original_created_at(link):
        created_at = (
            CommunityBridgeDelivery.objects.filter(
                channel_id=link.channel_id,
                source_platform=CommunityBridgePlatform.SLACK,
                target_platform=CommunityBridgePlatform.BUZZ,
                source_channel_id=link.source_channel_id,
                source_message_id=link.source_message_id,
                delivery_type=CommunityBridgeDeliveryType.CREATE,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
            )
            .order_by("created_at", "id")
            .values_list("created_at", flat=True)
            .first()
        )
        return created_at or link.created_at.astimezone(datetime_timezone.utc)

    @staticmethod
    def _provenance(link, payload):
        identity = verified_identity_for_slack(
            slack_workspace_id=link.channel.slack_workspace_id,
            slack_user_id=link.source_author_id,
        )
        metadata = dict(payload.get("metadata") or {})
        return {
            "source_workspace_id": link.channel.slack_workspace_id,
            "source_channel_id": link.source_channel_id,
            "source_message_id": str(
                metadata.get("slack_message_id") or link.source_message_id
            ).strip(),
            "source_author_id": link.source_author_id,
            "source_author_display_name": str(
                payload.get("source_author_display_name") or ""
            ).strip(),
            "source_author_avatar_url": str(
                payload.get("source_author_avatar_url") or ""
            ).strip(),
            "linked_pubkey": str((identity or {}).get("buzz_pubkey") or ""),
            "linked_profile_id": str(
                (identity or {}).get("user_profile_id") or ""
            ),
        }
