"""Database-free checks for the explicit catalogue update command."""

from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError

from roo.management.commands.set_community_event_ticket_price import Command


class EventTicketPriceTests(TestCase):
    def run_command(self, reward, apply=False):
        output = StringIO()
        with patch(
            "roo.management.commands.set_community_event_ticket_price.transaction.atomic"
        ), patch(
            "roo.management.commands.set_community_event_ticket_price.RewardsCatalog.objects"
        ) as manager:
            manager.select_for_update.return_value.filter.return_value.first.return_value = (
                reward
            )
            Command(stdout=output).handle(apply=apply)
            manager.select_for_update.return_value.filter.assert_called_once_with(
                code="EVENT_TICKET"
            )
        return output.getvalue()

    def test_preview_does_not_write(self):
        reward = MagicMock(cost_points=6)
        self.assertIn("6 -> 15", self.run_command(reward))
        self.assertEqual(reward.cost_points, 6)
        reward.save.assert_not_called()

    def test_apply_updates_only_price(self):
        reward = MagicMock(cost_points=6)
        self.assertIn("6 -> 15", self.run_command(reward, apply=True))
        self.assertEqual(reward.cost_points, 15)
        reward.save.assert_called_once_with(update_fields=["cost_points"])

    def test_repeat_is_idempotent(self):
        reward = MagicMock(cost_points=15)
        self.assertIn("already costs 15", self.run_command(reward, apply=True))
        reward.save.assert_not_called()

    def test_missing_reward_fails_closed(self):
        with self.assertRaises(CommandError):
            self.run_command(None, apply=True)
