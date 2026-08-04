from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeMessageLink,
)


class Command(BaseCommand):
    help = "Requeue one investigated dead community-bridge delivery without changing its idempotency identity."

    def add_arguments(self, parser):
        parser.add_argument("delivery_id", type=int)
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--refresh-event-timestamp", action="store_true")
        parser.add_argument("--confirm-stale-relay-timestamp", action="store_true")
        parser.add_argument("--confirm-no-destination-event", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("--confirm is required after investigating the dead letter.")
        refresh_timestamp = bool(options["refresh_event_timestamp"])
        if refresh_timestamp and not options["confirm_stale_relay_timestamp"]:
            raise CommandError(
                "--confirm-stale-relay-timestamp is required when refreshing the event timestamp"
            )
        if refresh_timestamp and not options["confirm_no_destination_event"]:
            raise CommandError(
                "--confirm-no-destination-event is required when refreshing the event timestamp"
            )

        with transaction.atomic():
            try:
                delivery = CommunityBridgeDelivery.objects.select_for_update().get(
                    id=options["delivery_id"]
                )
            except CommunityBridgeDelivery.DoesNotExist as exc:
                raise CommandError("community bridge delivery was not found") from exc
            if delivery.status != CommunityBridgeDeliveryStatus.DEAD:
                raise CommandError("only a dead community bridge delivery can be requeued")
            if refresh_timestamp and CommunityBridgeMessageLink.objects.filter(
                source_platform=delivery.source_platform,
                source_channel_id=delivery.source_channel_id,
                source_message_id=delivery.source_message_id,
                destination_platform=delivery.target_platform,
            ).exists():
                raise CommandError(
                    "cannot refresh the event timestamp after a destination message link exists"
                )

            now = timezone.now()
            delivery.status = CommunityBridgeDeliveryStatus.PENDING
            delivery.attempts = 0
            delivery.available_at = now
            delivery.locked_at = None
            delivery.completed_at = None
            delivery.last_error = ""
            update_fields = [
                "status",
                "attempts",
                "available_at",
                "locked_at",
                "completed_at",
                "last_error",
                "updated_at",
            ]
            if refresh_timestamp:
                delivery.created_at = now
                update_fields.append("created_at")
            delivery.save(update_fields=update_fields)

        timestamp_note = " with a refreshed event timestamp" if refresh_timestamp else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Requeued community bridge delivery {delivery.id}{timestamp_note} "
                "and its original delivery ID."
            )
        )
