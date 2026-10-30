"""Database-free checks for the explicit catalogue update command."""

from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from django.core.management.base import CommandError

from roo.management.commands.set_community_event_ticket_price import Command
from roo.management.commands.configure_event_ticket_offer import (
    Command as ConfigureOfferCommand,
)
from integrations.services.luma_event_discounts import LumaEventDiscountError
from roo.services import RewardsService
from roo.event_discounts import EventDiscountError, _available_offer, _description
from django.utils import timezone
from datetime import timedelta


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
        self.assertIn("6 -> 32", self.run_command(reward))
        self.assertEqual(reward.cost_points, 6)
        reward.save.assert_not_called()

    def test_apply_updates_only_price(self):
        reward = MagicMock(cost_points=6)
        self.assertIn("6 -> 32", self.run_command(reward, apply=True))
        self.assertEqual(reward.cost_points, 32)
        reward.save.assert_called_once_with(update_fields=["cost_points"])

    def test_repeat_is_idempotent(self):
        reward = MagicMock(cost_points=32)
        self.assertIn("already costs 32", self.run_command(reward, apply=True))
        reward.save.assert_not_called()

    def test_missing_reward_fails_closed(self):
        with self.assertRaises(CommandError):
            self.run_command(None, apply=True)


class ConfigureEventTicketOfferTests(TestCase):
    module = "roo.management.commands.configure_event_ticket_offer"

    def setUp(self):
        self.gateway_patch = patch(self.module + ".LumaEventDiscountService")
        self.gateway = self.gateway_patch.start().return_value
        self.addCleanup(self.gateway_patch.stop)
        self.gateway.get_event.return_value = {"start_at": "2030-11-01T10:00:00Z"}
        self.gateway.get_ticket_types.return_value = [
            {"id": "ttype-synthetic", "valid_end_at": "2030-11-01T09:00:00Z"}
        ]
        self.snapshot = {
            "event_id": "evt-synthetic",
            "event_ticket_type_id": "ttype-synthetic",
            "name": "Synthetic event",
            "ticket": {"cents": 9900},
            "valid_end_at": "2030-11-01T09:00:00Z",
        }
        self.gateway.validate_offer.return_value = self.snapshot

    def run_command(self, apply=False):
        output = StringIO()
        ConfigureOfferCommand(stdout=output).handle(
            event_id="evt-synthetic", ticket_type_id="ttype-synthetic", apply=apply
        )
        return output.getvalue()

    def test_preview_validates_full_ticket_and_never_writes(self):
        with patch(self.module + ".RewardsCatalog.objects") as rewards, patch(
            self.module + ".EventDiscountOffer.objects"
        ) as offers:
            result = self.run_command()
            rewards.update_or_create.assert_not_called()
            offers.update_or_create.assert_not_called()
        self.assertIn('"points_cost": 32', result)
        self.assertIn('"applied": false', result)
        terms = self.gateway.validate_offer.call_args.args[0]
        self.assertEqual(
            terms["discount"], {"discount_type": "percent", "percent_off": 100}
        )
        self.assertEqual(terms["valid_end_at"], "2030-11-01T09:00:00+00:00")

    def test_provider_rejection_cannot_save_an_offer(self):
        self.gateway.validate_offer.side_effect = LumaEventDiscountError(
            "ticket_price_limit", "Below A$100 only."
        )
        with patch(self.module + ".transaction.atomic") as atomic:
            with self.assertRaisesRegex(CommandError, "ticket_price_limit"):
                self.run_command(apply=True)
            atomic.assert_not_called()

    def test_apply_sets_exact_points_and_full_discount(self):
        with patch(self.module + ".transaction.atomic"), patch(
            self.module + ".RewardsCatalog.objects"
        ) as rewards, patch(
            self.module + ".EventDiscountOffer.objects"
        ) as offers, patch(
            self.module + ".EventDiscountRedemption.objects"
        ) as receipts:
            receipts.filter.return_value.exists.return_value = False
            offers.filter.return_value.first.return_value = None
            offers.select_for_update.return_value.filter.return_value.first.return_value = (
                None
            )
            rewards.update_or_create.return_value = (
                SimpleNamespace(code="synthetic"),
                True,
            )
            self.assertIn('"applied": true', self.run_command(apply=True))
        self.assertEqual(
            rewards.update_or_create.call_args.kwargs["defaults"]["cost_points"], 32
        )
        defaults = offers.update_or_create.call_args.kwargs["defaults"]
        self.assertEqual(defaults["percent_off"], 100)
        self.assertIsNone(defaults["cents_off"])
        self.assertEqual(defaults["event_ticket_type_id"], "ttype-synthetic")

    def test_processing_receipt_prevents_offer_changes(self):
        with patch(self.module + ".transaction.atomic"), patch(
            self.module + ".RewardsCatalog.objects"
        ) as rewards, patch(
            self.module + ".EventDiscountRedemption.objects"
        ) as receipts:
            receipts.filter.return_value.exists.return_value = True
            with patch(self.module + ".EventDiscountOffer.objects") as offers:
                offers.filter.return_value.first.return_value = None
                offers.select_for_update.return_value.filter.return_value.first.return_value = (
                    None
                )
                with self.assertRaisesRegex(CommandError, "processing redemptions"):
                    self.run_command(apply=True)
            rewards.update_or_create.assert_not_called()

    def test_missing_or_ambiguous_ticket_cannot_be_configured(self):
        for tickets in ([], [{"id": "ttype-synthetic"}, {"id": "ttype-synthetic"}]):
            with self.subTest(tickets=tickets):
                self.gateway.get_ticket_types.return_value = tickets
                with self.assertRaisesRegex(CommandError, "exact ticket"):
                    self.run_command()

    def test_date_only_deadline_is_rejected(self):
        self.gateway.get_event.return_value = {"start_at": "2030-11-01"}
        with self.assertRaisesRegex(CommandError, "deadline"):
            self.run_command()

    def test_generic_ticket_request_cannot_bypass_event_eligibility(self):
        user = SimpleNamespace(pk=1)
        with patch("roo.services.User.objects") as users, patch(
            "roo.services.RewardsCatalog.objects"
        ) as rewards, patch("roo.services.PointsService.spend") as spend:
            users.select_for_update.return_value.get.return_value = user
            rewards.select_for_update.return_value.get.return_value = SimpleNamespace(
                code="EVENT_TICKET"
            )
            with self.assertRaisesRegex(ValueError, "confirmed event quote"):
                RewardsService.request_redemption.__wrapped__(
                    user=user, reward_code="EVENT_TICKET"
                )
            spend.assert_not_called()

    def test_community_ticket_cannot_be_repriced_or_changed_to_partial_discount(self):
        now = timezone.now()
        reward = SimpleNamespace(
            code="EVENT_TICKET_SYNTHETIC",
            cost_points=32,
            is_active=True,
            stock_remaining=None,
        )
        offer = SimpleNamespace(
            reward=reward,
            discount_type="percent",
            percent_off=100,
            is_active=True,
            valid_start_at=None,
            valid_end_at=now + timedelta(days=1),
        )
        _available_offer(offer, now)
        for price, percent in ((31, 100), (33, 100), (32, 99)):
            with self.subTest(price=price, percent=percent):
                reward.cost_points, offer.percent_off = price, percent
                with self.assertRaisesRegex(EventDiscountError, "32 Roo Points"):
                    _available_offer(offer, now)

    def test_quote_discloses_approval_and_charge_timing_before_confirmation(self):
        description = _description(
            {
                "discount": {"discount_type": "percent", "percent_off": 100},
                "ticket": {"name": "Standard", "requires_approval": True},
            }
        )
        self.assertIn("100% off one Standard ticket", description)
        self.assertIn("Organiser approval is required", description)
        self.assertIn(
            "Points are spent when the coupon is issued, before approval", description
        )
