import unittest

from roo.manual_time_rules import CATALOGUE, ManualTimeError, normalize, roo_text

SAMPLE = {
    "submission_key": "28dbfe4e-0af0-476e-9b54-9a4cec856602",
    "catalog_version": CATALOGUE["version"],
    "action_ids": ["event.pack", "event.door", "event.pack"],
    "volunteer_minutes": 45,
}


class ManualTimeRulesTests(unittest.TestCase):
    def test_actions_are_deduplicated_and_duration_counted_once(self):
        _, result = normalize(SAMPLE)
        self.assertEqual(result["action_ids"], ["event.door", "event.pack"])
        self.assertEqual(result["amount_microroo"], 4_500_000)

    def test_amount_display(self):
        for minutes, expected in [(1, "0.1"), (45, "4.5"), (135, "13.5"), (180, "18"), (240, "24")]:
            with self.subTest(minutes=minutes):
                self.assertEqual(roo_text(minutes), expected)

    def test_invalid_or_ambiguous_values(self):
        for invalid in (
            {"volunteer_minutes": 0},
            {"volunteer_minutes": True},
            {"volunteer_minutes": 1.5},
            {"volunteer_minutes": "45"},
            {"volunteer_minutes": 2**63},
            {"action_ids": []},
            {"action_ids": ["unknown"]},
            {"action_ids": ["other"], "note": " "},
            {"catalog_version": "old"},
            {"submission_key": ""},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ManualTimeError):
                    normalize({**SAMPLE, **invalid})
