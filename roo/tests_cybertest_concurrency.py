"""Real row-lock checks, only in an approved disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import secrets
import uuid
from unittest import skipUnless
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from roo import cybertest_checkout as checkout
from roo.cybertest_policy import CheckoutError
from roo.models import CybertestCheckout, Ledger, PointsAccount
from roo.test_cybertest_delivery import basic, upgrade
from roo.test_cybertest_plan import plan
from roo.tests_cybertest_phases import PhaseCheckoutTests


@skipUnless(connection.vendor == 'postgresql', 'Requires actual PostgreSQL row locks')
@override_settings(CYBERTEST_BILLING_MODE='live', CYBERTEST_LIVE_BILLING_ENABLED=True,
                   CYBERTEST_PHASED_SCANS_ENABLED=True,
                   CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
class PhaseConcurrencyTests(TransactionTestCase):
    # These helpers create synthetic users and local ledger rows only.
    purchase = PhaseCheckoutTests.purchase
    start = PhaseCheckoutTests.start
    result = PhaseCheckoutTests.result
    delivered_basic = PhaseCheckoutTests.delivered_basic

    def setUp(self):
        PhaseCheckoutTests.setUp(self)
        PointsAccount.objects.create(user=self.user, microroo_initialized=True,
            digital_balance_microroo=0, balance_microroo=1_000_000_000,
            purchased_topup_balance_microroo=1_000_000_000, earned_balance_microroo=0)

    def race(self, operation):
        barrier = Barrier(2)

        def attempt():
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                barrier.wait(timeout=10)
                try:
                    return ('ok', operation())
                except CheckoutError as error:
                    return ('rejected', error.code)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt) for _ in range(2)]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_basic_quotes_create_one_pending_purchase(self):
        intent, snapshot = self.purchase(basic()), plan()
        results = self.race(lambda: checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent, execution_plan=snapshot))
        self.assertEqual([r[0] for r in results].count('ok'), 1)
        self.assertIn(('rejected', 'BASIC_PURCHASE_PENDING'), results)
        self.assertEqual(CybertestCheckout.objects.count(), 1)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_concurrent_upgrade_quotes_create_one_entitlement(self):
        parent, _, _ = self.delivered_basic()
        intent = self.purchase({**upgrade('standard'), 'parentCheckoutId': str(parent)})
        results = self.race(lambda: checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent))
        self.assertEqual([r[0] for r in results].count('ok'), 1)
        self.assertIn(('rejected', 'UPGRADE_EXISTS'), results)
        self.assertEqual(CybertestCheckout.objects.filter(parent_checkout_id=parent).count(), 1)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 976)

    def test_concurrent_charge_and_dispatch_do_not_spend_or_start_twice(self):
        quote, audit, token = uuid.uuid4(), uuid.uuid4(), secrets.token_urlsafe(32)
        intent = self.purchase(basic())
        checkout.create_quote(quote, token, intent, execution_plan=plan())
        checkout.confirm(quote, token, self.user, expected_points=24)
        results = self.race(lambda: checkout.charge(quote, audit, intent))
        self.assertTrue(all(kind == 'ok' for kind, _ in results))
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 976)
        dispatches = self.race(lambda: checkout.start_dispatch(quote, audit))
        self.assertEqual(sum(bool(data['dispatch_allowed']) for kind, data in dispatches if kind == 'ok'), 1)

    def test_concurrent_failed_upgrade_settlements_refund_only_once(self):
        parent, _, _ = self.delivered_basic()
        contract = {**upgrade('large'), 'parentCheckoutId': str(parent)}
        quote, audit, _ = self.start(contract, 100)
        evidence = self.result(audit, contract, missing_required=True)
        results = self.race(lambda: checkout.settle_delivery(quote, audit, evidence, None))
        self.assertTrue(all(kind == 'ok' for kind, _ in results))
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 976)
        self.assertIsNone(CybertestCheckout.objects.get(pk=parent).refund_ledger_id)
