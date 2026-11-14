"""The delivery manifest is accepted only from the authenticated scan service."""
from unittest.mock import patch
from uuid import UUID
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient
from roo.cybertest_policy import CheckoutError


@override_settings(CYBERTEST_BILLING_API_KEY='synthetic-phase-service-key-not-secret')
class PhaseApiTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.receipt = UUID('00000000-0000-4000-8000-000000000001')
        self.audit = '00000000-0000-4000-8000-000000000002'
        self.url = f'/api/v1/points/cybertest/quotes/{self.receipt}/settle-delivery/'
        self.body = {'audit_id': self.audit, 'evidence': {'report': {'persisted': False}}, 'costs': None}

    def test_browser_cannot_assert_delivery_or_trigger_refund(self):
        with patch('roo.cybertest_checkout.settle_delivery') as settle:
            response = self.client.post(self.url, self.body, format='json')
        self.assertEqual(response.status_code, 403)
        settle.assert_not_called()

    def test_recovery_is_service_only_and_returns_no_plan_to_unauthenticated_browser(self):
        url = '/api/v1/points/cybertest/quotes/recover/'
        with patch('roo.cybertest_checkout.recover_quote', create=True, return_value=None) as recover:
            self.assertEqual(self.client.post(url, {'binding': {}, 'token': 't'*43}, format='json').status_code, 403)
            recover.assert_not_called()
            response = self.client.post(url, {'binding': {}, 'token': 't'*43}, format='json', HTTP_X_CYBERTEST_KEY='synthetic-phase-service-key-not-secret')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'receipt': None})
        recover.assert_called_once_with({}, 't'*43)

    def test_service_passes_manifest_to_transaction_and_disables_caching(self):
        with patch('roo.cybertest_checkout.settle_delivery', return_value={'status': 'refunded'}) as settle:
            response = self.client.post(self.url, self.body, format='json', HTTP_X_CYBERTEST_KEY='synthetic-phase-service-key-not-secret')
        self.assertEqual(response.status_code, 200)
        settle.assert_called_once_with(self.receipt, self.audit, self.body['evidence'], None)
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_service_can_recover_an_upgrade_for_one_specific_parent(self):
        with patch('roo.cybertest_checkout.recover_quote', return_value=None) as recover:
            response = self.client.post('/api/v1/points/cybertest/quotes/recover/',
                {'binding': {}, 'token': 't'*43, 'parent_checkout_id': str(self.receipt)},
                format='json', HTTP_X_CYBERTEST_KEY='synthetic-phase-service-key-not-secret')
        self.assertEqual(response.status_code, 200)
        recover.assert_called_once_with({}, 't'*43, parent_checkout_id=str(self.receipt))

    def test_extra_fields_and_invalid_contract_produce_specific_errors(self):
        with patch('roo.cybertest_checkout.settle_delivery') as settle:
            response = self.client.post(self.url, {**self.body, 'points': 0}, format='json', HTTP_X_CYBERTEST_KEY='synthetic-phase-service-key-not-secret')
        self.assertEqual(response.status_code, 400)
        settle.assert_not_called()
        with patch('roo.cybertest_checkout.settle_delivery', side_effect=CheckoutError('INVALID_DELIVERY', 'Wrong audit.')):
            response = self.client.post(self.url, self.body, format='json', HTTP_X_CYBERTEST_KEY='synthetic-phase-service-key-not-secret')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'INVALID_DELIVERY')
