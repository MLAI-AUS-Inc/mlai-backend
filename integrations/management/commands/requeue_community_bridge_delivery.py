from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations.models import (
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
)


class Command(BaseCommand):
    help = "Requeue one investigated dead community-bridge delivery without changing its idempotency identity."

    def add_arguments(self, parser):
        parser.add_argument("delivery_id", type=int)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("--confirm is required after investigating the dead letter.")
        try:
            delivery = CommunityBridgeDelivery.objects.get(id=options["delivery_id"])
        except CommunityBridgeDelivery.DoesNotExist as exc:
            raise CommandError("community bridge delivery was not found") from exc
        if delivery.status != CommunityBridgeDeliveryStatus.DEAD:
            raise CommandError("only a dead community bridge delivery can be requeued")

        delivery.status = CommunityBridgeDeliveryStatus.PENDING
        delivery.attempts = 0
        delivery.available_at = timezone.now()
        delivery.locked_at = None
        delivery.completed_at = None
        delivery.last_error = ""
        delivery.save(
            update_fields=[
                "status",
                "attempts",
                "available_at",
                "locked_at",
                "completed_at",
                "last_error",
                "updated_at",
            ]
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Requeued community bridge delivery {delivery.id} with its original delivery ID."
            )
        )
