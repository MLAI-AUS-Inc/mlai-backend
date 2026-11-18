"""Pure cosmetic catalogue and validation; never awards or spends Roo."""

from collections.abc import Mapping
from dataclasses import dataclass

CATALOG_VERSION = "roo-avatar-v2"
SLOTS = ("fur", "hair", "glasses", "jacket", "shoes")
# Clients released before the fur slot send exactly the other slots.
OPTIONAL_UPDATE_SLOTS = frozenset({"fur"})


@dataclass(frozen=True)
class Item:
    """Stable cosmetic identity, independent of its renderer array position."""

    id: str
    name: str
    required_level: int


CATALOG = {
    # Null fur is the default "Outback red"; every fur colour is a starter.
    "fur": (
        Item("golden_sand", "Golden sand", 0),
        Item("blue_flier", "Blue flier", 0),
        Item("ironbark", "Ironbark", 0),
        Item("snow_gum", "Snow gum", 0),
        Item("midnight_wallaroo", "Midnight wallaroo", 0),
    ),
    # Hair items are styles; colour belongs to the fur slot.
    "hair": (
        Item("honey_tuft", "Tuft", 0),
        Item("cocoa_curls", "Curls", 1),
        Item("peach_fringe", "Swept fringe", 2),
        Item("lilac_bob", "Bob", 4),
        Item("mint_mohawk", "Mohawk", 6),
    ),
    "glasses": (
        Item("classic_black", "Classic black", 0),
        Item("honey_rounds", "Honey rounds", 1),
        Item("rose_hearts", "Rose hearts", 3),
        Item("ocean_visor", "Ocean visor", 4),
        Item("lilac_stars", "Lilac stars", 6),
    ),
    "jacket": (
        Item("sage_bomber", "Sage bomber", 1),
        Item("blue_denim", "Blue denim", 2),
        Item("berry_varsity", "Berry varsity", 3),
        Item("sunflower_raincoat", "Sunflower raincoat", 5),
        Item("lilac_puffer", "Lilac puffer", 6),
    ),
    "shoes": (
        Item("red_sneakers", "Red sneakers", 0),
        Item("cream_high_tops", "Cream high tops", 2),
        Item("bush_boots", "Bush boots", 3),
        Item("pink_roller_skates", "Pink roller skates", 4),
        Item("teal_runners", "Teal runners", 5),
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


def item_unlocked(item, level):
    """Allow starter items without pretending unreconciled history is Level 0."""
    return item.required_level == 0 or (
        type(level) is int and level >= item.required_level
    )


def catalog_for(level):
    """Return all choices and current server-derived availability."""
    return {
        slot: [
            {
                "id": item.id,
                "name": item.name,
                "required_level": item.required_level,
                "unlocked": item_unlocked(item, level),
                "unavailable_reason": (
                    None
                    if item_unlocked(item, level)
                    else "history_unreconciled" if level is None else "level_required"
                ),
            }
            for item in items
        ]
        for slot, items in CATALOG.items()
    }


def effective_outfit(outfit, level):
    """Suppress ineligible saved choices without deleting the preference."""
    return {
        slot: next(
            (
                item.id
                for item in CATALOG[slot]
                if item.id == outfit.get(slot) and item_unlocked(item, level)
            ),
            None,
        )
        for slot in SLOTS
    }


def require_unlocked(outfit, level):
    """Reject a forged or newly locked selection at the service boundary."""
    effective = effective_outfit(outfit, level)
    locked = [slot for slot in SLOTS if outfit[slot] != effective[slot]]
    if locked:
        raise RooAvatarError("roo_avatar_item_locked", 403, slots=locked)
