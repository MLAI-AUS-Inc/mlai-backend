"""Pure policy checks: python -m unittest; no Django or database is loaded."""

import unittest
from itertools import pairwise

from community_chat.roo_avatar.policy import (
    CATALOG,
    CATALOG_VERSION,
    SLOTS,
    RooAvatarError,
    catalog_for,
    effective_outfit,
    empty_outfit,
    require_unlocked,
    validate_update,
)


class RooAvatarPolicyTests(unittest.TestCase):
    def test_every_category_has_five_stable_unique_choices(self):
        self.assertEqual(set(CATALOG), set(SLOTS))
        for items in CATALOG.values():
            self.assertEqual(len(items), 5)
            self.assertEqual(len({item.id for item in items}), 5)

    def test_unlock_and_price_curve(self):
        items = sorted(
            (item for group in CATALOG.values() for item in group),
            key=lambda item: item.required_level,
        )
        levels = [item.required_level for item in items]
        prices = [item.price for item in items]
        self.assertEqual((levels[0], levels[-1]), (1, 36))
        self.assertTrue(all(1 <= b - a <= 2 for a, b in pairwise(levels)))
        self.assertEqual(items[-5:], list(CATALOG["jacket"]))
        self.assertEqual((prices[0], prices[-1]), (1, 40))
        self.assertEqual(prices, sorted(prices))

    def test_every_item_uses_its_profile_level_for_catalogue_and_equip(self):
        for slot, items in CATALOG.items():
            for index, item in enumerate(items):
                outfit = {**empty_outfit(), slot: item.id}
                for level in [None, item.required_level - 1, item.required_level, 200]:
                    with self.subTest(item=item.id, level=level):
                        unlocked = level is not None and level >= item.required_level
                        dto = catalog_for(level)[slot][index]
                        self.assertEqual(dto["unlocked"], unlocked)
                        self.assertEqual(
                            dto["unavailable_reason"],
                            None
                            if unlocked
                            else "history_unreconciled"
                            if level is None
                            else "level_required",
                        )
                        if unlocked:
                            require_unlocked(outfit, level)
                            self.assertEqual(effective_outfit(outfit, level), outfit)
                        else:
                            with self.assertRaises(RooAvatarError) as caught:
                                require_unlocked(outfit, level)
                            self.assertEqual(caught.exception.slots, (slot,))
                            self.assertEqual(
                                effective_outfit(outfit, level), empty_outfit()
                            )

    def test_classic_coat_empty_slots_and_owned_items_are_always_wearable(self):
        self.assertEqual(CATALOG_VERSION, "roo-avatar-v1")
        self.assertEqual(SLOTS, ("fur", "hair", "glasses", "jacket", "shoes"))
        for level in [None, 0, 1, 36, 200]:
            require_unlocked(empty_outfit(), level)
            owned = {"lilac_puffer"}
            outfit = {**empty_outfit(), "jacket": "lilac_puffer"}
            require_unlocked(outfit, level, owned)
            self.assertEqual(effective_outfit(outfit, level, owned), outfit)
            dto = catalog_for(level, owned)["jacket"][-1]
            self.assertTrue(dto["unlocked"] and dto["owned"])
            self.assertIsNone(dto["unavailable_reason"])

    def test_valid_update_keeps_explicit_none_and_returns_an_independent_dict(self):
        outfit = {**empty_outfit(), "hair": "honey_tuft"}
        version, parsed = validate_update({"version": 0, "outfit": outfit})
        self.assertEqual((version, parsed), (0, outfit))
        self.assertIsNot(parsed, outfit)
        for fur in [None, "snow_gum"]:
            outfit = {**empty_outfit(), "fur": fur}
            self.assertEqual(
                validate_update({"version": 3, "outfit": outfit}), (3, outfit)
            )

    def test_fur_is_optional_so_earlier_clients_can_still_save(self):
        earlier = {
            "hair": "honey_tuft",
            "glasses": None,
            "jacket": None,
            "shoes": "red_sneakers",
        }
        version, parsed = validate_update({"version": 2, "outfit": earlier})
        self.assertEqual((version, parsed), (2, earlier))
        self.assertNotIn("fur", parsed)
        for slot in ("hair", "glasses", "jacket", "shoes"):
            outfit = empty_outfit()
            del outfit[slot]
            with (
                self.subTest(missing=slot),
                self.assertRaises(RooAvatarError) as caught,
            ):
                validate_update({"version": 0, "outfit": outfit})
            self.assertEqual(caught.exception.slots, ())
        with self.assertRaises(RooAvatarError):
            validate_update({"version": 0, "outfit": {**earlier, "hat": None}})

    def test_unknown_fur_names_only_the_fur_slot(self):
        for value in ["outback_red", "honey_tuft", "Golden sand", "", 1, True, [], {}]:
            with self.subTest(value=value), self.assertRaises(RooAvatarError) as caught:
                validate_update(
                    {"version": 0, "outfit": {**empty_outfit(), "fur": value}}
                )
            self.assertEqual(caught.exception.code, "invalid_roo_avatar")
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(caught.exception.slots, ("fur",))

    def test_forged_ids_types_categories_and_incomplete_outfits_fail(self):
        for value in [
            "mint-mohawk",
            "classic_black",
            " honey_tuft",
            "",
            1,
            True,
            [],
            {},
        ]:
            with self.subTest(value=value), self.assertRaises(RooAvatarError):
                validate_update(
                    {"version": 0, "outfit": {**empty_outfit(), "hair": value}}
                )
        for outfit in [{}, {"hair": None}, {**empty_outfit(), "hat": None}]:
            with self.subTest(outfit=outfit), self.assertRaises(RooAvatarError):
                validate_update({"version": 0, "outfit": outfit})

    def test_versions_and_unknown_authority_fields_are_strict(self):
        for version in [None, True, False, -1, 1.0, "1", 2_147_483_647]:
            with self.subTest(version=version), self.assertRaises(RooAvatarError):
                validate_update({"version": version, "outfit": empty_outfit()})
        for field in [
            "account_id",
            "community_id",
            "current_level",
            "unlocked",
            "catalog_version",
        ]:
            with self.subTest(field=field), self.assertRaises(RooAvatarError):
                validate_update(
                    {"version": 0, "outfit": empty_outfit(), field: "forged"}
                )

    def test_independent_categories_remain_mixable(self):
        outfit = {
            "fur": "blue_flier",
            "hair": "lilac_bob",
            "glasses": "classic_black",
            "jacket": "berry_varsity",
            "shoes": "cream_high_tops",
        }
        require_unlocked(outfit, 32)
        self.assertEqual(effective_outfit(outfit, 32), outfit)
        self.assertIsNone(effective_outfit(outfit, 31)["jacket"])
        self.assertEqual(effective_outfit(outfit, 31)["hair"], "lilac_bob")


if __name__ == "__main__":
    unittest.main()
