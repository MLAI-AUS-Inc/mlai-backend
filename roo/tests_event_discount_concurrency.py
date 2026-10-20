"""PostgreSQL locking proofs for event holds and external coupon fulfilment."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import Mock
from uuid import uuid4

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.models import User
from integrations.services.luma_event_discounts import LumaEventDiscountService
from roo.event_discounts import EventDiscountError, EventDiscountService
from roo.models import EventDiscountOffer, EventDiscountQuote, EventDiscountRedemption, Ledger, PointsAccount, RewardsCatalog


SCOPE = {"team_id": "TTEST", "channel_id": "DTEST", "thread_ts": "1792454400.000100"}


@skipUnless(connection.vendor == "postgresql", "Real row-lock verification requires disposable PostgreSQL")
@override_settings(ROO_EVENT_DISCOUNTS_ENABLED=True, ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID="TTEST", LUMA_API_KEY="synthetic", ROO_API_KEY="synthetic")
class EventDiscountConcurrencyTests(TransactionTestCase):
    """Competing requests must serialize under the canonical member/reward locks."""

    def setUp(self):
        self.user = User.objects.create_user(email="concurrency-owner@example.invalid", slack_id="UCONCURRENT")
        self.other = User.objects.create_user(email="concurrency-other@example.invalid", slack_id="UOTHER")
        for user in (self.user, self.other):
            PointsAccount.objects.create(user=user, balance=15, earned_balance=15,
                                        balance_microroo=15_000_000, earned_balance_microroo=15_000_000,
                                        microroo_initialized=True)
        self.offer, self.quote = self.offer_and_quote("one", self.user)

    def offer_and_quote(self, suffix, user):
        reward = RewardsCatalog.objects.create(code="concurrent-" + suffix, name="Synthetic event " + suffix,
                                                cost_points=10, fulfillment="auto", stock_remaining=1)
        offer = EventDiscountOffer.objects.create(reward=reward, event_id="evt-" + suffix,
                                                   event_ticket_type_id="ttype-" + suffix,
                                                   discount_type="percent", percent_off=20, is_active=True,
                                                   valid_end_at=timezone.now() + timedelta(days=1))
        return offer, self.quote_for(offer, user)

    def quote_for(self, offer, user):
        from roo.event_discounts import _fingerprint
        snapshot = {
            "id": offer.event_id, "event_id": offer.event_id, "name": "Synthetic event",
            "url": "https://luma.com/synthetic-event", "starts_at": (timezone.now() + timedelta(days=2)).isoformat(),
            "event_ticket_type_id": offer.event_ticket_type_id, "points_cost": 10,
            "discount": {"discount_type": "percent", "percent_off": 20},
            "discount_description": "20% off General", "valid_end_at": offer.valid_end_at.isoformat(),
            "ticket": {"id": offer.event_ticket_type_id, "name": "General", "cents": 5000, "currency": "aud"},
        }
        return EventDiscountQuote.objects.create(user=user, offer=offer, offer_fingerprint=_fingerprint(offer),
            snapshot=snapshot, slack_team_id=SCOPE["team_id"], slack_channel_id=SCOPE["channel_id"],
            slack_thread_ts=SCOPE["thread_ts"], expires_at=timezone.now() + timedelta(minutes=10))

    def race(self, requests):
        barrier = Barrier(len(requests))

        def reserve(values):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return ("ok", str(EventDiscountService.reserve(scope=SCOPE, **values).pk))
            except EventDiscountError as exc:
                return (exc.code, None)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            return list(pool.map(reserve, requests))

    def test_simultaneous_duplicate_confirmation_reserves_once(self):
        request = {"user": self.user, "quote_id": self.quote.pk, "request_id": str(uuid4())}
        result = self.race([request, request])
        self.assertEqual([value[0] for value in result], ["ok", "ok"])
        self.assertEqual(result[0][1], result[1][1])
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.offer.reward.refresh_from_db()
        self.assertEqual(self.offer.reward.stock_remaining, 0)

    def test_two_events_cannot_reserve_the_same_general_points(self):
        _, second = self.offer_and_quote("two", self.user)
        result = self.race([
            {"user": self.user, "quote_id": quote.pk, "request_id": str(uuid4())}
            for quote in (self.quote, second)
        ])
        self.assertCountEqual([value[0] for value in result], ["ok", "insufficient_points"])
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)

    def test_last_reward_allocation_can_only_be_reserved_by_one_member(self):
        second = self.quote_for(self.offer, self.other)
        result = self.race([
            {"user": user, "quote_id": quote.pk, "request_id": str(uuid4())}
            for user, quote in ((self.user, self.quote), (self.other, second))
        ])
        self.assertCountEqual([value[0] for value in result], ["ok", "offer_unavailable"])
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)

    def test_competing_workers_create_one_coupon_and_one_ledger_spend(self):
        row = EventDiscountService.reserve(user=self.user, quote_id=self.quote.pk, request_id=str(uuid4()), scope=SCOPE)
        started, release = Event(), Event()
        gateway = Mock(spec=LumaEventDiscountService)
        gateway.validate_offer.return_value = self.quote.snapshot
        gateway.matches_coupon.return_value = True

        def lookup(*args):
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("Competing worker never completed its lease check")
            return None

        gateway.find_coupon.side_effect = lookup
        gateway.create_coupon.return_value = {"id": "coupon-synthetic", "remaining_count": 1}

        def process():
            close_old_connections()
            try:
                return EventDiscountService.process(row.pk, gateway=gateway).status
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(process)
            self.assertTrue(started.wait(timeout=10))
            second = pool.submit(process)
            try:
                self.assertEqual(second.result(timeout=10), "processing")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), "fulfilled")
        self.assertEqual(gateway.create_coupon.call_count, 1)
        self.assertEqual(Ledger.objects.filter(source="EVENT", kind="SPEND").count(), 1)
