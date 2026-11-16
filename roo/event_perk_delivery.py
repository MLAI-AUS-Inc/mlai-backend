"""Email a member their issued event coupon. Best effort: never touches points."""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.dateparse import parse_datetime

from integrations.services.luma_event_discounts import LumaEventDiscountError, LumaEventDiscountService

logger = logging.getLogger(__name__)
PERK_LABELS = {"half_price": "Half-price ticket", "free_ticket": "Free ticket"}


def _label(snapshot):
    perk = snapshot.get("member_perk")
    if perk in PERK_LABELS:
        return PERK_LABELS[perk]
    discount = snapshot.get("discount") or {}
    if discount.get("discount_type") == "percent" and discount.get("percent_off") == 100:
        return PERK_LABELS["free_ticket"]
    return "Ticket discount"


def _expires_display(snapshot):
    expires = parse_datetime(str(snapshot.get("valid_end_at") or ""))
    if expires is None:
        return "the event starts"
    try:
        expires = expires.astimezone(ZoneInfo(snapshot.get("timezone") or "Australia/Melbourne"))
    except (ZoneInfoNotFoundError, ValueError):
        expires = expires.astimezone(ZoneInfo("Australia/Melbourne"))
    return expires.strftime("%a %-d %b %Y, %-I:%M %p %Z")


def email_event_perk_code(row):
    """Send the code once per redemption. Returns True only when Resend accepted it."""
    from system_email import api as system_email

    if row.status != "fulfilled" or not row.coupon_code or not getattr(row.user, "email", ""):
        return False
    try:
        if not system_email.routes_to_resend("event_perk_code"):
            return False
        snapshot = row.quote.snapshot
        system_email.send(
            "event_perk_code",
            to=row.user.email,
            idempotency_key=f"event-perk/{row.pk}",
            user_id=row.user_id,
            context={
                "first_name": row.user.first_name or "",
                "event_name": snapshot.get("name") or "your MLAI event",
                "perk_label": _label(snapshot),
                "coupon_code": row.coupon_code,
                "checkout_url": LumaEventDiscountService.coupon_url(snapshot["url"], row.coupon_code),
                "points_cost": snapshot["points_cost"],
                "expires_display": _expires_display(snapshot),
                "requires_approval": bool((snapshot.get("ticket") or {}).get("requires_approval")),
            },
        )
        return True
    except (LumaEventDiscountError, KeyError) as exc:
        logger.warning("Event perk email skipped redemption=%s error_type=%s", row.pk, type(exc).__name__)
    except Exception as exc:  # The code is already saved in the app; mail must not break issuance.
        logger.warning("Event perk email failed redemption=%s error_type=%s", row.pk, type(exc).__name__)
    return False
