"""Pure cosmetic catalogue and validation; never awards or spends Roo."""

from collections.abc import Mapping
from dataclasses import dataclass

# Shipped iOS builds reject any other value, so adding the fur slot keeps it.
CATALOG_VERSION = "roo-avatar-v1"
SLOTS = ("fur", "hair", "glasses", "jacket", "shoes")
# Clients released before the fur slot send exactly the other slots.
OPTIONAL_UPDATE_SLOTS = frozenset({"fur"})


@dataclass(frozen=True)
class Item:
    """Stable cosmetic identity, independent of its renderer array position."""

    id: str
    name: str
    required_level: int
    price: int


CATALOG = {
    # Null fur is the default "Outback red".
    "fur": (
        Item("golden_sand", "Golden sand", 1, 1),
        Item("blue_flier", "Blue flier", 5, 3),
        Item("ironbark", "Ironbark", 9, 6),
        Item("snow_gum", "Snow gum", 16, 12),
        Item("midnight_wallaroo", "Midnight wallaroo", 22, 18),
    ),
    # Hair items are styles; colour belongs to the fur slot.
    "hair": (
        Item("honey_tuft", "Tuft", 2, 1),
        Item("cocoa_curls", "Curls", 6, 3),
        Item("peach_fringe", "Swept fringe", 10, 7),
        Item("lilac_bob", "Bob", 18, 14),
        Item("mint_mohawk", "Mohawk", 24, 21),
    ),
    "glasses": (
        Item("classic_black", "Classic black", 3, 2),
        Item("honey_rounds", "Honey rounds", 7, 4),
        Item("rose_hearts", "Rose hearts", 12, 8),
        Item("ocean_visor", "Ocean visor", 19, 15),
        Item("lilac_stars", "Lilac stars", 25, 23),
    ),
    "jacket": (
        Item("sage_bomber", "Sage bomber", 28, 27),
        Item("blue_denim", "Blue denim", 30, 30),
        Item("berry_varsity", "Berry varsity", 32, 33),
        Item("sunflower_raincoat", "Sunflower raincoat", 34, 36),
        Item("lilac_puffer", "Lilac puffer", 36, 40),
    ),
    "shoes": (
        Item("red_sneakers", "Red sneakers", 4, 2),
        Item("cream_high_tops", "Cream high tops", 8, 5),
        Item("bush_boots", "Bush boots", 14, 10),
        Item("pink_roller_skates", "Pink roller skates", 20, 16),
        Item("teal_runners", "Teal runners", 26, 24),
    ),
}


class RooAvatarError(ValueError):
    """A safe, stable API failure with optional affected outfit slots."""

    def __init__(self, code, status=400, *, slots=()):
        self.code = code
        self.status = status
        self.slots = tuple(slots)
        super().__init__(code)


def empty_outfit():
    """Return a fresh unequipped outfit; None is always available."""
    return dict.fromkeys(SLOTS)


def choices(slot):
    """Return stable model choices for one category."""
    return tuple((item.id, item.name) for item in CATALOG[slot])


def validate_update(value):
    """Strictly validate a replacement and an exact optimistic version.

    Every slot is required except ``fur``: an outfit without it leaves the
    saved fur unchanged, and the returned outfit then has no ``fur`` key.
    """
    invalid = RooAvatarError("invalid_roo_avatar")
    if not isinstance(value, Mapping) or set(value) != {"version", "outfit"}:
        raise invalid
    version = value["version"]
    if type(version) is not int or not 0 <= version < 2_147_483_647:
        raise invalid
    outfit = value["outfit"]
    if not isinstance(outfit, Mapping) or set(outfit) | OPTIONAL_UPDATE_SLOTS != set(
        SLOTS
    ):
        raise invalid
    for slot in SLOTS:
        if slot not in outfit:
            continue
        selected = outfit[slot]
        if selected is not None and (
            not isinstance(selected, str)
            or selected not in {item.id for item in CATALOG[slot]}
        ):
            raise RooAvatarError("invalid_roo_avatar", slots=(slot,))
    return version, dict(outfit)


def item_unlocked(item, level, owned):
    """Ownership permits re-equipping, including after a catalog or rank change."""
    return item.id in owned or (type(level) is int and level >= item.required_level)


def catalog_for(level, owned=()):
    """Return all choices and current server-derived availability."""
    return {
        slot: [
            {
                "id": item.id,
                "name": item.name,
                "required_level": item.required_level,
                "price_points": item.price,
                "owned": item.id in owned,
                "unlocked": item_unlocked(item, level, owned),
                "unavailable_reason": (
                    None
                    if item_unlocked(item, level, owned)
                    else "history_unreconciled"
                    if level is None
                    else "level_required"
                ),
            }
            for item in items
        ]
        for slot, items in CATALOG.items()
    }


def effective_outfit(outfit, level, owned=()):
    """Suppress ineligible saved choices without deleting the preference."""
    return {
        slot: next(
            (
                item.id
                for item in CATALOG[slot]
                if item.id == outfit.get(slot) and item_unlocked(item, level, owned)
            ),
            None,
        )
        for slot in SLOTS
    }


def require_unlocked(outfit, level, owned=()):
    """Reject a forged or newly locked selection at the service boundary."""
    effective = effective_outfit(outfit, level, owned)
    locked = [slot for slot in SLOTS if outfit[slot] != effective[slot]]
    if locked:
        raise RooAvatarError("roo_avatar_item_locked", 403, slots=locked)
