from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import User
from roo.models import Ledger, PointsAccount


@override_settings(
    ROO_API_KEY='test-roo-api-key-that-is-long-enough',
    KIMI_ROO_POINTS_PER_PROMPT=1,
)
class KimiPromptUsageViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('kimi-prompt-usage')
        self.user = User.objects.create_user(email='developer@mlai.au')
        self.account = PointsAccount.objects.create(
            user=self.user,
            balance=3,
            earned_balance=3,
            lifetime_earned=3,
        )
        self.client.credentials(
            HTTP_X_API_KEY='test-roo-api-key-that-is-long-enough'
        )

    def payload(self, key='request-1234567890abcdef'):
        return {
            'email': 'DEVELOPER@MLAI.AU',
            'idempotency_key': key,
            'session_id': 'session_123',
        }

    def test_charges_one_point_atomically(self):
        response = self.client.post(self.url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['charged'])
        self.assertEqual(response.data['charged_points'], 1)
        self.assertEqual(response.data['balance'], 2)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 2)
        self.assertEqual(self.account.lifetime_spent, 1)
        ledger = Ledger.objects.get(idempotency_key='kimi_prompt:request-1234567890abcdef')
        self.assertEqual(ledger.delta, -1)
        self.assertEqual(ledger.source, 'TOOLS')
        self.assertEqual(ledger.reference_type, 'KIMI_PROMPT')

    def test_reports_balance_without_charging(self):
        response = self.client.get(self.url, {'email': 'developer@mlai.au'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['balance'], 3)
        self.assertEqual(response.data['prompt_cost_points'], 1)
        self.assertFalse(Ledger.objects.exists())

    def test_replay_does_not_charge_twice(self):
        first = self.client.post(self.url, self.payload(), format='json')
        second = self.client.post(self.url, self.payload(), format='json')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(second.data['charged'])
        self.assertEqual(second.data['charged_points'], 0)
        self.assertEqual(second.data['balance'], 2)
        self.assertEqual(
            Ledger.objects.filter(idempotency_key__startswith='kimi_prompt:').count(),
            1,
        )

    def test_insufficient_balance_returns_payment_required_without_ledger(self):
        self.account.balance = 0
        self.account.earned_balance = 0
        self.account.save()

        response = self.client.post(self.url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(response.data['code'], 'insufficient_points')
        self.assertEqual(response.data['required_points'], 1)
        self.assertEqual(response.data['balance'], 0)
        self.assertFalse(Ledger.objects.exists())

    def test_unknown_email_is_rejected(self):
        payload = self.payload()
        payload['email'] = 'missing@mlai.au'

        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['code'], 'account_not_found')

    def test_requires_strict_roo_api_key(self):
        self.client.credentials()

        response = self.client.post(self.url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_idempotency_key_collision_with_another_user(self):
        other = User.objects.create_user(email='other@mlai.au')
        Ledger.objects.create(
            user=other,
            delta=-1,
            kind='SPEND',
            source='TOOLS',
            reference_type='KIMI_PROMPT',
            idempotency_key='kimi_prompt:request-1234567890abcdef',
        )

        response = self.client.post(self.url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'idempotency_conflict')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 3)
