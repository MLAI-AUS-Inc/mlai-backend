"""Pure codec regressions: no Django setup, credentials or database needed."""

import unittest
from integrations.services.slack_emoji import (
    _emoji_maps,
    emoji_to_slack_reaction,
    slack_reaction_to_emoji,
)


class SlackEmojiTests(unittest.TestCase):
    def test_full_catalog_round_trips_without_losing_skin_tones(self):
        names, _ = _emoji_maps()
        self.assertGreater(len(names), 3000)
        for name, native in names.items():
            with self.subTest(name=name):
                self.assertEqual(slack_reaction_to_emoji(name), native)
                self.assertEqual(
                    slack_reaction_to_emoji(emoji_to_slack_reaction(native)), native
                )

    def test_examples_and_legacy_spelling(self):
        self.assertEqual(slack_reaction_to_emoji("white_check_mark"), "✅")
        self.assertEqual(emoji_to_slack_reaction("👍"), "thumbsup")
        self.assertEqual(slack_reaction_to_emoji("thumbsup::skin-tone-4"), "👍🏽")
        self.assertEqual(
            slack_reaction_to_emoji(emoji_to_slack_reaction("👩🏻‍💻")), "👩🏻‍💻"
        )
        self.assertEqual(emoji_to_slack_reaction("❤️"), "heart")

    def test_custom_names_stay_bounded_and_malformed_values_fail_closed(self):
        self.assertEqual(slack_reaction_to_emoji("party_parrot"), ":party_parrot:")
        self.assertEqual(emoji_to_slack_reaction(":party_parrot:"), "party_parrot")
        self.assertEqual(
            emoji_to_slack_reaction(":thumbsup::skin-tone-4:"), "thumbsup::skin-tone-4"
        )
        self.assertEqual(slack_reaction_to_emoji("x" * 62), ":" + "x" * 62 + ":")
        for name in ["x" * 63, "<script>", "bad name", "wave::skin-tone-9"]:
            self.assertEqual(slack_reaction_to_emoji(name), "")
            self.assertEqual(emoji_to_slack_reaction(":" + name + ":"), "")
