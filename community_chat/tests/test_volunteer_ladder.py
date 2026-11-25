"""Run with python -m unittest; these tests never initialise Django or a DB."""

import unittest

from community_chat.volunteer.ladder import (
    MAX_LEVEL,
    RANKS,
    VERSION,
    catalogue,
    frame_for,
    level_for,
    milestone_key,
    plain_label,
    position,
    preview,
    rank_for,
    step_cost,
    threshold,
)
from community_chat.volunteer.policy import MICROROO

# "Entry Roo" for each rank, exactly as the owner's proposal lists them.
PROPOSAL_ENTRY = (
    0, 12, 25, 39, 54, 70, 105, 144, 187, 234,
    285, 369, 462, 564, 675, 795, 969, 1159, 1365, 1587,
    1825, 2145, 2490, 2860, 3255, 3675, 4120, 4590, 5085, 5605,
    6150, 6720, 7315, 7935, 8580, 9250, 9945, 10665, 11410, 12180,
    12975, 13795, 14640, 15510, 16405, 17512, 18655, 19834, 21049, 22300,
)


def points(n):
    return n * MICROROO


class LadderCurveTests(unittest.TestCase):
    def test_pinned_values_from_the_plan(self):
        self.assertEqual(level_for(points(11)), 0)
        self.assertEqual(level_for(points(12)), 1)
        self.assertEqual(level_for(points(505)), 22)
        self.assertEqual(level_for(points(22_299)), 199)
        self.assertEqual(level_for(points(22_300)), 200)
        self.assertEqual(threshold(200), 22_300)

    def test_each_step_costs_one_more_than_the_last(self):
        self.assertEqual(step_cost(0), 12)
        for level in range(MAX_LEVEL):
            self.assertEqual(threshold(level + 1) - threshold(level), 12 + level)

    def test_formula_matches_counting_up_for_every_point_total(self):
        level = 0
        for p in range(0, 25_000):
            while level < MAX_LEVEL and p >= threshold(level + 1):
                level += 1
            self.assertEqual(level_for(points(p)), level, p)

    def test_only_whole_points_count(self):
        self.assertEqual(level_for(points(12) - 1), 0)
        self.assertEqual(level_for(points(12) + 999_999), 1)

    def test_level_is_capped_at_200(self):
        self.assertEqual(level_for(points(10_000_000)), MAX_LEVEL)

    def test_rejects_bad_input(self):
        for bad in (-1, 1.5, "12", True, None):
            with self.assertRaises(ValueError):
                level_for(bad)
        for bad in (-1, 201, 1.0, True):
            with self.assertRaises(ValueError):
                threshold(bad)
        with self.assertRaises(ValueError):
            step_cost(MAX_LEVEL)


class LadderRankTests(unittest.TestCase):
    def test_fifty_ranks_cover_every_level_once(self):
        self.assertEqual(len(RANKS), 50)
        ranks = catalogue()["ranks"]
        covered = [
            level for rank in ranks for level in range(rank["first_level"], rank["last_level"] + 1)
        ]
        self.assertEqual(covered, list(range(MAX_LEVEL + 1)))

    def test_entry_points_match_the_proposal(self):
        self.assertEqual(
            tuple(int(rank["entry_roo"]) for rank in catalogue()["ranks"]), PROPOSAL_ENTRY
        )

    def test_rank_spacing_widens_as_described(self):
        widths = [rank["last_level"] - rank["first_level"] + 1 for rank in catalogue()["ranks"]]
        self.assertEqual(widths, [1] * 5 + [2] * 5 + [3] * 5 + [4] * 5 + [5] * 24 + [6] * 5 + [1])

    def test_every_rank_boundary(self):
        for index, (first, name, _) in enumerate(RANKS):
            self.assertEqual(rank_for(first)["name"], name)
            self.assertEqual(rank_for(first)["number"], index + 1)
            if first:
                self.assertNotEqual(rank_for(first - 1)["name"], name)

    def test_names_held_back_until_the_committee_decides(self):
        names = {row[1] for row in RANKS}
        self.assertIn("Hype Debugger", names)
        self.assertIn("Human in the Thick of It", names)
        self.assertIn("Department of Actually Shipping", names)
        self.assertFalse(any(name.startswith("VP ") for name in names))

    def test_frames(self):
        self.assertEqual(frame_for(4)["key"], "none")
        self.assertEqual(frame_for(5)["key"], "creek")
        self.assertEqual(frame_for(14)["key"], "creek")
        self.assertEqual(frame_for(15)["key"], "paddock")
        self.assertEqual(frame_for(30)["key"], "ridge")
        self.assertEqual(frame_for(50)["key"], "coast")
        self.assertEqual(frame_for(100)["key"], "reef")
        self.assertEqual(frame_for(150)["key"], "sky")
        self.assertEqual(frame_for(199)["key"], "sky")
        self.assertEqual(frame_for(200)["name"], "The lanyard")

    def test_new_milestone_keys_never_match_the_seven_level_keys(self):
        self.assertEqual(milestone_key(1), "ladder_level_1")
        seven = {f"level_{n}" for n in range(7)}
        self.assertFalse(seven & {milestone_key(n) for n in range(MAX_LEVEL + 1)})
        self.assertTrue(all(len(milestone_key(n)) <= 64 for n in range(MAX_LEVEL + 1)))

    def test_plain_label(self):
        self.assertEqual(plain_label(31), "Level 31 contributor")


class LadderPositionTests(unittest.TestCase):
    def test_worked_example(self):
        where = position(points(505))
        self.assertEqual(where["policy"], VERSION)
        self.assertEqual(where["level"], 22)
        self.assertEqual(where["rank"]["name"], "Full-Stack Feral")
        self.assertEqual(where["points_to_next_level"], "24")
        self.assertEqual(where["next_rank"]["name"], "Rubber Duck Consultant")
        self.assertEqual(where["points_to_next_rank"], "59")
        self.assertEqual(where["progress"]["earned_roo"], "10")
        self.assertEqual(where["progress"]["required_roo"], "34")

    def test_fractions_stay_exact(self):
        where = position(points(12) - 1)
        self.assertEqual(where["level"], 0)
        self.assertEqual(where["points_to_next_level"], "0.000001")

    def test_unknown_history_is_never_level_zero(self):
        self.assertIsNone(position(None))

    def test_top_of_the_ladder(self):
        where = position(points(30_000))
        self.assertEqual(where["level"], 200)
        self.assertIsNone(where["next_level"])
        self.assertIsNone(where["next_rank"])
        self.assertEqual(where["points_to_next_level"], "0")
        self.assertEqual(where["points_to_next_rank"], "0")
        self.assertEqual(where["progress"]["fraction"], 1)

    def test_preview_counts_today_and_ladder_levels(self):
        report = preview(
            [(1, points(4)), (2, points(505)), (3, None), (4, points(250))],
            lambda total: 6 if total >= points(250) else 1 if total >= points(4) else 0,
        )
        self.assertEqual(report["members"], 4)
        self.assertEqual(report["unknown"], 1)
        self.assertEqual(report["today_levels"], {"1": 1, "6": 2})
        self.assertEqual(report["ladder_levels"], {"0": 1, "13": 1, "22": 1})
        self.assertEqual(report["frames"], {"none": 1, "creek": 1, "paddock": 1})
        self.assertEqual(report["rows"][2]["ladder"], None)


if __name__ == "__main__":
    unittest.main()
