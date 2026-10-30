"""Preview or configure one validated, fully covered community event ticket."""

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.services.luma_event_discounts import (
    LumaEventDiscountError,
    LumaEventDiscountService,
)
from roo.event_ticket_policy import EVENT_TICKET_POINTS
from roo.models import EventDiscountOffer, EventDiscountRedemption, RewardsCatalog


class Command(BaseCommand):
    """Use live Luma eligibility before writing a fixed-price, 100% offer."""

    help = (
        "Preview a 32-point free ticket below A$100; --apply saves the validated offer."
    )

    def add_arguments(self, parser):
        parser.add_argument("--event-id", required=True)
        parser.add_argument("--ticket-type-id", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        gateway = LumaEventDiscountService()
        try:
            event = gateway.get_event(options["event_id"])
            tickets = [
                ticket
                for ticket in gateway.get_ticket_types(options["event_id"])
                if ticket.get("id") == options["ticket_type_id"]
            ]
            if len(tickets) != 1:
                raise CommandError("The exact ticket type is unavailable.")
            deadlines = [event.get("start_at"), tickets[0].get("valid_end_at")]
            parsed = [parse_datetime(value) for value in deadlines if value is not None]
            if not parsed or any(
                value is None or timezone.is_naive(value) for value in parsed
            ):
                raise CommandError(
                    "The event or ticket sales deadline could not be verified."
                )
            snapshot = gateway.validate_offer(
                {
                    "event_id": options["event_id"],
                    "event_ticket_type_id": options["ticket_type_id"],
                    "discount": {"discount_type": "percent", "percent_off": 100},
                    "valid_end_at": min(parsed).isoformat(),
                }
            )
        except LumaEventDiscountError as exc:
            raise CommandError(
                f"Offer not configured: {exc.code}. {exc.message}"
            ) from None
        plan = {
            "event_id": snapshot["event_id"],
            "event_name": snapshot["name"],
            "ticket_type_id": snapshot["event_ticket_type_id"],
            "ticket_cents": snapshot["ticket"]["cents"],
            "points_cost": EVENT_TICKET_POINTS,
            "percent_off": 100,
            "valid_end_at": snapshot["valid_end_at"],
            "applied": bool(options["apply"]),
        }
        if options["apply"]:
            with transaction.atomic():
                existing = EventDiscountOffer.objects.filter(
                    event_id=snapshot["event_id"]
                ).first()
                code = (
                    existing.reward_id
                    if existing
                    else "EVENT_TICKET_"
                    + hashlib.sha256(snapshot["event_id"].encode())
                    .hexdigest()[:16]
                    .upper()
                )
                # Match reservation's catalogue-before-offer lock order. Once
                # this lock is held, an existing offer cannot reserve new points
                # between the processing check and its terms update.
                RewardsCatalog.objects.select_for_update().filter(code=code).first()
                locked = (
                    EventDiscountOffer.objects.select_for_update()
                    .filter(event_id=snapshot["event_id"])
                    .first()
                )
                if locked and locked.reward_id != code:
                    raise CommandError(
                        "The offer changed. Preview it again before applying."
                    )
                # Never replace terms while an externally attempted coupon may
                # still need reconciliation. Existing fulfilled receipts persist.
                if EventDiscountRedemption.objects.filter(
                    event_id=snapshot["event_id"], status="processing"
                ).exists():
                    raise CommandError(
                        "Resolve this event's processing redemptions before changing its offer."
                    )
                reward, _ = RewardsCatalog.objects.update_or_create(
                    code=code,
                    defaults={
                        "name": f"Free ticket: {snapshot['name']}"[:255],
                        "description": "One ticket priced below A$100, fully covered for 32 Roo Points. Complete registration in Luma.",
                        "cost_points": EVENT_TICKET_POINTS,
                        "fulfillment": "auto",
                        "is_active": True,
                        "max_per_user": 1,
                    },
                )
                EventDiscountOffer.objects.update_or_create(
                    event_id=snapshot["event_id"],
                    defaults={
                        "reward": reward,
                        "event_ticket_type_id": snapshot["event_ticket_type_id"],
                        "discount_type": "percent",
                        "percent_off": 100,
                        "cents_off": None,
                        "currency": "aud",
                        "is_active": True,
                        "valid_end_at": parse_datetime(snapshot["valid_end_at"]),
                    },
                )
        self.stdout.write(json.dumps(plan))
