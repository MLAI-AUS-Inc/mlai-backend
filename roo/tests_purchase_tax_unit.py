"""Database-free regression tests for actual checkout/webhook service paths."""
import copy
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from roo.purchase_tax import inclusive_gst_snapshot, validate_gst_rate, validate_gst_payment
from roo.services import PointsPurchaseService
from roo.views import PointsPurchaseViewSet, StripeWebhookView

RATE_ID = 'txr_testGst'
RATE = {'id': RATE_ID, 'active': True, 'inclusive': True, 'country': 'AU',
        'percentage': 10, 'tax_type': 'gst', 'display_name': 'GST'}


def purchase(amount=1999):
    identifier = uuid4()
    return SimpleNamespace(
        id=identifier, pk=identifier, user_id=1, user=SimpleNamespace(id=1),
        slack_user_id='UTEST', pack_id='topup_5', points_amount=10,
        amount_cents=amount, currency='aud', status='pending',
        expires_at=timezone.now() + timedelta(hours=1),
        created_at=timezone.now(), paid_at=None, ledger_entry_id=None,
        stripe_checkout_session_id=None, stripe_checkout_session_url=None,
        purchase_from={},
        metadata={'original': 'preserve', 'tax_quote': inclusive_gst_snapshot(amount, 'aud')},
        save=Mock(),
    )


class PurchaseTaxTests(unittest.TestCase):
    def test_pack_rounding_and_gross_total(self):
        for gross, gst in [(1999, 182), (3699, 336), (6399, 582)]:
            with self.subTest(gross=gross):
                tax = inclusive_gst_snapshot(gross, 'aud')
                self.assertEqual(tax['amount_cents'], gst)
                self.assertEqual(tax['subtotal_cents'] + gst, gross)

    def test_rejects_invalid_amount_and_currency(self):
        for amount, currency in [(0, 'aud'), (-1, 'aud'), (19.99, 'aud'), (True, 'aud'), (1999, 'usd')]:
            with self.subTest(amount=amount, currency=currency), self.assertRaises(ValueError):
                inclusive_gst_snapshot(amount, currency)

    def test_accepts_only_the_configured_inclusive_au_rate(self):
        validate_gst_rate(RATE, RATE_ID)
        for changes in [{'active': False}, {'inclusive': False}, {'country': 'US'},
                        {'percentage': 0}, {'percentage': 'invalid'}, {'id': 'txr_other'},
                        {'tax_type': 'vat'}, {'display_name': 'Sales tax'}]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                validate_gst_rate({**RATE, **changes}, RATE_ID)


