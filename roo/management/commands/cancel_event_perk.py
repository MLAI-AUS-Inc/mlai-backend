"""Cancel one issued event coupon and refund its exact Roo Points."""

import json
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.services.luma_event_discounts import LumaEventDiscountError, LumaEventDiscountService
from roo.event_discounts import EventDiscountService
from roo.models import EventDiscountRedemption
from roo.services import PointsService


class Command(BaseCommand):
    """Preview by default. ``--apply`` disables the unused coupon first, then refunds."""

    help = "Cancel an unused Roo event coupon: disable it in Luma, then refund the points."

    def add_arguments(self, parser):
        parser.add_argument("--redemption", required=True, help="EventDiscountRedemption id")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            redemption_id = uuid.UUID(options["redemption"])
        except ValueError:
            raise CommandError("Use a redemption id.") from None
        row = EventDiscountRedemption.objects.select_related("quote", "user").filter(pk=redemption_id).first()
        if row is None:
            raise CommandError("Redemption not found.")
        if row.status != "fulfilled":
            raise CommandError(f"Only an issued coupon can be cancelled (status={row.status}).")
        gateway = LumaEventDiscountService()
        try:
            coupon = gateway.find_coupon(row.event_id, row.coupon_code)
        except LumaEventDiscountError as exc:
            raise CommandError(f"Luma lookup failed: {exc.code}") from None
        if coupon is None:
            raise CommandError("The coupon is not in Luma. Review by hand; nothing changed.")
        remaining = coupon.get("remaining_count")
        plan = {
            "redemption": str(row.pk), "event_id": row.event_id,
            "points": row.quote.snapshot["points_cost"], "remaining_count": remaining,
            "applied": False,
        }
        if remaining != 1:
            raise CommandError("The coupon was already used or disabled. Nothing was refunded.")
        if not options["apply"]:
            self.stdout.write(json.dumps(plan))
            return
        try:
            gateway.disable_coupon(row.event_id, row.coupon_code)
        except LumaEventDiscountError as exc:
            raise CommandError(f"Coupon not disabled ({exc.code}). Nothing was refunded.") from None
        with transaction.atomic():
            locked = EventDiscountService._locked(row.pk)
            if locked.status != "fulfilled":
                raise CommandError("The redemption changed while cancelling. Nothing was refunded.")
            PointsService.refund(
                user=locked.user, delta=locked.quote.snapshot["points_cost"], source="EVENT",
                description=f"Event discount cancelled: {locked.quote.snapshot['name']}",
                created_by_slack_id="", idempotency_key=f"event_discount_refund:{locked.pk}",
                reference_type="EVENT_DISCOUNT", reference_id=str(locked.pk),
                purchased_delta_microroo=locked.purchased_microroo, reverse_lifetime_spent=True,
            )
            locked.status = EventDiscountRedemption.Status.CANCELLED
            locked.last_error_code = "cancelled_by_operator"
            locked.save(update_fields=("status", "last_error_code", "updated_at"))
        plan["applied"] = True
        plan["cancelled_at"] = timezone.now().isoformat()
        self.stdout.write(json.dumps(plan))
