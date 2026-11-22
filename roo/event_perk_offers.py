"""Automatic member-perk offers for events MLAI hosts on its Luma calendar.

A hand-made ``EventDiscountOffer`` (``configure_event_ticket_offer``) is never
changed here. For any other event the offer is created on demand from live
Luma data once ``ROO_EVENT_PERKS_AUTO`` allows it, and kept in step with the
ticket while no redemption for the event is still processing.
"""

import hashlib

from django.db import transaction
from django.utils.dateparse import parse_datetime

from integrations.services.luma_event_discounts import LumaEventDiscountService
from .models import EventDiscountOffer, EventDiscountRedemption, RewardsCatalog
from .event_ticket_policy import (
    AUTO_OFFER_REWARD_PREFIX,
    auto_perks_allowed,
    is_auto_offer,
    member_perk_points,
)


def auto_reward_code(event_id):
    return AUTO_OFFER_REWARD_PREFIX + hashlib.sha256(event_id.encode()).hexdigest()[:16].upper()


def _current(offer, pick, valid_end_at, cost):
    return bool(
        offer.is_active and offer.reward.is_active
        and offer.event_ticket_type_id == pick["event_ticket_type_id"]
        and offer.discount_type == "percent" and offer.percent_off == 100
        and offer.valid_end_at == valid_end_at and offer.valid_start_at is None
        and offer.reward.cost_points == cost
    )


def ensure_auto_offer(event_id, user, *, gateway=None, now=None):
    """Return the event's offer, creating an automatic one when eligible.

    Returns ``None`` when automatic perks are not allowed for this member.
    Raises ``LumaEventDiscountError`` with a member-safe reason when the
    event or its tickets are not eligible.
    """
    existing = EventDiscountOffer.objects.select_related("reward").filter(event_id=event_id).first()
    if existing is not None and not is_auto_offer(existing):
        return existing
    if not auto_perks_allowed(event_id, user):
        return None
    gateway = gateway or LumaEventDiscountService()
    pick = gateway.member_perk_ticket(event_id, now=now)
    valid_end_at = parse_datetime(pick["valid_end_at"])
    # The catalogue row carries the free-ticket price for admin display only;
    # each quote is priced from the live ticket.
    cost = member_perk_points(pick["cents"], "free_ticket")
    if existing is not None and _current(existing, pick, valid_end_at, cost):
        return existing
    code = auto_reward_code(event_id)
    with transaction.atomic():
        # Same lock order as reservation: catalogue row, then offer.
        RewardsCatalog.objects.select_for_update().filter(code=code).first()
        locked = EventDiscountOffer.objects.select_for_update().select_related("reward").filter(event_id=event_id).first()
        if locked is not None and not is_auto_offer(locked):
            return locked
        if locked is not None and EventDiscountRedemption.objects.filter(event_id=event_id, status="processing").exists():
            # Never move terms under an in-flight coupon; quotes re-validate anyway.
            return locked
        reward, _ = RewardsCatalog.objects.update_or_create(
            code=code,
            defaults={
                "name": f"Member perk: {pick['name']}"[:255] if pick["name"] else "Member perk",
                "description": "Half-price or free ticket for Roo Points, priced from the ticket (1 point = A$1).",
                "cost_points": cost,
                "fulfillment": "auto",
                "is_active": True,
                "max_per_user": 1,
                "stock_remaining": None,
            },
        )
        offer, _ = EventDiscountOffer.objects.update_or_create(
            event_id=event_id,
            defaults={
                "reward": reward,
                "event_ticket_type_id": pick["event_ticket_type_id"],
                "discount_type": "percent",
                "percent_off": 100,
                "cents_off": None,
                "currency": "aud",
                "is_active": True,
                "valid_start_at": None,
                "valid_end_at": valid_end_at,
            },
        )
        offer.reward = reward
        return offer