@override_settings(STRIPE_SECRET_KEY='rk_test_fixture', ROO_POINTS_GST_TAX_RATE_ID=RATE_ID)
class CheckoutTaxTests(SimpleTestCase):
    def setUp(self):
        self.purchase = purchase()
        self.manager = Mock()
        self.manager.select_for_update.return_value.get.return_value = self.purchase
        self.rate_get = Mock(return_value=Mock(status_code=200, json=Mock(return_value=RATE)))
        self.session_post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={
            'id': 'cs_testGST', 'url': 'https://checkout.stripe.com/c/pay/cs_testGST'})))

    def checkout(self, **options):
        with patch('roo.services.PointsPurchase.objects', self.manager), \
             patch('roo.services.requests.get', self.rate_get), \
             patch('roo.services.requests.post', self.session_post), \
             patch.object(PointsPurchaseService, 'validate_purchase_limits'):
            # The runner forbids DB connections; exercise the transactional
            # function body with the manager mocked, retaining its row-lock query.
            return PointsPurchaseService.create_checkout_session.__wrapped__(
                self.purchase, 'terms-test', 'privacy-test', **options)

    def test_inclusive_checkout_and_persisted_snapshot(self):
        result = self.checkout()
        data = self.session_post.call_args.kwargs['data']
        self.assertEqual(data['line_items[0][price_data][unit_amount]'], '1999')
        self.assertEqual(data['line_items[0][tax_rates][0]'], RATE_ID)
        self.assertEqual(data['line_items[0][price_data][tax_behavior]'], 'inclusive')
        self.assertNotIn('automatic_tax[enabled]', data)
        self.assertNotIn('payment_method_types', data)
        self.assertEqual(data['payment_intent_data[metadata][gst_amount_cents]'], '182')
        self.assertEqual(self.purchase.metadata['tax']['amount_cents'], 182)
        self.assertEqual(self.purchase.metadata['original'], 'preserve')
        self.assertEqual(result['checkout_session_id'], 'cs_testGST')
        self.manager.select_for_update.assert_called_once()
        self.assertIn('metadata', self.purchase.save.call_args.kwargs['update_fields'])

    def test_slack_checkout_keeps_consent_and_tax_together(self):
        self.checkout(collect_terms_in_checkout=True)
        data = self.session_post.call_args.kwargs['data']
        self.assertEqual(data['consent_collection[terms_of_service]'], 'required')
        self.assertEqual(self.purchase.metadata['checkout_consent_mode'], 'stripe')
        self.assertEqual(self.purchase.metadata['tax']['stripe_tax_rate_id'], RATE_ID)

    def test_retry_reuses_existing_session_without_repricing_or_network(self):
        self.checkout()
        before = copy.deepcopy(self.purchase.metadata)
        self.rate_get.reset_mock()
        self.session_post.reset_mock()
        self.checkout()
        self.rate_get.assert_not_called()
        self.session_post.assert_not_called()
        self.assertEqual(self.purchase.metadata, before)

    @override_settings(ROO_POINTS_GST_TAX_RATE_ID='')
    def test_missing_gst_configuration_cannot_create_untaxed_checkout(self):
        with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
            self.checkout()
        self.rate_get.assert_not_called()
        self.session_post.assert_not_called()
        self.purchase.save.assert_not_called()

    def test_wrong_rate_cannot_add_gst_on_top_or_omit_it(self):
        self.rate_get.return_value.json.return_value = {**RATE, 'inclusive': False}
        with self.assertRaises(RuntimeError):
            self.checkout()
        self.session_post.assert_not_called()
        self.purchase.save.assert_not_called()

    def test_rate_lookup_failure_cannot_create_a_session(self):
        self.rate_get.side_effect = requests.Timeout()
        with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
            self.checkout()
        self.session_post.assert_not_called()

    def test_failed_session_creation_does_not_record_tax_as_collected(self):
        self.session_post.side_effect = requests.Timeout()
        with self.assertRaises(RuntimeError):
            self.checkout()
        self.assertNotIn('tax', self.purchase.metadata)
        self.purchase.save.assert_not_called()

    def test_public_purchase_response_contains_quote_without_private_data(self):
        response = PointsPurchaseViewSet._response_data(self.purchase)
        self.assertEqual(response['tax']['amount_cents'], 182)
        self.assertNotIn('slack_user_id', response)
        self.assertNotIn('user_id', response)
        self.purchase.metadata = {}
        self.assertIsNone(PointsPurchaseViewSet._response_data(self.purchase)['tax'])


