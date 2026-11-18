"""Pure policy checks: python -m unittest; no Django or database is loaded."""

import unittest

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
from community_chat.volunteer.policy import LEVELS, MICROROO, progress


class RooAvatarPolicyTests(unittest.TestCase):
    def test_every_category_has_five_stable_unique_choices(self):
        self.assertEqual(set(CATALOG), set(SLOTS))
        for items in CATALOG.values():
            self.assertEqual(len(items), 5)
            self.assertEqual(len({item.id for item in items}), 5)
            self.assertTrue(all(0 <= item.required_level <= 6 for item in items))

    def test_fur_is_the_first_slot_and_every_colour_is_a_starter(self):
        self.assertEqual(CATALOG_VERSION, "roo-avatar-v2")
        self.assertEqual(SLOTS, ("fur", "hair", "glasses", "jacket", "shoes"))
        self.assertEqual(tuple(empty_outfit()), SLOTS)
        self.assertEqual(
            [(item.id, item.name, item.required_level) for item in CATALOG["fur"]],
            [
                ("golden_sand", "Golden sand", 0),
                ("blue_flier", "Blue flier", 0),
                ("ironbark", "Ironbark", 0),
                ("snow_gum", "Snow gum", 0),
                ("midnight_wallaroo", "Midnight wallaroo", 0),
            ],
        )
        for level in [None, *range(7)]:
            projected = catalog_for(level)
            self.assertEqual(tuple(projected), SLOTS)
            self.assertEqual(len(projected["fur"]), 5)
            for item in projected["fur"]:
                self.assertTrue(item["unlocked"])
                self.assertIsNone(item["unavailable_reason"])
            for item in CATALOG["fur"]:
                outfit = {**empty_outfit(), "fur": item.id}
                require_unlocked(outfit, level)
                self.assertEqual(effective_outfit(outfit, level), outfit)

    def test_hair_keeps_stable_ids_and_levels_but_names_only_the_style(self):
        self.assertEqual(
            [(item.id, item.name, item.required_level) for item in CATALOG["hair"]],
            [
                ("honey_tuft", "Tuft", 0),
                ("cocoa_curls", "Curls", 1),
                ("peach_fringe", "Swept fringe", 2),
                ("lilac_bob", "Bob", 4),
                ("mint_mohawk", "Mohawk", 6),
            ],
        )

    def test_rank_thresholds_unlock_exactly_at_the_existing_contribution_level(self):
        for _, number, _, threshold, *_ in LEVELS:
            for slot, items in CATALOG.items():
                at = progress(threshold * MICROROO)["current_level"]["level"]
                projected = catalog_for(at)[slot]
                for item, dto in zip(items, projected):
                    self.assertEqual(dto["unlocked"], number >= item.required_level)
                if number:
                    below = progress(threshold * MICROROO - 1)["current_level"]["level"]
                    for dto in catalog_for(below)[slot]:
                        if dto["required_level"] == number:
                            self.assertFalse(dto["unlocked"])

    def test_unknown_history_offers_only_starters_without_inventing_a_rank(self):
        for items in catalog_for(None).values():
            for item in items:
                starter = item["required_level"] == 0
                self.assertEqual(item["unlocked"], starter)
                self.assertEqual(
                    item["unavailable_reason"],
                    None if starter else "history_unreconciled",
                )
        self.assertFalse(any(item["unlocked"] for item in catalog_for(None)["jacket"]))

    def test_none_is_allowed_at_every_level_and_unknown_history(self):
        for level in [None, *range(7)]:
            require_unlocked(empty_outfit(), level)
            self.assertEqual(effective_outfit(empty_outfit(), level), empty_outfit())

    def test_correction_suppresses_saved_items_without_destroying_preferences(self):
        saved = {slot: items[-1].id for slot, items in CATALOG.items()}
        original = saved.copy()
        self.assertEqual(effective_outfit(saved, 6), saved)
        self.assertEqual(
            effective_outfit(saved, 0), {**empty_outfit(), "fur": saved["fur"]}
        )
        self.assertEqual(saved, original)
        with self.assertRaises(RooAvatarError) as caught:
            require_unlocked(saved, 0)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.slots, ("hair", "glasses", "jacket", "shoes"))
        self.assertEqual(effective_outfit(saved, 6), original)

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
            with self.subTest(missing=slot), self.assertRaises(
                RooAvatarError
            ) as caught:
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
        require_unlocked(outfit, 4)
        self.assertEqual(effective_outfit(outfit, 4), outfit)
        self.assertEqual(effective_outfit(outfit, 3)["hair"], None)
        self.assertEqual(effective_outfit(outfit, 3)["jacket"], "berry_varsity")
        self.assertEqual(effective_outfit(outfit, None)["fur"], "blue_flier")


if __name__ == "__main__":
    unittest.main()
