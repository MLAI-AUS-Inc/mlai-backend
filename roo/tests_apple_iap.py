"""Synthetic ledger, ownership and delivery race tests; never use real receipts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.tests.test_account_profiles import credentials_for
from roo.apple_purchases import deliver_purchase, process_notification
from roo.apple_verification import InvalidApplePurchase, VerifiedApplePurchase, VerifiedAppleNotification
from roo.models import AppleIapTransaction, AppleIapNotification, Ledger, PointsAccount
from roo.services import PointsService, InsufficientBalanceError, IdempotencyConflictError


def purchase_for(user, **changes):
    now = timezone.now()
    return replace(VerifiedApplePurchase(
        environment="Production", transaction_id="123456789", original_transaction_id="123456789",
        account_token=user.community_chat_profile_id, product_id="au.mlai.chat.roo.digital.10",
        quantity=1, microroo=10_000_000, price_milliunits=19990, currency="AUD",
        purchased_at=now, signed_at=now, revoked_at=None, payload_digest="a" * 64,
    ), **changes)


class AppleLedgerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="iap@example.com")
        self.purchase = purchase_for(self.user)

    def deliver(self, **changes):
        return deliver_purchase(user=self.user, purchase=replace(self.purchase, **changes))

    def account(self):
        return PointsAccount.objects.get(user=self.user)

    def spend(self, amount, key="spend"):
        return PointsService.spend_microroo(
            user=self.user, delta_microroo=amount, source="CONTENT_FACTORY",
            description="Synthetic digital service", created_by_slack_id="test",
            idempotency_key=key,
        )[0]

    def refund(self, amount=10, key="refund"):
        return PointsService.refund(
            user=self.user, delta=amount, source="CONTENT_FACTORY", description="Synthetic failure",
            created_by_slack_id="test", idempotency_key=key, original_spend_key="spend",
        )

    def test_duplicate_delivery_cannot_credit_physical_or_lifetime_earned(self):
        first = self.deliver()
        self.assertEqual(self.deliver().pk, first.pk)
        account = self.account()
        self.assertEqual(account.digital_balance_microroo, 10_000_000)
        self.assertEqual(account.balance_microroo, 0)
        self.assertEqual(account.lifetime_earned_microroo, 0)
        self.assertEqual(Ledger.objects.count(), 1)
        with self.assertRaises(InsufficientBalanceError):
            PointsService.spend(user=self.user, delta=1, source="COWORKING", description="Physical",
                                created_by_slack_id="test", idempotency_key="physical")

    def test_wrong_account_and_environment_make_no_records(self):
        for changes in ({"account_token": uuid4()}, {"environment": "Sandbox"}):
            with self.subTest(changes=changes), self.assertRaises(InvalidApplePurchase):
                self.deliver(**changes)
        self.assertFalse(AppleIapTransaction.objects.exists())
        self.assertFalse(PointsAccount.objects.exists())

    def test_transaction_identity_conflict_cannot_regrant(self):
        self.deliver()
        with self.assertRaises(InvalidApplePurchase):
            self.deliver(product_id="au.mlai.chat.roo.digital.20", microroo=20_000_000)
        self.assertEqual(self.account().digital_balance_microroo, 10_000_000)

    def test_sandbox_is_account_scoped_and_lifetime_bounded(self):
        with override_settings(APPLE_IAP_SANDBOX_ACCOUNT_TOKENS=[str(self.user.community_chat_profile_id)]):
            with self.assertRaises(InvalidApplePurchase):
                self.deliver()
            for index in range(10):
                self.deliver(environment="Sandbox", transaction_id=str(index))
            with self.assertRaisesMessage(InvalidApplePurchase, "sandbox_purchase_limit"):
                self.deliver(environment="Sandbox", transaction_id="11")
        self.assertEqual(self.account().digital_balance_microroo, 100_000_000)
        self.assertEqual(AppleIapTransaction.objects.count(), 10)

    def test_refund_before_delivery_and_later_reversal(self):
        later = self.purchase.signed_at + timedelta(seconds=2)
        row = self.deliver(revoked_at=later, signed_at=later)
        self.assertEqual(row.status, "refunded")
        self.assertEqual(self.account().digital_balance_microroo, 0)
        self.deliver()
        self.deliver(signed_at=later + timedelta(seconds=10))
        self.assertEqual(self.account().digital_balance_microroo, 0)
        deliver_purchase(user=self.user, purchase=self.purchase, event_kind="REFUND_REVERSED",
                         event_signed_at=later + timedelta(seconds=20))
        self.assertEqual(self.account().digital_balance_microroo, 10_000_000)

    def test_refund_of_consumed_credit_blocks_digital_spending_not_earned_points(self):
        self.deliver()
        self.spend(8_000_000)
        when = self.purchase.signed_at + timedelta(seconds=10)
        self.deliver(revoked_at=when, signed_at=when)
        self.assertEqual(self.account().digital_balance_microroo, 0)
        self.assertEqual(self.account().digital_refund_debt_microroo, 8_000_000)
        with self.assertRaises(InsufficientBalanceError):
            self.spend(1, "blocked")
        deliver_purchase(user=self.user, purchase=self.purchase, event_kind="REFUND_REVERSED",
                         event_signed_at=when + timedelta(seconds=1))
        self.assertEqual(self.account().digital_refund_debt_microroo, 0)
        self.assertEqual(self.account().digital_balance_microroo, 2_000_000)
        # Stale refund and duplicate reversal cannot alter that balance.
        self.deliver(revoked_at=when, signed_at=when)
        deliver_purchase(user=self.user, purchase=self.purchase, event_kind="REFUND_REVERSED",
                         event_signed_at=when + timedelta(seconds=1))
        self.assertEqual(self.account().digital_balance_microroo, 2_000_000)

    def test_service_failure_restores_original_buckets_and_rejects_second_refund_key(self):
        self.deliver()
        account = self.account()
        account.balance_microroo = 7_000_000
        account.purchased_topup_balance_microroo = 4_000_000
        account.earned_balance_microroo = 3_000_000
        account.microroo_initialized = True
        account.save()
        debit = self.spend(15_000_000)
        self.assertEqual(debit.digital_delta_microroo, -10_000_000)
        self.assertEqual(debit.purchased_delta_microroo, -4_000_000)
        self.assertEqual(self.account().balance_microroo, 2_000_000)
        refund, created = self.refund(15)
        self.assertTrue(created)
        self.assertEqual(refund.refund_of_id, debit.pk)
        self.assertFalse(self.refund(15)[1])
        with self.assertRaises(IdempotencyConflictError):
            self.refund(15, key="other-key")
        account = self.account()
        self.assertEqual(account.digital_balance_microroo, 10_000_000)
        self.assertEqual(account.purchased_topup_balance_microroo, 4_000_000)
        self.assertEqual(account.earned_balance_microroo, 3_000_000)

    def test_service_refund_after_apple_refund_cancels_debt_without_recreating_credit(self):
        self.deliver()
        self.spend(10_000_000)
        when = self.purchase.signed_at + timedelta(seconds=1)
        self.deliver(revoked_at=when, signed_at=when)
        self.refund()
        self.assertEqual(self.account().digital_balance_microroo, 0)
        self.assertEqual(self.account().balance_microroo, 0)
        self.assertEqual(self.account().digital_refund_debt_microroo, 0)

    def test_mismatched_or_missing_service_charge_cannot_create_credit(self):
        self.deliver()
        with self.assertRaises(ValueError):
            self.refund()
        self.spend(1_000_000)
        with self.assertRaises(ValueError):
            self.refund(2)
        self.assertEqual(self.account().digital_balance_microroo, 9_000_000)

    def test_notifications_replay_safely_and_do_not_recreate_deleted_owner(self):
        notification = VerifiedAppleNotification(
            uuid4(), "Production", "ONE_TIME_CHARGE", "", self.purchase.signed_at,
            self.purchase, "b" * 64,
        )
        process_notification(notification)
        process_notification(notification)
        self.assertEqual(AppleIapNotification.objects.count(), 1)
        self.assertEqual(Ledger.objects.count(), 1)
        self.user.delete()
        process_notification(replace(notification, notification_id=uuid4(), notification_type="REFUND"))
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertIsNone(AppleIapTransaction.objects.get().user_id)


@override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=False)
class AppleApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="api-iap@example.com")
        self.credentials = credentials_for(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}")
        self.url = "/api/v1/community-chat/apple-iap/transactions/"

    def test_unsigned_or_extra_fields_never_credit(self):
        for body in ({"signed_transaction": "fake.jwt.token"}, {"signed_transaction": "fake", "amount": 50}):
            response = self.client.post(self.url, body, format="json")
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Ledger.objects.exists())

    def test_verified_transaction_ack_matches_purchase_and_delivery_survives_sales_disabled(self):
        purchase = purchase_for(self.user)
        with patch("community_chat.apple_iap_views.verify_purchase", return_value=purchase):
            response = self.client.post(self.url, {"signed_transaction": "synthetic"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["acknowledged"])
        self.assertEqual(response.data["transaction_id"], purchase.transaction_id)
        self.assertEqual(response.data["digital_balance_microroo"], 10_000_000)

    def test_revoked_session_during_verification_cannot_credit(self):
        def verify(*args, **kwargs):
            session = self.credentials.session
            session.revoked_at = timezone.now()
            session.save(update_fields=("revoked_at",))
            return purchase_for(self.user)
        with patch("community_chat.apple_iap_views.verify_purchase", side_effect=verify):
            response = self.client.post(self.url, {"signed_transaction": "synthetic"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Ledger.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Requires real row locks")
class AppleConcurrencyTests(TransactionTestCase):
    def test_refund_racing_delivery_converges_without_credit(self):
        user = get_user_model().objects.create_user(email="refund-race@example.com")
        purchase = purchase_for(user)
        later = purchase.signed_at + timedelta(seconds=1)
        barrier = Barrier(2)
        def deliver(refund):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                receipt = replace(purchase, revoked_at=later, signed_at=later) if refund else purchase
                deliver_purchase(user=user, purchase=receipt)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(deliver, (False, True)))
        account = PointsAccount.objects.get(user=user)
        self.assertEqual(account.digital_balance_microroo, 0)
        self.assertEqual(account.digital_refund_debt_microroo, 0)
        self.assertEqual(AppleIapTransaction.objects.get().status, "refunded")

    def test_cross_account_spend_key_collision_rolls_back_losing_balance(self):
        users = [get_user_model().objects.create_user(email=f"collision-{index}@example.com") for index in range(2)]
        for index, user in enumerate(users):
            deliver_purchase(user=user, purchase=purchase_for(user, transaction_id=str(index)))
        barrier = Barrier(2)
        def spend(user):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    PointsService.spend_microroo(
                        user=user, delta_microroo=1_000_000, source="TOOLS", description="Synthetic collision",
                        created_by_slack_id="test", idempotency_key="same-global-key",
                    )
                    return "spent"
                except IdempotencyConflictError:
                    return "conflict"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(spend, users)), ["spent", "conflict"])
        self.assertEqual(sum(PointsAccount.objects.values_list("digital_balance_microroo", flat=True)), 19_000_000)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)

    def test_concurrent_delivery_and_notifications_credit_once(self):
        user = get_user_model().objects.create_user(email="race-iap@example.com")
        purchase = purchase_for(user)
        barrier = Barrier(4)
        def deliver(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if index % 2:
                    process_notification(VerifiedAppleNotification(
                        uuid4(), "Production", "ONE_TIME_CHARGE", "", purchase.signed_at, purchase, "c" * 64,
                    ))
                else:
                    deliver_purchase(user=user, purchase=purchase)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(deliver, range(4)))
        self.assertEqual(PointsAccount.objects.get(user=user).digital_balance_microroo, 10_000_000)
        self.assertEqual(Ledger.objects.count(), 1)
