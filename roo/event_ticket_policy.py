"""Approved price and ticket-value boundary for community event rewards."""

from django.conf import settings

EVENT_TICKET_POINTS = 32
EVENT_HALF_TICKET_POINTS = 16
EVENT_TICKET_MAX_CENTS_EXCLUSIVE = 10_000

# Member perks priced from the ticket: 1 Roo Point = A$1. Half price costs half
# the ticket; a free ticket costs 90% of it (a better rate per dollar saved).
MEMBER_PERK_PERCENT_OFF = {"half_price": 50, "free_ticket": 100}
MEMBER_PERK_POINTS_RATE_PERCENT = {"half_price": 50, "free_ticket": 90}
AUTO_OFFER_REWARD_PREFIX = "EVENT_PERK_"


def member_perk_points(ticket_cents, perk):
    """Whole Roo Points for one perk on a ticket, always rounded up."""
    if perk not in MEMBER_PERK_POINTS_RATE_PERCENT:
        raise ValueError("unknown member perk")
    if isinstance(ticket_cents, bool) or not isinstance(ticket_cents, int) or ticket_cents <= 0:
        raise ValueError("ticket price must be positive whole cents")
    # cents * rate% / 100 cents-per-point, ceiling in integer arithmetic.
    return -(-ticket_cents * MEMBER_PERK_POINTS_RATE_PERCENT[perk] // 10_000)


def auto_perks_mode():
    """``off`` (hand-made offers only), ``test`` (listed events, staff) or ``on``."""
    value = str(getattr(settings, "ROO_EVENT_PERKS_AUTO", "off") or "off").strip().lower()
    return value if value in ("off", "test", "on") else "off"


def test_event_ids():
    """Event ids that may be private while the automatic perks are tested."""
    raw = getattr(settings, "ROO_EVENT_PERKS_TEST_EVENT_IDS", ()) or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    return frozenset(str(value).strip() for value in raw if str(value).strip())


def auto_perks_allowed(event_id, user):
    """Whether an automatic offer may be created or quoted for this member."""
    mode = auto_perks_mode()
    if mode == "on":
        return True
    return bool(mode == "test" and event_id in test_event_ids() and getattr(user, "is_staff", False))


def is_auto_offer(offer):
    return str(getattr(offer, "reward_id", "") or "").startswith(AUTO_OFFER_REWARD_PREFIX)


def price_based(offer):
    """Automatic offers, and every offer once the switch is on, use ticket pricing."""
    return is_auto_offer(offer) or auto_perks_mode() == "on"