class TaxWebhookTests(unittest.TestCase):
    def setUp(self):
        self.purchase = purchase()
        self.purchase.stripe_checkout_session_id = 'cs_testGST'
        self.purchase.metadata['tax'] = inclusive_gst_snapshot(1999, 'aud', RATE_ID)
        self.session = {
            'id': 'cs_testGST', 'status': 'complete', 'payment_status': 'paid',
            'amount_total': 1999, 'currency': 'aud', 'total_details': {'amount_tax': 182},
            'payment_intent': 'pi_testGST',
            'metadata': {'gst_policy_version': self.purchase.metadata['tax']['policy_version']},
        }
        self.view = StripeWebhookView()
        self.view._find_purchase_for_session = Mock(return_value=self.purchase)
        self.view._post_paid_slack_confirmation = Mock(return_value=False)
        self.credit = Mock(return_value=(SimpleNamespace(id=1), True))

    def complete(self):
        with patch('roo.views.transaction.atomic', return_value=nullcontext()), \
             patch('roo.views.PointsService.credit_purchased_topup', self.credit):
            return self.view._handle_checkout_completed(self.session)

    def test_payment_persists_stripe_gst_evidence_and_credits_exact_points(self):
        self.assertTrue(self.complete()['credited'])
        self.assertEqual(self.credit.call_args.kwargs['delta'], 10)
        self.assertEqual(self.purchase.metadata['tax_payment']['gst_cents'], 182)
        self.assertEqual(self.purchase.metadata['tax_payment']['total_cents'], 1999)
        self.assertEqual(self.purchase.metadata['original'], 'preserve')

    def test_mismatched_payment_cannot_credit_points(self):
        for changes in [{'amount_total': 2199}, {'currency': 'usd'},
                        {'total_details': {'amount_tax': 0}}, {'total_details': {}},
                        {'metadata': {}}, {'amount_total': '1999'}]:
            with self.subTest(changes=changes):
                candidate = {**self.session, **changes}
                with self.assertRaises(ValueError):
                    validate_gst_payment(candidate, self.purchase)
                original = self.session
                self.session = candidate
                with self.assertRaises(ValueError):
                    self.complete()
                self.session = original
                self.credit.assert_not_called()
                self.purchase.save.assert_not_called()

    def test_unpaid_completed_session_waits_for_async_success(self):
        self.session['payment_status'] = 'unpaid'
        self.assertTrue(self.complete()['ignored'])
        self.credit.assert_not_called()
        self.session['payment_status'] = 'paid'
        self.assertTrue(self.complete()['credited'])

    def test_duplicate_event_does_not_credit_again(self):
        self.complete()
        self.purchase.ledger_entry_id = 1
        self.credit.reset_mock()
        self.assertTrue(self.complete()['already_paid'])
        self.credit.assert_not_called()

    def test_legacy_inflight_session_is_not_given_invented_tax_evidence(self):
        self.purchase.metadata = {}
        self.session['metadata'] = {}
        self.session.pop('total_details')
        self.assertTrue(self.complete()['credited'])
        self.assertNotIn('tax_payment', self.purchase.metadata)

    def test_inconsistent_purchase_snapshot_cannot_credit(self):
        self.purchase.metadata['tax']['amount_cents'] = 0
        with self.assertRaises(ValueError):
            self.complete()
        self.credit.assert_not_called()

    def post_event(self, event_type):
        request = SimpleNamespace(body=json.dumps({
            'type': event_type, 'data': {'object': self.session},
        }).encode(), META={})
        with patch.object(self.view, '_verify_stripe_signature'), \
             patch('roo.views.transaction.atomic', return_value=nullcontext()), \
             patch('roo.views.PointsService.credit_purchased_topup', self.credit):
            return self.view.post(request)

    def test_async_success_event_routes_to_taxed_fulfillment(self):
        response = self.post_event('checkout.session.async_payment_succeeded')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['credited'])
        self.assertEqual(self.purchase.metadata['tax_payment']['gst_cents'], 182)

    def test_async_failure_marks_pending_purchase_failed_without_credit(self):
        self.session['payment_status'] = 'unpaid'
        response = self.post_event('checkout.session.async_payment_failed')
        self.assertTrue(response.data['failed'])
        self.assertEqual(self.purchase.status, 'failed')
        self.credit.assert_not_called()
        self.assertNotIn('tax_payment', self.purchase.metadata)

    def test_late_failure_cannot_reverse_a_paid_purchase(self):
        self.purchase.status = 'paid'
        response = self.post_event('checkout.session.async_payment_failed')
        self.assertTrue(response.data['ignored'])
        self.assertEqual(self.purchase.status, 'paid')
        self.credit.assert_not_called()

    def test_new_taxed_session_cannot_be_mistaken_for_legacy_when_snapshot_missing(self):
        self.purchase.metadata = {}
        with self.assertRaisesRegex(ValueError, 'snapshot is missing'):
            self.complete()
        self.credit.assert_not_called()
