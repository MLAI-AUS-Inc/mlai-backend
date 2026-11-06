"""Synthetic event-redemption integration tests; provider calls are mocked."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.core.cache import cache
from django.utils import timezone

from core.models import User
from integrations.services.luma_event_discounts import LumaEventDiscountError, LumaEventDiscountService
from roo.event_discounts import EventDiscountError, EventDiscountService, serialize_redemption
from roo.models import (
    EventDiscountOffer, EventDiscountQuote, EventDiscountRedemption,
    Ledger, PointsAccount, RewardsCatalog,
)
from roo.permissions import InsufficientBalanceError
from roo.services import PointsService, RewardsService


SCOPE = {"team_id": "TTEST", "channel_id": "DTEST", "thread_ts": "1792454400.000100"}


@override_settings(
    ROO_EVENT_DISCOUNTS_ENABLED=True, ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID="TTEST",
    LUMA_API_KEY="synthetic", ROO_API_KEY="synthetic-test-key",
)
class EventDiscountIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(email="event-owner@example.invalid", slack_id="UEVENTOWNER")
        self.other = User.objects.create_user(email="event-other@example.invalid", slack_id="UEVENTOTHER")
        self.reward = RewardsCatalog.objects.create(
            code="synthetic-event", name="Synthetic event discount", description="Synthetic fixture only",
            cost_points=10, fulfillment="auto", is_active=True, stock_remaining=3, max_per_user=1,
        )
        self.offer = EventDiscountOffer.objects.create(
            reward=self.reward, event_id="evt-synthetic", event_ticket_type_id="ttype-synthetic",
            discount_type="percent", percent_off=20, currency="aud", is_active=True,
            valid_end_at=timezone.now() + timedelta(days=14),
        )
        self.account = PointsAccount.objects.create(
            user=self.user, balance=25, earned_balance=20, purchased_topup_balance=5,
            balance_microroo=25_000_000, earned_balance_microroo=20_000_000,
            purchased_topup_balance_microroo=5_000_000, microroo_initialized=True,
        )
        PointsAccount.objects.create(
            user=self.other, balance=25, earned_balance=25, balance_microroo=25_000_000,
            earned_balance_microroo=25_000_000, microroo_initialized=True,
        )
        self.gateway = Mock(spec=LumaEventDiscountService)
        self.gateway.validate_offer.side_effect = self.snapshot
        self.coupons = {}
        self.gateway.find_coupon.side_effect = lambda event_id, code: self.coupons.get(code)
        self.gateway.matches_coupon.side_effect = LumaEventDiscountService.matches_coupon
        self.gateway.create_coupon.side_effect = self.create_coupon
        self.gateway_patch = patch("roo.event_discounts.LumaEventDiscountService", return_value=self.gateway)
        gateway_class = self.gateway_patch.start()
        gateway_class.coupon_url.side_effect = LumaEventDiscountService.coupon_url
        self.addCleanup(self.gateway_patch.stop)

    def snapshot(self, terms, **kwargs):
        expiry = datetime.fromisoformat(terms["valid_end_at"].replace("Z", "+00:00"))
        expiry = expiry.replace(microsecond=(expiry.microsecond // 1000) * 1000)
        return {
            **terms, "id": terms["event_id"], "name": "Synthetic event",
            "valid_end_at": expiry.isoformat(),
            "url": "https://luma.com/synthetic-event", "timezone": "Australia/Sydney",
            "starts_at": (timezone.now() + timedelta(days=15)).isoformat(),
            "ticket": {"id": terms["event_ticket_type_id"], "name": "General", "cents": 5000, "currency": "aud"},
        }

    def create_coupon(self, snapshot, code):
        coupon = {
            "id": "coupon-" + code, "code": code, "remaining_count": 1,
            "event_ticket_type_id": snapshot["event_ticket_type_id"],
            "percent_off": 20, "cents_off": None, "currency": None,
            "valid_start_at": None, "valid_end_at": snapshot["valid_end_at"],
        }
        self.coupons[code] = coupon
        return coupon

    def quote(self, user=None, scope=None):
        return EventDiscountService.quote(
            user=user or self.user, event_query="evt-synthetic", scope=scope or SCOPE,
        )

    def reserve(self, quote=None, *, user=None, request_id=None, scope=None):
        return EventDiscountService.reserve(
            user=user or self.user, quote_id=(quote or self.quote()).pk,
            request_id=request_id or str(uuid4()), scope=scope or SCOPE,
        )

    def process(self, row):
        return EventDiscountService.process(row.pk, gateway=self.gateway)

    def due_again(self, row):
        EventDiscountRedemption.objects.filter(pk=row.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1),
            lease_until=timezone.now() - timedelta(seconds=1),
        )

    def test_quote_does_not_spend_or_create_coupon(self):
        quote = self.quote()
        self.assertEqual(quote.user_id, self.user.pk)
        self.assertEqual(quote.offer_id, self.offer.pk)
        self.assertFalse(EventDiscountRedemption.objects.exists())
        self.assertFalse(Ledger.objects.exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 25_000_000)
        self.gateway.create_coupon.assert_not_called()

    def test_actual_event_title_matches_when_reward_label_is_different(self):
        with patch("roo.event_discounts.LumaAttendeeReportService") as calendar:
            calendar.return_value.list_upcoming_events.return_value = [
                {"id": self.offer.event_id, "name": "Melbourne Makers Meetup"},
            ]
            quote = EventDiscountService.quote(user=self.user, event_query="Makers Meetup", scope=SCOPE)
        self.assertEqual(quote.offer_id, self.offer.pk)
        calendar.return_value.list_upcoming_events.assert_called_once()
        self.assertFalse(Ledger.objects.exists())

    def test_ambiguous_canonical_names_return_configured_events_with_real_titles(self):
        reward = RewardsCatalog.objects.create(
            code="synthetic-second-event", name="Other internal discount label", cost_points=10,
            fulfillment="auto", is_active=True, stock_remaining=3, max_per_user=1,
        )
        other_offer = EventDiscountOffer.objects.create(
            reward=reward, event_id="evt-synthetic-two", event_ticket_type_id="ttype-synthetic-two",
            discount_type="percent", percent_off=20, currency="aud", is_active=True,
            valid_end_at=self.offer.valid_end_at,
        )
        titles = {self.offer.event_id: "Makers Meetup Melbourne", other_offer.event_id: "Makers Meetup Sydney"}
        with patch("roo.event_discounts.LumaAttendeeReportService") as calendar:
            calendar.return_value.list_upcoming_events.return_value = [
                {"id": event_id, "name": name} for event_id, name in titles.items()
            ]
            with self.assertRaises(EventDiscountError) as error:
                EventDiscountService.quote(user=self.user, event_query="Makers Meetup", scope=SCOPE)
        self.assertEqual(error.exception.code, "ambiguous_event")
        candidates = error.exception.extra["candidates"]
        self.assertEqual({row["event_id"]: row["event_name"] for row in candidates}, titles)
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.gateway.validate_offer.assert_not_called()

    def test_unconfigured_listed_event_cannot_make_eligible_match_ambiguous(self):
        with patch("roo.event_discounts.LumaAttendeeReportService") as calendar:
            calendar.return_value.list_upcoming_events.return_value = [
                {"id": self.offer.event_id, "name": "Makers Meetup Melbourne"},
                {"id": "evt-listed-not-configured", "name": "Makers Meetup Sydney"},
            ]
            quote = EventDiscountService.quote(user=self.user, event_query="Makers Meetup", scope=SCOPE)
        self.assertEqual(quote.offer_id, self.offer.pk)
        self.assertEqual(EventDiscountQuote.objects.count(), 1)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_reserve_creates_general_point_hold_and_opaque_code(self):
        row = self.reserve()
        self.assertEqual(row.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(row.reserved_microroo, 10_000_000)
        self.assertRegex(row.coupon_code, r"^ROO-[A-Z2-7]{16}$")
        self.assertEqual(len(row.coupon_code), 20)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 25_000_000)
        self.assertEqual(PointsService.get_available_microroo(self.user), 15_000_000)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 2)
        self.gateway.create_coupon.assert_not_called()

    def test_insufficient_general_balance_cannot_spend_digital_credits(self):
        self.account.balance_microroo = self.account.earned_balance_microroo = 5_000_000
        self.account.purchased_topup_balance_microroo = 0
        self.account.digital_balance_microroo = 100_000_000
        PointsService._sync_legacy_account(self.account)
        self.account.save()
        try:
            quote = self.quote()
            with self.assertRaises((EventDiscountError, InsufficientBalanceError)):
                self.reserve(quote)
        except (EventDiscountError, InsufficientBalanceError):
            pass
        self.assertFalse(EventDiscountRedemption.objects.exists())
        self.assertFalse(Ledger.objects.exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.digital_balance_microroo, 100_000_000)

    def test_confirmation_replay_returns_same_redemption_and_stock_once(self):
        quote = self.quote()
        request_id = str(uuid4())
        first = self.reserve(quote, request_id=request_id)
        second = self.reserve(quote, request_id=request_id)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 2)

    def test_different_request_id_for_same_quote_cannot_spend_twice(self):
        quote = self.quote()
        first = self.reserve(quote)
        try:
            second = self.reserve(quote)
            self.assertEqual(first.pk, second.pk)
        except EventDiscountError:
            pass
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.assertEqual(PointsService._reserved_microroo(self.user), 10_000_000)

    def test_foreign_actor_cannot_confirm_another_members_quote(self):
        quote = self.quote()
        with self.assertRaises(EventDiscountError):
            self.reserve(quote, user=self.other)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_quote_is_bound_to_same_private_conversation(self):
        quote = self.quote()
        for changes in ({"team_id": "TOTHER"}, {"channel_id": "DOTHER"}, {"thread_ts": "1792454400.000200"}):
            with self.subTest(changes=changes), self.assertRaises(EventDiscountError):
                self.reserve(quote, scope={**SCOPE, **changes})
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_expired_quote_cannot_create_hold(self):
        quote = self.quote()
        EventDiscountQuote.objects.filter(pk=quote.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(EventDiscountError):
            self.reserve(quote)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_changed_offer_or_points_price_requires_new_quote(self):
        quote = self.quote()
        self.reward.cost_points = 12
        self.reward.save()
        with self.assertRaises(EventDiscountError):
            self.reserve(quote)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_stock_last_allocation_cannot_be_reserved_by_second_member(self):
        self.reward.stock_remaining = 1
        self.reward.save()
        first_quote = self.quote()
        second_quote = self.quote(user=self.other)
        self.reserve(first_quote)
        with self.assertRaises(EventDiscountError):
            self.reserve(second_quote, user=self.other)
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 0)

    def test_success_settles_once_and_provides_applied_coupon_link(self):
        row = self.reserve()
        completed = self.process(row)
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.assertEqual(completed.reserved_microroo, 0)
        self.assertIsNotNone(completed.ledger_entry_id)
        self.assertEqual(self.process(completed).ledger_entry_id, completed.ledger_entry_id)
        self.assertEqual(Ledger.objects.filter(user=self.user, kind="SPEND").count(), 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 15_000_000)
        self.assertEqual(self.account.digital_balance_microroo, 0)
        self.assertEqual(completed.purchased_microroo, 5_000_000)
        self.assertEqual(completed.ledger_entry.purchased_delta_microroo, -5_000_000)
        self.gateway.create_coupon.assert_called_once()
        payload = serialize_redemption(completed)
        self.assertIn("coupon=" + row.coupon_code, str(payload))

    def test_definitive_create_rejection_releases_hold_and_stock_without_charge(self):
        row = self.reserve()
        self.gateway.create_coupon.side_effect = LumaEventDiscountError(
            "luma_rejected", "Synthetic invalid request", status_code=422,
        )
        failed = self.process(row)
        self.assertEqual(failed.status, EventDiscountRedemption.Status.FAILED)
        self.assertEqual(failed.reserved_microroo, 0)
        self.assertFalse(Ledger.objects.exists())
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 3)
        self.assertEqual(PointsService.get_available_microroo(self.user), 25_000_000)

    def test_timeout_after_remote_success_recovers_existing_coupon_without_recreation(self):
        row = self.reserve()

        def remote_success_then_timeout(snapshot, code):
            self.create_coupon(snapshot, code)
            raise LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)

        self.gateway.create_coupon.side_effect = remote_success_then_timeout
        pending = self.process(row)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(pending.reserved_microroo, 10_000_000)
        self.assertFalse(Ledger.objects.exists())
        self.due_again(pending)
        completed = self.process(pending)
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.gateway.create_coupon.assert_called_once()
        self.assertEqual(Ledger.objects.filter(user=self.user, kind="SPEND").count(), 1)

    def test_unknown_remote_result_never_releases_hold_or_creates_another_code(self):
        row = self.reserve()
        self.gateway.create_coupon.side_effect = LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)
        pending = self.process(row)
        original_code = pending.coupon_code
        self.due_again(pending)
        pending = self.process(pending)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(pending.reserved_microroo, 10_000_000)
        self.assertEqual(pending.coupon_code, original_code)
        self.assertFalse(Ledger.objects.exists())
        self.gateway.create_coupon.assert_called_once()

    def test_pending_retry_obeys_backoff_without_remote_calls(self):
        row = self.reserve()
        self.gateway.create_coupon.side_effect = LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)
        pending = self.process(row)
        calls = self.gateway.find_coupon.call_count
        again = self.process(pending)
        self.assertEqual(again.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(self.gateway.find_coupon.call_count, calls)
        self.gateway.create_coupon.assert_called_once()

    def test_later_auth_failure_does_not_disprove_previous_unknown_write(self):
        row = self.reserve()
        self.gateway.create_coupon.side_effect = LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)
        pending = self.process(row)
        self.due_again(pending)
        self.gateway.find_coupon.side_effect = LumaEventDiscountError("luma_rejected", "Synthetic auth refusal", status_code=403)
        pending = self.process(pending)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(pending.reserved_microroo, 10_000_000)
        self.assertFalse(Ledger.objects.exists())
        self.gateway.create_coupon.assert_called_once()

    def test_recovered_wrong_coupon_terms_require_review_and_preserve_hold(self):
        row = self.reserve()

        def wrong_coupon_then_timeout(snapshot, code):
            self.create_coupon(snapshot, code)
            self.coupons[code]["percent_off"] = 99
            raise LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)

        self.gateway.create_coupon.side_effect = wrong_coupon_then_timeout
        pending = self.process(row)
        self.due_again(pending)
        pending = self.process(pending)
        self.assertTrue(pending.needs_review)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(pending.reserved_microroo, 10_000_000)
        self.assertFalse(Ledger.objects.exists())
        self.gateway.create_coupon.assert_called_once()

    def test_active_worker_lease_prevents_second_creation_attempt(self):
        row = self.reserve()
        EventDiscountRedemption.objects.filter(pk=row.pk).update(
            lease_token=uuid4(), lease_until=timezone.now() + timedelta(minutes=5),
        )
        pending = self.process(row)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.gateway.find_coupon.assert_not_called()
        self.gateway.create_coupon.assert_not_called()

    def test_coupon_expiring_while_settlement_waits_never_charges_points(self):
        row = self.reserve()
        settle = EventDiscountService._settle
        after_expiry = self.offer.valid_end_at + timedelta(seconds=1)

        def lock_acquired_after_expiry(*args, **kwargs):
            with patch("roo.event_discounts.timezone.now", return_value=after_expiry):
                return settle(*args, **kwargs)

        with patch.object(EventDiscountService, "_settle", side_effect=lock_acquired_after_expiry):
            pending = self.process(row)
        self.assertEqual(pending.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(pending.reserved_microroo, 10_000_000)
        self.assertFalse(Ledger.objects.exists())

    def test_stale_worker_cannot_disable_coupon_after_another_worker_settles(self):
        row = self.reserve()
        claim = EventDiscountService._claim
        captured = {}

        def capture_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            captured["row"] = result[0]
            return result

        def another_worker_finishes(snapshot, code):
            coupon = self.create_coupon(snapshot, code)
            newer_token = uuid4()
            EventDiscountRedemption.objects.filter(pk=row.pk).update(lease_token=newer_token)
            completed = EventDiscountService._settle(row.pk, newer_token, coupon)
            self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
            # Simulate an old worker resuming with stale member state after
            # the new worker has committed and made the booking link visible.
            captured["row"].user.is_active = False
            return coupon

        self.gateway.create_coupon.side_effect = another_worker_finishes
        with patch.object(EventDiscountService, "_claim", side_effect=capture_claim):
            completed = self.process(row)
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.gateway.disable_coupon.assert_not_called()
        self.assertEqual(Ledger.objects.filter(user=self.user, kind="SPEND").count(), 1)

    def test_db_failure_after_coupon_creation_is_recovered_without_second_coupon(self):
        row = self.reserve()
        with patch.object(PointsService, "spend", side_effect=RuntimeError("Synthetic database interruption")):
            try:
                self.process(row)
            except RuntimeError:
                pass
        row.refresh_from_db()
        self.assertEqual(row.status, EventDiscountRedemption.Status.PROCESSING)
        self.assertEqual(row.reserved_microroo, 10_000_000)
        self.assertFalse(Ledger.objects.exists())
        self.assertEqual(len(self.coupons), 1)
        self.due_again(row)
        completed = self.process(row)
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.gateway.create_coupon.assert_called_once()

    def test_reserved_points_are_unavailable_to_other_general_spends(self):
        self.reserve()
        with self.assertRaises(InsufficientBalanceError):
            PointsService.spend(
                user=self.user, delta=16, source="COWORKING", description="Synthetic conflict",
                created_by_slack_id=self.user.slack_id, idempotency_key="synthetic-general",
            )

    def test_digital_service_uses_digital_credits_but_cannot_consume_event_hold(self):
        self.account.digital_balance_microroo = 2_000_000
        self.account.save()
        self.reserve()
        with self.assertRaises(InsufficientBalanceError):
            PointsService.spend_microroo(
                user=self.user, delta_microroo=18_000_000, source="CONTENT_FACTORY",
                description="Synthetic digital conflict", created_by_slack_id=self.user.slack_id,
                idempotency_key="synthetic-digital-too-large",
            )
        PointsService.spend_microroo(
            user=self.user, delta_microroo=17_000_000, source="CONTENT_FACTORY",
            description="Synthetic digital usage", created_by_slack_id=self.user.slack_id,
            idempotency_key="synthetic-digital-available",
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 10_000_000)
        self.assertEqual(self.account.digital_balance_microroo, 0)
        completed = self.process(EventDiscountRedemption.objects.get(user=self.user))
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 0)

    def test_coding_admission_reserves_only_remaining_capacity(self):
        from roo.coding import create_turn
        from roo.tests_coding import create_account_session, ensure_pricing

        self.account.digital_balance_microroo = 2_000_000
        self.account.save()
        self.reserve()
        ensure_pricing()
        session, _ = create_account_session(self.user)
        turn, _ = create_turn(
            user=self.user, account_session=session, idempotency_key=str(uuid4()),
            local_session_id=str(uuid4()), model="kimi-k3",
        )
        self.assertEqual(turn.reserved_microroo, 17_000_000)
        self.assertEqual(PointsService.get_available_microroo(self.user), 0)
        # The digital part of Coding's reservation cannot block settlement of
        # the general points that were separately held for this event.
        completed = self.process(EventDiscountRedemption.objects.get(user=self.user))
        self.assertEqual(completed.status, EventDiscountRedemption.Status.FULFILLED)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_microroo, 15_000_000)
        self.assertEqual(self.account.digital_balance_microroo, 2_000_000)

    def test_generic_reward_redemption_cannot_bypass_coupon_workflow(self):
        with self.assertRaises((EventDiscountError, ValueError)):
            RewardsService.request_redemption(user=self.user, reward_code=self.reward.code)
        self.assertFalse(Ledger.objects.exists())

    def test_disabled_feature_cannot_quote_or_reserve(self):
        quote = self.quote()
        with override_settings(ROO_EVENT_DISCOUNTS_ENABLED=False):
            with self.assertRaises(EventDiscountError):
                self.quote()
            with self.assertRaises(EventDiscountError):
                self.reserve(quote)
        self.assertFalse(EventDiscountRedemption.objects.exists())
