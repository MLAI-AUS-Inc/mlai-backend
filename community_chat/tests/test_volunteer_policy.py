"""Run with python -m unittest; these tests never initialise Django or a DB."""

import unittest
from datetime import datetime, timezone

from community_chat.volunteer.policy import (
    catalogue,
    levels,
    microroo,
    next_actions,
    period_bounds,
    progress,
    roo,
)


class VolunteerPolicyTests(unittest.TestCase):
    def test_exact_amount_roundtrip(self):
        for amount in ("0", "4", "0.000001", "12.123456", "250"):
            self.assertEqual(roo(microroo(amount)), amount)
        for amount in (
            "-1",
            "0.0000001",
            "nan",
            "Infinity",
            True,
            0.1,
            "1e999999999",
            "1e-999999999",
            "1.00000000000000000000000000001",
        ):
            with self.assertRaises(ValueError):
                microroo(amount)

    def test_boundaries_and_level_local_progress(self):
        for level in levels():
            self.assertEqual(
                progress(microroo(level["threshold_roo"]))["current_level"]["level"],
                level["level"],
            )
            if level["level"]:
                self.assertEqual(
                    progress(microroo(level["threshold_roo"]) - 1)["current_level"][
                        "level"
                    ],
                    level["level"] - 1,
                )
        result = progress(microroo("7"))
        self.assertEqual(
            result["progress"],
            {"earned_roo": "3", "required_roo": "6", "fraction": 0.5},
        )
        self.assertEqual(result["points_to_next"], "3")
        self.assertIsNone(progress(microroo("250"))["next_level"])

    def test_bonus_is_separate_and_total_liability(self):
        result = progress(microroo("4"))
        self.assertEqual(result["current_level"]["level"], 1)
        self.assertEqual(sum(int(row["bonus_roo"]) for row in levels()[:6]), 29)
        self.assertEqual(sum(int(row["bonus_roo"]) for row in levels()), 49)

    def test_calendar_period_uses_melbourne_and_monday(self):
        when = datetime(2026, 9, 6, 14, 30, tzinfo=timezone.utc)
        start, end = period_bounds(when, "week")
        self.assertEqual(start.isoformat(), "2026-09-07T00:00:00+10:00")
        self.assertEqual((end - start).days, 7)
        start, end = period_bounds(
            datetime(2026, 12, 31, 15, tzinfo=timezone.utc), "month"
        )
        self.assertEqual(start.month, 1)
        self.assertEqual(end.month, 2)

    def test_catalogue_and_verified_checklist(self):
        actions = catalogue(20)
        self.assertEqual(len(actions), 17)
        self.assertEqual(actions["monthly_learning_update"]["reward_roo"], "20")
        self.assertEqual(actions["buy_merch"]["reward_roo"], "0")
        self.assertEqual(
            actions["monthly_learning_update"]["cap_group"],
            actions["monthly_startup_update"]["cap_group"],
        )
        candidates = [
            dict(item, eligible=True, completed=False) for item in actions.values()
        ]
        self.assertEqual(
            [item["key"] for item in next_actions(candidates)],
            ["introduce_yourself", "boost_startup", "attend_first_event"],
        )
        candidates[0]["completed"] = True
        self.assertNotIn(
            "introduce_yourself", [item["key"] for item in next_actions(candidates)]
        )
