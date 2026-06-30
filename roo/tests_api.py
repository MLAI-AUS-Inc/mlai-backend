import hashlib
import hmac
import json
import time
from datetime import date, timedelta
import threading

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from .models import (
    ChannelFirstPost,
    CoworkingBooking,
    Ledger,
    PointsAccount,
    PointsAdmin,
    PointsPurchase,
    PointsRequest,
    RewardRedemption,
    RewardsCatalog,
    Task,
    TaskAssignment,
    TaskSubmission,
)
from django.contrib.auth import get_user_model
from unittest.mock import Mock, patch
from .services import PointsService

User = get_user_model()


class RewardsCatalogAPITests(APITestCase):
    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_rewards_catalog_uses_current_pricing_and_hides_coffee(self, mock_permission):
        response = self.client.get(reverse('rewards-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rewards_by_code = {reward['code']: reward for reward in response.data}

        self.assertNotIn('COFFEE', rewards_by_code)
        self.assertEqual(rewards_by_code['EVENT_TICKET']['cost_points'], 6)
        self.assertEqual(rewards_by_code['WORKSHOP_50']['cost_points'], 24)
        self.assertEqual(rewards_by_code['WORKSHOP_FREE']['cost_points'], 42)
        self.assertEqual(rewards_by_code['WORKSHOP_FREE']['stock_remaining'], 5)


class PointsPurchaseViewSetTests(APITestCase):
    def setUp(self):
        self.url = reverse('points-purchase-list')
        self.slack_user_id = 'UTOPUPAPI123'
        self.user = User.objects.create_user(
            email='topup-api@example.com',
            slack_id=self.slack_user_id,
        )
        self.user.date_joined = timezone.now() - timedelta(days=30)
        self.user.save(update_fields=['date_joined'])

    @override_settings(DEFAULT_FRONTEND_URL='https://mlai.test')
    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_create_pending_purchase_success(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': f'  {self.slack_user_id} ',
                'pack_id': 'topup_10',
                'purchase_from': {
                    'source': 'slack',
                    'slack_channel_id': 'C123',
                    'slack_thread_ts': '1712345678.000100',
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        purchase = PointsPurchase.objects.get()
        self.assertEqual(response.data['id'], str(purchase.id))
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['pack_id'], 'topup_10')
        self.assertEqual(response.data['points_amount'], 20)
        self.assertEqual(response.data['amount_cents'], 3699)
        self.assertEqual(response.data['currency'], 'aud')
        self.assertEqual(
            response.data['frontend_checkout_page_url'],
            f'https://mlai.test/roo/topup/{purchase.id}',
        )
        self.assertEqual(purchase.user, self.user)
        self.assertEqual(purchase.slack_user_id, self.slack_user_id)
        self.assertEqual(purchase.purchase_from['source'], 'slack')
        self.assertEqual(purchase.purchase_from['slack_user_id'], self.slack_user_id)
        self.assertEqual(purchase.purchase_from['slack_channel_id'], 'C123')
        self.assertIsNone(purchase.ledger_entry)
        self.assertIsNone(purchase.stripe_checkout_session_id)
        self.assertFalse(PointsAccount.objects.filter(user=self.user).exists())

    @override_settings(DEFAULT_FRONTEND_URL='https://mlai.test')
    def test_retrieve_pending_purchase_is_public(self):
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_5',
            points_amount=10,
            amount_cents=1999,
        )

        response = self.client.get(reverse('points-purchase-detail', kwargs={'pk': purchase.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(purchase.id))
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['pack_id'], 'topup_5')
        self.assertEqual(response.data['points_amount'], 10)
        self.assertEqual(response.data['amount_cents'], 1999)
        self.assertEqual(response.data['currency'], 'aud')
        self.assertEqual(
            response.data['frontend_checkout_page_url'],
            f'https://mlai.test/roo/topup/{purchase.id}',
        )
        self.assertNotIn('user', response.data)
        self.assertNotIn('slack_user_id', response.data)

    def test_retrieve_purchase_not_found(self):
        response = self.client.get('/api/v1/points/purchases/00000000-0000-0000-0000-000000000000/')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('Points purchase not found', response.data['error'])

    def test_retrieve_purchase_malformed_id_returns_not_found(self):
        response = self.client.get('/api/v1/points/purchases/not-a-uuid/')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('Points purchase not found', response.data['error'])

    @override_settings(DEFAULT_FRONTEND_URL='https://mlai.test', STRIPE_SECRET_KEY='sk_test_roo')
    @patch('roo.services.requests.post')
    def test_checkout_creates_stripe_session_for_pending_purchase(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={
                'id': 'cs_test_roo_points',
                'url': 'https://checkout.stripe.com/c/pay/cs_test_roo_points',
            }),
        )
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_10',
            points_amount=10,
            amount_cents=3699,
        )

        response = self.client.post(
            reverse('points-purchase-checkout', kwargs={'pk': purchase.id}),
            {
                'terms_version_accepted': 'roo-points-terms-2026-05-04',
                'privacy_version_accepted': 'privacy-2026-05-04',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['stripe_checkout_session_id'], 'cs_test_roo_points')
        self.assertEqual(response.data['checkout_session_url'], 'https://checkout.stripe.com/c/pay/cs_test_roo_points')

        purchase.refresh_from_db()
        self.assertEqual(purchase.stripe_checkout_session_id, 'cs_test_roo_points')
        self.assertEqual(purchase.terms_version_accepted, 'roo-points-terms-2026-05-04')
        self.assertIsNotNone(purchase.terms_accepted_at)
        self.assertEqual(purchase.privacy_version_accepted, 'privacy-2026-05-04')
        self.assertIsNotNone(purchase.privacy_accepted_at)
        self.assertIsNone(purchase.ledger_entry)
        self.assertFalse(Ledger.objects.exists())

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs['auth'], ('sk_test_roo', ''))
        stripe_data = kwargs['data']
        self.assertEqual(stripe_data['mode'], 'payment')
        self.assertEqual(stripe_data['client_reference_id'], str(purchase.id))
        self.assertEqual(stripe_data['success_url'], f'https://mlai.test/roo/topup/{purchase.id}?checkout=success')
        self.assertEqual(stripe_data['cancel_url'], f'https://mlai.test/roo/topup/{purchase.id}?checkout=cancelled')
        self.assertEqual(stripe_data['line_items[0][price_data][currency]'], 'aud')
        self.assertEqual(stripe_data['line_items[0][price_data][unit_amount]'], '3699')
        self.assertEqual(stripe_data['line_items[0][price_data][product_data][name]'], '20 Top-up Roo Points')
        self.assertEqual(stripe_data['metadata[points_purchase_id]'], str(purchase.id))
        self.assertEqual(stripe_data['metadata[mlai_user_id]'], str(self.user.id))
        self.assertEqual(stripe_data['metadata[slack_user_id]'], self.slack_user_id)
        self.assertEqual(stripe_data['metadata[pack_id]'], 'topup_10')
        self.assertEqual(stripe_data['metadata[points_amount]'], '10')
        self.assertEqual(stripe_data['metadata[terms_version_accepted]'], 'roo-points-terms-2026-05-04')
        self.assertEqual(stripe_data['metadata[privacy_version_accepted]'], 'privacy-2026-05-04')

    @override_settings(STRIPE_SECRET_KEY='sk_test_roo')
    @patch('roo.services.requests.post')
    def test_checkout_requires_terms_and_privacy_versions(self, mock_post):
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_5',
            points_amount=5,
            amount_cents=1999,
        )

        response = self.client.post(
            reverse('points-purchase-checkout', kwargs={'pk': purchase.id}),
            {'terms_version_accepted': 'roo-points-terms-2026-05-04'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('terms_version_accepted and privacy_version_accepted are required', response.data['error'])
        mock_post.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY='sk_test_roo')
    @patch('roo.services.requests.post')
    def test_checkout_rejects_expired_purchase(self, mock_post):
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_5',
            points_amount=5,
            amount_cents=1999,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.client.post(
            reverse('points-purchase-checkout', kwargs={'pk': purchase.id}),
            {
                'terms_version_accepted': 'roo-points-terms-2026-05-04',
                'privacy_version_accepted': 'privacy-2026-05-04',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('expired', response.data['error'])
        mock_post.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY='sk_test_roo')
    @patch('roo.services.requests.post')
    def test_checkout_rejects_non_pending_purchase(self, mock_post):
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_5',
            points_amount=5,
            amount_cents=1999,
            status='paid',
            paid_at=timezone.now(),
        )

        response = self.client.post(
            reverse('points-purchase-checkout', kwargs={'pk': purchase.id}),
            {
                'terms_version_accepted': 'roo-points-terms-2026-05-04',
                'privacy_version_accepted': 'privacy-2026-05-04',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('paid', response.data['error'])
        mock_post.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY='')
    def test_checkout_requires_stripe_configuration(self):
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_5',
            points_amount=5,
            amount_cents=1999,
        )

        response = self.client.post(
            reverse('points-purchase-checkout', kwargs={'pk': purchase.id}),
            {
                'terms_version_accepted': 'roo-points-terms-2026-05-04',
                'privacy_version_accepted': 'privacy-2026-05-04',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn('Stripe is not configured', response.data['error'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_create_purchase_rejects_unlinked_slack_user(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UNLINKEDTOPUP',
                'pack_id': 'topup_5',
                'purchase_from': {'source': 'slack'},
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('linked user account', response.data['error'])
        self.assertFalse(PointsPurchase.objects.exists())

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_create_purchase_rejects_invalid_pack(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': self.slack_user_id,
                'pack_id': 'topup_50',
                'purchase_from': {'source': 'slack'},
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Unsupported top-up pack', response.data['error'])
        self.assertFalse(PointsPurchase.objects.exists())

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_create_purchase_requires_object_purchase_from(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': self.slack_user_id,
                'pack_id': 'topup_5',
                'purchase_from': 'slack',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('purchase_from must be an object', response.data['error'])
        self.assertFalse(PointsPurchase.objects.exists())


class StripeWebhookViewTests(APITestCase):
    webhook_secret = 'whsec_roo_test'

    def setUp(self):
        self.url = reverse('points-stripe-webhook')
        self.slack_user_id = 'USTRIPEWEBHOOK123'
        self.user = User.objects.create_user(
            email='stripe-webhook@example.com',
            slack_id=self.slack_user_id,
        )
        self.purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.slack_user_id,
            pack_id='topup_10',
            points_amount=10,
            amount_cents=3699,
            stripe_checkout_session_id='cs_test_roo_paid',
        )

    def _payload(self, session=None, event_type='checkout.session.completed'):
        session = session or {
            'id': 'cs_test_roo_paid',
            'status': 'complete',
            'payment_status': 'paid',
            'metadata': {'points_purchase_id': str(self.purchase.id)},
        }
        return json.dumps({
            'id': 'evt_test_roo_points',
            'type': event_type,
            'data': {'object': session},
        }).encode('utf-8')

    def _signature(self, payload, secret=None, timestamp=None):
        secret = secret or self.webhook_secret
        timestamp = timestamp or int(time.time())
        signed_payload = f"{timestamp}.".encode('utf-8') + payload
        signature = hmac.new(
            secret.encode('utf-8'),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        return f't={timestamp},v1={signature}'

    def _post_webhook(self, payload, signature=None):
        return self.client.post(
            self.url,
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE=signature or self._signature(payload),
        )

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    def test_checkout_completed_webhook_credits_purchase(self):
        payload = self._payload()

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['purchase_id'], str(self.purchase.id))
        self.assertTrue(response.data['credited'])

        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'paid')
        self.assertIsNotNone(self.purchase.paid_at)
        self.assertIsNotNone(self.purchase.ledger_entry)

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
        self.assertEqual(account.earned_balance, 0)
        self.assertEqual(account.purchased_topup_balance, 10)
        self.assertEqual(account.lifetime_earned, 0)
        self.assertEqual(account.lifetime_purchased_topup, 10)

        ledger = self.purchase.ledger_entry
        self.assertEqual(ledger.delta, 10)
        self.assertEqual(ledger.kind, 'EARN')
        self.assertEqual(ledger.source, 'purchased_topup')
        self.assertEqual(ledger.reference_type, 'POINTS_PURCHASE')
        self.assertEqual(ledger.reference_id, str(self.purchase.id))
        self.assertEqual(ledger.idempotency_key, 'stripe_checkout_session:cs_test_roo_paid')

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    def test_checkout_completed_webhook_is_idempotent(self):
        payload = self._payload()

        first_response = self._post_webhook(payload)
        second_response = self._post_webhook(payload)

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data['credited'])
        self.assertFalse(second_response.data['credited'])
        self.assertTrue(second_response.data['already_paid'])
        self.assertEqual(Ledger.objects.count(), 1)

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
        self.assertEqual(account.purchased_topup_balance, 10)

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    def test_checkout_completed_webhook_falls_back_to_session_id(self):
        payload = self._payload(session={
            'id': 'cs_test_roo_paid',
            'status': 'complete',
            'payment_status': 'paid',
            'metadata': {},
        })

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'paid')
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 10)

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    def test_checkout_completed_webhook_rejects_invalid_signature(self):
        payload = self._payload()

        response = self._post_webhook(payload, signature=self._signature(payload, secret='wrong'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'pending')
        self.assertFalse(Ledger.objects.exists())

    @override_settings(STRIPE_WEBHOOK_SECRET='')
    def test_webhook_requires_webhook_secret(self):
        payload = self._payload()

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn('Stripe webhook secret is not configured', response.data['error'])

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    def test_webhook_ignores_unhandled_event_type(self):
        payload = self._payload(event_type='payment_intent.payment_failed')

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['ignored'])
        self.assertFalse(Ledger.objects.exists())

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    @patch('roo.views.SlackService.send_message', return_value=(True, '1712345678.000200'))
    def test_checkout_completed_webhook_posts_paid_confirmation_to_slack_thread(self, mock_send_message):
        self.purchase.purchase_from = {
            'source': 'slack',
            'slack_channel_id': 'C123',
            'slack_thread_ts': '111.222',
        }
        self.purchase.save(update_fields=['purchase_from'])
        payload = self._payload()

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['slack_confirmation_sent'])
        mock_send_message.assert_called_once()
        args, kwargs = mock_send_message.call_args
        self.assertEqual(args[0], 'C123')
        self.assertIn('10 Roo Points have been added', args[1])
        self.assertIn('do not count toward lifetime earned contribution', args[1])
        self.assertEqual(kwargs['thread_ts'], '111.222')

        self.purchase.refresh_from_db()
        self.assertEqual(
            self.purchase.metadata['slack_paid_confirmation_message_ts'],
            '1712345678.000200',
        )

    @override_settings(STRIPE_WEBHOOK_SECRET=webhook_secret)
    @patch('roo.views.SlackService.send_message', return_value=(True, '1712345678.000200'))
    def test_duplicate_paid_webhook_does_not_post_duplicate_slack_confirmation(self, mock_send_message):
        self.purchase.purchase_from = {
            'source': 'slack',
            'slack_channel_id': 'C123',
            'slack_thread_ts': '111.222',
        }
        self.purchase.save(update_fields=['purchase_from'])
        payload = self._payload()

        first_response = self._post_webhook(payload)
        second_response = self._post_webhook(payload)

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        mock_send_message.assert_called_once()
        self.assertTrue(second_response.data['already_paid'])


class CurrentUserBalanceViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('current-user-balance')
        self.user = User.objects.create_user(
            email='current-balance@example.com',
            slack_id='UCURRENTBALANCE',
        )

    def test_requires_authentication(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_gets_zero_balance_and_account_is_created(self):
        self.assertFalse(PointsAccount.objects.filter(user=self.user).exists())
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user_id'], self.user.id)
        self.assertEqual(response.data['email'], self.user.email)
        self.assertEqual(response.data['slack_user_id'], self.user.slack_id)
        self.assertEqual(response.data['balance'], 0)
        self.assertEqual(response.data['earned_balance'], 0)
        self.assertEqual(response.data['purchased_topup_balance'], 0)
        self.assertEqual(response.data['lifetime_earned'], 0)
        self.assertEqual(response.data['lifetime_purchased_topup'], 0)
        self.assertEqual(response.data['lifetime_spent'], 0)
        self.assertEqual(response.data['expired_or_reversed_points'], 0)
        self.assertTrue(PointsAccount.objects.filter(user=self.user).exists())

    def test_authenticated_user_gets_existing_positive_balance(self):
        PointsAccount.objects.create(
            user=self.user,
            balance=17,
            earned_balance=12,
            purchased_topup_balance=5,
            lifetime_earned=30,
            lifetime_purchased_topup=10,
            lifetime_spent=13,
            expired_or_reversed_points=2,
        )
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['balance'], 17)
        self.assertEqual(response.data['earned_balance'], 12)
        self.assertEqual(response.data['purchased_topup_balance'], 5)
        self.assertEqual(response.data['lifetime_earned'], 30)
        self.assertEqual(response.data['lifetime_purchased_topup'], 10)
        self.assertEqual(response.data['lifetime_spent'], 13)
        self.assertEqual(response.data['expired_or_reversed_points'], 2)


class PointsPacksViewTests(APITestCase):
    def test_packs_are_public_and_match_config(self):
        response = self.client.get(reverse('points-packs'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        packs_by_id = {pack['pack_id']: pack for pack in response.data['packs']}
        self.assertEqual(set(packs_by_id), {'topup_5', 'topup_10', 'topup_25'})
        self.assertEqual(packs_by_id['topup_10']['points'], 20)
        self.assertEqual(packs_by_id['topup_10']['amount_cents'], 3699)
        self.assertEqual(packs_by_id['topup_10']['currency'], 'aud')
        self.assertEqual(packs_by_id['topup_25']['points'], 50)
        self.assertEqual(packs_by_id['topup_25']['amount_cents'], 6399)


class CurrentUserPurchaseViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('current-user-purchase')
        self.user = User.objects.create_user(
            email='web-buyer@example.com',
            slack_id='UWEBBUYER',
        )

    def test_requires_authentication(self):
        response = self.client.post(self.url, {'pack_id': 'topup_10'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(DEFAULT_FRONTEND_URL='https://mlai.test')
    def test_authenticated_user_creates_purchase_for_self(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, {'pack_id': 'topup_10'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        purchase = PointsPurchase.objects.get()
        self.assertEqual(purchase.user, self.user)
        self.assertEqual(purchase.pack_id, 'topup_10')
        self.assertEqual(purchase.points_amount, 20)
        self.assertEqual(purchase.amount_cents, 3699)
        self.assertEqual(purchase.status, 'pending')
        self.assertEqual(purchase.purchase_from['source'], 'web')
        self.assertEqual(
            response.data['frontend_checkout_page_url'],
            f'https://mlai.test/roo/topup/{purchase.id}',
        )

    def test_user_without_linked_slack_can_buy(self):
        web_only_user = User.objects.create_user(email='no-slack@example.com')
        self.client.force_authenticate(user=web_only_user)

        response = self.client.post(self.url, {'pack_id': 'topup_5'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        purchase = PointsPurchase.objects.get()
        self.assertEqual(purchase.user, web_only_user)
        self.assertEqual(purchase.slack_user_id, '')

    def test_guardrails_removed_brand_new_account_over_old_cap_is_allowed(self):
        # Brand-new account (would have failed the old 7-day age rule) with a
        # balance over the old 100-point cap: both guardrails are gone now.
        self.user.date_joined = timezone.now()
        self.user.save(update_fields=['date_joined'])
        PointsAccount.objects.create(user=self.user, balance=500)
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, {'pack_id': 'topup_25'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PointsPurchase.objects.get().points_amount, 50)

    def test_invalid_pack_is_rejected(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, {'pack_id': 'topup_999'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PointsPurchase.objects.exists())

    def test_missing_pack_id_is_rejected(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('pack_id is required', response.data['error'])


class ManualAwardViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('manual-award')
        self.admin_slack_id = 'UADMIN123'
        self.partner_slack_id = 'UPARTNER123'
        self.target_slack_id = 'UTARGET456'
        self.admin_user = User.objects.create_user(
            email='manual-admin@example.com',
            slack_id=self.admin_slack_id,
        )
        self.partner_user = User.objects.create_user(
            email='manual-partner@example.com',
            slack_id=self.partner_slack_id,
        )
        
        # Create admin
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            user=self.admin_user,
            role='admin',
            is_active=True
        )
        PointsAdmin.objects.create(
            slack_user_id=self.partner_slack_id,
            user=self.partner_user,
            role='partner',
            is_active=True,
        )

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_slack_user_id_success(self, mock_permission):
        """Test award works with slack_user_id parameter."""
        data = {
            'slack_user_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10,
            'reason': 'Test award'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_whitespace_stripped(self, mock_permission):
        """Test that whitespace is stripped from IDs."""
        data = {
            'slack_user_id': f"  {self.admin_slack_id}  ",
            'target_slack_id': f" {self.target_slack_id} ",
            'points': 10,
            'reason': 'Test whitespace'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_partner_cannot_award_points(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': self.partner_slack_id,
                'target_slack_id': self.target_slack_id,
                'points': 10,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'Only Points Admins can award points manually')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_legacy_admin_slack_id(self, mock_permission):
        """Test that legacy admin_slack_id still works."""
        data = {
            'admin_slack_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_requires_linked_admin_user(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id='UNLINKEDADMIN',
            role='admin',
            is_active=True,
        )

        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UNLINKEDADMIN',
                'target_slack_id': self.target_slack_id,
                'points': 10,
                'reason': 'Should fail',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('linked user account', response.data['error'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_requires_real_points_admin_role(self, mock_permission):
        non_admin_user = User.objects.create_user(
            email='not-admin@example.com',
            slack_id='UNOTADMINLINKED',
        )

        response = self.client.post(
            self.url,
            {
                'slack_user_id': non_admin_user.slack_id,
                'target_slack_id': self.target_slack_id,
                'points': 10,
                'reason': 'Should fail',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Only Points Admins can award points manually', response.data['error'])


class SystemAwardViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('system-award')
        self.target_slack_id = 'USYSTEMTARGET'

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_system_award_can_award_without_human_points_admin(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'created_by_slack_id': 'UROOBOT',
                'target_slack_id': self.target_slack_id,
                'points': 12,
                'reason': 'System award test',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['points_awarded'], 12)
        ledger = Ledger.objects.get(created_by_slack_id='UROOBOT')
        self.assertEqual(ledger.source, 'EVENT')
        self.assertEqual(ledger.delta, 12)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_system_award_uses_supplied_idempotency_key(self, mock_permission):
        User.objects.create_user(
            email='system-idempotent@example.com',
            slack_id='USYSTEMIDEMPOTENT',
        )
        data = {
            'created_by_slack_id': 'UROOBOT',
            'target_slack_id': 'USYSTEMIDEMPOTENT',
            'points': 12,
            'reason': 'System award test',
            'idempotency_key': 'link_love:CBOOST:111.000:USYSTEMIDEMPOTENT',
        }

        first_response = self.client.post(self.url, data, format='json')
        second_response = self.client.post(self.url, data, format='json')

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_response.data['ledger_id'], second_response.data['ledger_id'])
        self.assertEqual(
            Ledger.objects.filter(idempotency_key='link_love:CBOOST:111.000:USYSTEMIDEMPOTENT').count(),
            1,
        )
        balance = PointsService.get_balance(User.objects.get(slack_id='USYSTEMIDEMPOTENT'))
        self.assertEqual(balance['balance'], 12)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile')
    def test_system_award_handles_empty_slack_profile_names(self, mock_profile, mock_permission):
        mock_profile.return_value = {
            'real_name': '',
            'display_name': '',
            'name': '',
            'email': 'empty-profile@example.com',
            'image_url': 'https://example.com/avatar.png',
        }

        response = self.client.post(
            self.url,
            {
                'created_by_slack_id': 'UROOBOT',
                'target_slack_id': 'UEMPTYPROFILE',
                'points': 2,
                'reason': 'System award test',
                'idempotency_key': 'system-empty-profile',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(slack_id='UEMPTYPROFILE')
        self.assertEqual(user.first_name, 'Unknown')
        self.assertEqual(user.last_name, 'Slack User')
        self.assertEqual(user.email, 'empty-profile@example.com')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile')
    def test_system_award_uses_display_name_when_real_name_missing(self, mock_profile, mock_permission):
        mock_profile.return_value = {
            'real_name': None,
            'display_name': 'Helpful Founder',
            'name': 'helpful_founder',
            'email': 'helpful-founder@example.com',
        }

        response = self.client.post(
            self.url,
            {
                'created_by_slack_id': 'UROOBOT',
                'target_slack_id': '<@UDISPLAYNAME>',
                'points': 2,
                'reason': 'System award test',
                'idempotency_key': 'system-display-name',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(slack_id='UDISPLAYNAME')
        self.assertEqual(user.first_name, 'Helpful')
        self.assertEqual(user.last_name, 'Founder')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value=None)
    def test_system_award_creates_placeholder_when_slack_profile_missing(self, mock_profile, mock_permission):
        response = self.client.post(
            self.url,
            {
                'created_by_slack_id': 'UROOBOT',
                'target_slack_id': 'UNOPROFILE',
                'points': 2,
                'reason': 'System award test',
                'idempotency_key': 'system-no-profile',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(slack_id='UNOPROFILE')
        self.assertEqual(user.email, 'UNOPROFILE@slack.placeholder.com')
        self.assertEqual(user.first_name, 'Unknown Slack User')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_system_award_rejects_non_positive_points(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'target_slack_id': self.target_slack_id,
                'points': 0,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'System awards must be positive')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_system_award_rejects_empty_slack_mention(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'target_slack_id': '<@>',
                'points': 2,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'target_slack_id and points are required')


class PointsAdminManagementViewSetTests(APITestCase):
    def setUp(self):
        self.super_admin_slack_id = 'U05QPB483K9'
        self.other_slack_id = 'UNOTSUPER'
        self.target_slack_id = 'UTARGET456'
        self.list_url = reverse('points-admin-list')
        self.detail_url = reverse(
            'points-admin-detail',
            kwargs={'slack_user_id': self.target_slack_id},
        )
        self.target_user = User.objects.create_user(
            email='target@example.com',
            slack_id=self.target_slack_id,
        )

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_creates_admin(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.role, 'committee')
        self.assertEqual(admin.user, self.target_user)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertFalse(response.data['already_admin'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_is_idempotent_when_already_active(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            added_by_slack_id='UOLDER',
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).count(), 1)
        self.assertTrue(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_reactivates_inactive_admin_and_preserves_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
            added_by_slack_id='UOLDER',
            weekly_allowance=175,
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.weekly_allowance, 175)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertFalse(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_requires_super_admin_requester(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.other_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).exists())
        self.assertIn('super admin', response.data['error'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            weekly_allowance=100,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertEqual(admin.weekly_allowance, 150)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertEqual(response.data['weekly_allowance'], 150)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_rejects_non_positive_values(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'weekly_allowance must be positive')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_requires_existing_admin(self, mock_permission):
        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_revokes_active_admin(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertFalse(admin.is_active)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_is_idempotent_when_already_inactive(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['already_revoked'])
        self.assertFalse(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_existing_admin(self, mock_permission):
        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_super_admin_requester(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.other_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertIn('super admin', response.data['error'])


class PointsRequestViewSetTests(APITestCase):
    def setUp(self):
        self.requester = User.objects.create_user(
            email='requester@example.com',
            slack_id='UREQUESTER',
        )
        self.admin = User.objects.create_user(
            email='admin@example.com',
            slack_id='UADMIN123',
        )
        PointsAdmin.objects.create(
            slack_user_id='UADMIN123',
            user=self.admin,
            role='admin',
            is_active=True,
        )
        self.list_url = reverse('points-request-list')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_create_points_request(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': 'UREQUESTER',
                'target_slack_id': 'UREQUESTER',
                'points': 12,
                'reason': 'Running the 21st x MLAI event',
                'slack_channel_id': 'C123',
                'slack_thread_ts': '111.222',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        points_request = PointsRequest.objects.get()
        self.assertEqual(points_request.status, 'pending')
        self.assertEqual(points_request.points, 12)
        self.assertEqual(points_request.slack_channel_id, 'C123')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_attach_and_lookup_points_request_by_slack_message(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
            slack_channel_id='C123',
            slack_thread_ts='111.222',
        )

        attach_url = reverse('points-request-attach-slack-summary', args=[points_request.id])
        response = self.client.patch(
            attach_url,
            {
                'slack_channel_id': 'C123',
                'slack_thread_ts': '111.222',
                'slack_summary_message_ts': '222.333',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        points_request.refresh_from_db()
        self.assertEqual(points_request.slack_summary_message_ts, '222.333')

        lookup_url = reverse('points-request-by-slack-message')
        response = self.client.get(
            lookup_url,
            {
                'slack_channel_id': 'C123',
                'slack_message_ts': '222.333',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], points_request.id)
        self.assertEqual(response.data['status'], 'pending')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_approve_points_request_awards_points(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
            slack_channel_id='C123',
            slack_thread_ts='111.222',
            slack_summary_message_ts='222.333',
        )

        approve_url = reverse('points-request-approve', args=[points_request.id])
        response = self.client.post(
            approve_url,
            {'admin_slack_id': 'UADMIN123'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        points_request.refresh_from_db()
        self.requester.refresh_from_db()
        self.assertEqual(points_request.status, 'approved')
        self.assertEqual(points_request.approved_by_slack_id, 'UADMIN123')
        self.assertEqual(response.data['points_awarded'], 12)
        self.assertEqual(response.data['new_balance'], 12)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_approve_points_request_requires_points_admin(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
        )

        approve_url = reverse('points-request-approve', args=[points_request.id])
        response = self.client.post(
            approve_url,
            {'admin_slack_id': 'UNOTADMIN'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'Not a points admin')


class TaskViewSetTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='task-admin@example.com',
            slack_id='UTASKADMIN',
        )
        self.reviewer = User.objects.create_user(
            email='reviewer@example.com',
            slack_id='UREVIEWER',
        )
        PointsAdmin.objects.create(
            slack_user_id='UTASKADMIN',
            user=self.admin,
            role='admin',
            is_active=True,
        )

    def _make_task(self, **overrides):
        defaults = {
            'title': 'Implement volunteer flow',
            'description': 'Task description',
            'portfolio': 'tech',
            'work_domain': 'tech',
            'review_flow': 'pr_review',
            'points': 18,
            'points_estimate': 18,
            'points_min': 18,
            'points_max': 18,
            'created_by_user_id': 'UTASKADMIN',
            'reviewer_slack_id': 'UREVIEWER',
            'volunteer_ready': True,
            'repo': 'mlai-backend',
            'acceptance_criteria': 'Ship the change',
            'how_to_test': 'Run the tests',
            'estimate_minutes': 30,
        }
        defaults.update(overrides)
        return Task.objects.create(**defaults)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value={
        'email': 'new-volunteer@example.com',
        'real_name': 'New Volunteer',
        'image_url': 'https://example.com/avatar.png',
    })
    def test_claim_auto_links_user_and_creates_assignment(self, mock_profile, mock_permission):
        task = self._make_task()
        response = self.client.post(
            reverse('task-claim', args=[task.id]),
            {'slack_user_id': 'UNEWVOL'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        volunteer = User.objects.get(slack_id='UNEWVOL')
        assignment = TaskAssignment.objects.get(task=task)
        self.assertEqual(task.status, 'claimed')
        self.assertEqual(task.assigned_user, volunteer)
        self.assertEqual(assignment.status, 'claimed')
        self.assertEqual(assignment.assigned_user, volunteer)
        self.assertTrue(task.task_code.startswith('ROO-'))

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_reject_without_submission_id_rejects_latest_pending_submission(self, mock_permission):
        volunteer = User.objects.create_user(
            email='volunteer@example.com',
            slack_id='UVOL',
        )
        task = self._make_task(status='claimed', assigned_user=volunteer, assigned_to_user_id='UVOL')
        assignment = TaskAssignment.objects.create(
            task=task,
            assigned_user=volunteer,
            assigned_to_slack_id='UVOL',
            status='submitted',
        )
        rejected = TaskSubmission.objects.create(
            task=task,
            assignment=assignment,
            user=volunteer,
            submission_text='Attempt one',
            status='rejected',
        )
        pending = TaskSubmission.objects.create(
            task=task,
            assignment=assignment,
            user=volunteer,
            submission_text='Attempt two',
            status='submitted',
        )

        response = self.client.post(
            reverse('task-reject', args=[task.id]),
            {'slack_user_id': 'UREVIEWER', 'reason': 'Needs another pass'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rejected.refresh_from_db()
        pending.refresh_from_db()
        assignment.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(rejected.status, 'rejected')
        self.assertEqual(pending.status, 'rejected')
        self.assertEqual(pending.reviewed_by_slack_id, 'UREVIEWER')
        self.assertEqual(assignment.status, 'claimed')
        self.assertEqual(task.status, 'claimed')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_unclaim_is_blocked_after_any_submission_exists(self, mock_permission):
        volunteer = User.objects.create_user(
            email='volunteer2@example.com',
            slack_id='UVOL2',
        )
        task = self._make_task(status='claimed', assigned_user=volunteer, assigned_to_user_id='UVOL2')
        assignment = TaskAssignment.objects.create(
            task=task,
            assigned_user=volunteer,
            assigned_to_slack_id='UVOL2',
            status='claimed',
        )
        TaskSubmission.objects.create(
            task=task,
            assignment=assignment,
            user=volunteer,
            submission_text='Earlier attempt',
            status='rejected',
        )

        response = self.client.post(
            reverse('task-unclaim', args=[task.id]),
            {'slack_user_id': 'UVOL2'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cannot be unclaimed', response.data['error'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value={
        'email': 'coder@example.com',
        'real_name': 'Coder Volunteer',
        'image_url': 'https://example.com/coder.png',
    })
    def test_multi_submission_flow_awards_points_once(self, mock_profile, mock_permission):
        task = self._make_task(points=24, points_estimate=24, points_min=24, points_max=24)

        claim_response = self.client.post(
            reverse('task-claim', args=[task.id]),
            {'slack_user_id': 'UCODER'},
            format='json',
        )
        self.assertEqual(claim_response.status_code, status.HTTP_200_OK)

        first_submit = self.client.post(
            reverse('task-submit', args=[task.id]),
            {'slack_user_id': 'UCODER', 'submission_text': 'First try'},
            format='json',
        )
        self.assertEqual(first_submit.status_code, status.HTTP_201_CREATED)

        reject_response = self.client.post(
            reverse('task-reject', args=[task.id]),
            {'slack_user_id': 'UREVIEWER', 'reason': 'Please fix the tests'},
            format='json',
        )
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)

        second_submit = self.client.post(
            reverse('task-submit', args=[task.id]),
            {'slack_user_id': 'UCODER', 'submission_text': 'Second try'},
            format='json',
        )
        self.assertEqual(second_submit.status_code, status.HTTP_201_CREATED)

        approve_response = self.client.post(
            reverse('task-approve', args=[task.id]),
            {'slack_user_id': 'UREVIEWER'},
            format='json',
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)

        task.refresh_from_db()
        assignment = TaskAssignment.objects.get(task=task)
        submissions = list(TaskSubmission.objects.filter(task=task).order_by('created_at'))
        volunteer = User.objects.get(slack_id='UCODER')

        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0].status, 'rejected')
        self.assertEqual(submissions[1].status, 'approved')
        self.assertEqual(assignment.status, 'approved')
        self.assertEqual(task.status, 'approved')
        self.assertEqual(Ledger.objects.filter(reference_type='TASK_ASSIGNMENT', reference_id=str(assignment.id)).count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=volunteer).balance, 24)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_task_code_lookup_and_claimable_filter(self, mock_permission):
        open_task = self._make_task(title='Open fix', estimate_minutes=20)
        self._make_task(title='Not ready', volunteer_ready=False, created_by_user_id='UTASKADMIN')
        volunteer = User.objects.create_user(
            email='claimed-filter@example.com',
            slack_id='UFILTER',
        )
        claimed_task = self._make_task(title='Claimed feature', status='claimed', assigned_user=volunteer, assigned_to_user_id='UFILTER')
        TaskAssignment.objects.create(
            task=claimed_task,
            assigned_user=volunteer,
            assigned_to_slack_id='UFILTER',
            claimed_points_snapshot=claimed_task.points_estimate,
            status='claimed',
        )

        by_code_response = self.client.get(
            reverse('task-by-code', kwargs={'task_code': open_task.task_code}),
        )
        self.assertEqual(by_code_response.status_code, status.HTTP_200_OK)
        self.assertEqual(by_code_response.data['id'], open_task.id)

        claimable_response = self.client.get(reverse('task-list'), {'claimable': 'true'})
        self.assertEqual(claimable_response.status_code, status.HTTP_200_OK)
        returned_ids = {row['id'] for row in claimable_response.data}
        self.assertIn(open_task.id, returned_ids)
        self.assertNotIn(claimed_task.id, returned_ids)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_patch_task_requires_matching_updated_at(self, mock_permission):
        task = self._make_task()
        response = self.client.patch(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'UTASKADMIN',
                'expected_updated_at': '2026-01-01T00:00:00Z',
                'title': 'Updated title',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('Task changed', response.data['error'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_patch_task_rejects_unsupported_fields(self, mock_permission):
        task = self._make_task()
        response = self.client.patch(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'UTASKADMIN',
                'expected_updated_at': task.updated_at.isoformat(),
                'status': 'approved',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('You can edit:', response.data['error'])
        self.assertIn('status', response.data['unsupported_fields'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_put_task_uses_same_partial_update_contract(self, mock_permission):
        task = self._make_task()
        response = self.client.put(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'UTASKADMIN',
                'expected_updated_at': task.updated_at.isoformat(),
                'title': 'Retitled task',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.title, 'Retitled task')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_patch_task_requires_admin_role_even_for_linked_user(self, mock_permission):
        task = self._make_task()
        User.objects.create_user(
            email='linked-non-admin@example.com',
            slack_id='ULINKEDNONADMIN',
        )
        response = self.client.patch(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'ULINKEDNONADMIN',
                'expected_updated_at': task.updated_at.isoformat(),
                'title': 'Should not work',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Only Points Admins can edit tasks', response.data['error'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_cancel_task_cancels_active_assignment_with_history(self, mock_permission):
        volunteer = User.objects.create_user(
            email='cancel-volunteer@example.com',
            slack_id='UCANCELVOL',
        )
        task = self._make_task(status='claimed', assigned_user=volunteer, assigned_to_user_id='UCANCELVOL')
        assignment = TaskAssignment.objects.create(
            task=task,
            assigned_user=volunteer,
            assigned_to_slack_id='UCANCELVOL',
            claimed_points_snapshot=task.points_estimate,
            status='claimed',
        )
        TaskSubmission.objects.create(
            task=task,
            assignment=assignment,
            user=volunteer,
            submission_text='Earlier attempt',
            status='rejected',
        )

        response = self.client.post(
            reverse('task-cancel', args=[task.id]),
            {'slack_user_id': 'UTASKADMIN', 'reason': 'No longer needed'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(assignment.status, 'cancelled')
        self.assertEqual(assignment.closed_reason, 'task_cancelled')
        self.assertEqual(TaskSubmission.objects.filter(task=task).count(), 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_delete_task_returns_405(self, mock_permission):
        task = self._make_task()
        response = self.client.delete(reverse('task-detail', args=[task.id]))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_cancel_task_requires_linked_points_admin(self, mock_permission):
        task = self._make_task()
        response = self.client.post(
            reverse('task-cancel', args=[task.id]),
            {'slack_user_id': 'UNOTADMIN'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value={
        'email': 'freeze-volunteer@example.com',
        'real_name': 'Freeze Volunteer',
        'image_url': 'https://example.com/freeze.png',
    })
    def test_claimed_task_points_are_frozen_when_task_points_change(self, mock_profile, mock_permission):
        task = self._make_task(points=18, points_estimate=18, points_min=18, points_max=18)
        self.client.post(
            reverse('task-claim', args=[task.id]),
            {'slack_user_id': 'UFREEZE'},
            format='json',
        )
        task.refresh_from_db()

        patch_response = self.client.patch(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'UTASKADMIN',
                'expected_updated_at': task.updated_at.isoformat(),
                'points': 30,
            },
            format='json',
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)

        submit_response = self.client.post(
            reverse('task-submit', args=[task.id]),
            {'slack_user_id': 'UFREEZE', 'submission_text': 'Done'},
            format='json',
        )
        self.assertEqual(submit_response.status_code, status.HTTP_201_CREATED)

        approve_response = self.client.post(
            reverse('task-approve', args=[task.id]),
            {'slack_user_id': 'UREVIEWER'},
            format='json',
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)

        assignment = TaskAssignment.objects.get(task=task)
        volunteer = User.objects.get(slack_id='UFREEZE')
        task.refresh_from_db()
        self.assertEqual(task.points, 30)
        self.assertEqual(assignment.claimed_points_snapshot, 18)
        self.assertEqual(assignment.awarded_points, 18)
        self.assertEqual(PointsAccount.objects.get(user=volunteer).balance, 18)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value={
        'email': 'edit-active@example.com',
        'real_name': 'Edit Active',
        'image_url': 'https://example.com/edit-active.png',
    })
    def test_edit_during_active_assignment_preserves_submission_foundations(self, mock_profile, mock_permission):
        task = self._make_task(points=18, points_estimate=18, points_min=18, points_max=18)
        self.client.post(
            reverse('task-claim', args=[task.id]),
            {'slack_user_id': 'UEDITACTIVE'},
            format='json',
        )
        task.refresh_from_db()

        response = self.client.patch(
            reverse('task-detail', args=[task.id]),
            {
                'slack_user_id': 'UTASKADMIN',
                'expected_updated_at': task.updated_at.isoformat(),
                'points': 30,
                'acceptance_criteria': 'Updated criteria',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        assignment = TaskAssignment.objects.get(task=task)
        self.assertEqual(task.points, 30)
        self.assertEqual(task.acceptance_criteria, 'Updated criteria')
        self.assertEqual(assignment.claimed_points_snapshot, 18)


class CoworkingViewSetTests(APITestCase):
    def setUp(self):
        self.url = reverse('coworking-book')
        self.user = User.objects.create_user(
            email='coworking@example.com',
            slack_id='UCOBOOK',
        )
        PointsService.award(
            user=self.user,
            delta=10,
            source='MANUAL',
            description='Coworking setup',
            created_by_slack_id='UADMIN',
            idempotency_key='coworking_api_setup',
        )

    def _verify_company_for(self, org):
        """Attach a verified registered company to ``org`` (required for the discount)."""
        from django.utils import timezone
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

        profile, _ = VibeRaisingProfile.objects.get_or_create(
            user=self.user, defaults={'role': VibeRaisingProfile.ROLE_FOUNDER}
        )
        VibeRaisingCompany.objects.create(
            profile=profile, organization=org, name='Acme Pty Ltd',
            registered=True, acn='000000019', abr_verified_at=timezone.now(),
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_availability_quotes_standard_cost_without_user(self, mock_permission):
        url = reverse('coworking-availability')
        response = self.client.get(url, {'days': 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['cost_points'], 8)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_availability_quotes_discounted_cost_for_user_with_ready_update(self, mock_permission):
        from datetime import date
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        org = Organization.objects.create(name='Acme', domain='acme.coworking.example')
        UserStartupBinding.objects.create(user=self.user, organization=org)
        self._verify_company_for(org)
        MonthlyUpdateDraft.objects.create(
            organization=org,
            month=date.today().replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

        url = reverse('coworking-availability')
        response = self.client.get(url, {'days': 1, 'slack_user_id': self.user.slack_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['cost_points'], 4)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_book_response_flags_standard_price_without_discount(self, mock_permission):
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.post(
            self.url,
            {'slack_user_id': self.user.slack_id, 'date': booking_date},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['points_cost'], 8)
        self.assertEqual(response.data['standard_points_cost'], 8)
        self.assertFalse(response.data['monthly_update_discount_applied'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_book_response_flags_discount_when_monthly_update_ready(self, mock_permission):
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        booking_date = date.today() + timedelta(days=1)
        org = Organization.objects.create(name='Acme', domain='acme.book.example')
        UserStartupBinding.objects.create(user=self.user, organization=org)
        self._verify_company_for(org)
        MonthlyUpdateDraft.objects.create(
            organization=org,
            month=booking_date.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

        response = self.client.post(
            self.url,
            {'slack_user_id': self.user.slack_id, 'date': booking_date.isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['points_cost'], 4)
        self.assertEqual(response.data['standard_points_cost'], 8)
        self.assertTrue(response.data['monthly_update_discount_applied'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_book_endpoint_is_idempotent_for_existing_booking(self, mock_permission):
        booking_date = (date.today() + timedelta(days=1)).isoformat()

        first_response = self.client.post(
            self.url,
            {
                'slack_user_id': self.user.slack_id,
                'date': booking_date,
                'slack_channel_id': 'C123',
            },
            format='json',
        )
        second_response = self.client.post(
            self.url,
            {
                'slack_user_id': self.user.slack_id,
                'date': booking_date,
                'slack_channel_id': 'C123',
            },
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertTrue(second_response.data['already_booked'])
        self.assertTrue(second_response.data['idempotent'])
        self.assertEqual(
            CoworkingBooking.objects.filter(user=self.user, date=booking_date, status='booked').count(),
            1,
        )
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 2)  # charged once at the standard 8


class CoworkingReportViewSetTests(APITestCase):
    def setUp(self):
        self.url = reverse('coworking-report')
        self.admin_slack_id = 'UPOINTSADMIN'
        self.partner_slack_id = 'UPOINTSPARTNER'
        self.other_slack_id = 'UNOTADMIN'
        self.user_1 = User.objects.create_user(email='report1@example.com', slack_id='UREPORT1')
        self.user_2 = User.objects.create_user(email='report2@example.com', slack_id='UREPORT2')
        self.user_3 = User.objects.create_user(email='report3@example.com', slack_id='UREPORT3')
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True,
        )
        PointsAdmin.objects.create(
            slack_user_id=self.partner_slack_id,
            role='partner',
            is_active=True,
        )

    def _create_booking(self, user, booking_date, status='booked'):
        return CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status=status,
            points_cost=4,
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_counts_active_bookings_and_includes_rollups(self, mock_permission):
        self._create_booking(self.user_1, date(2026, 1, 1))
        self._create_booking(self.user_2, date(2026, 1, 1))
        self._create_booking(self.user_1, date(2026, 1, 2), status='cancelled')
        self._create_booking(self.user_1, date(2026, 1, 5))
        self._create_booking(self.user_3, date(2026, 1, 10))
        self._create_booking(self.user_2, date(2026, 2, 1))

        response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2026-02-03',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['range']['source'], 'active_coworking_bookings')
        self.assertEqual(response.data['totals']['booked_user_days'], 5)
        self.assertEqual(response.data['totals']['unique_users'], 3)
        self.assertEqual(response.data['totals']['active_days'], 4)
        self.assertEqual(response.data['totals']['range_days'], 34)
        self.assertEqual(response.data['totals']['average_per_day'], 0.15)
        self.assertEqual(
            response.data['totals']['busiest_days'],
            [{'date': '2026-01-01', 'booked_users': 2}],
        )

        daily_by_date = {row['date']: row['booked_users'] for row in response.data['daily']}
        self.assertEqual(daily_by_date['2026-01-01'], 2)
        self.assertEqual(daily_by_date['2026-01-02'], 0)
        self.assertEqual(daily_by_date['2026-01-03'], 0)
        self.assertEqual(daily_by_date['2026-02-01'], 1)
        self.assertEqual(len(response.data['daily']), 34)

        weekly_total = sum(row['booked_user_days'] for row in response.data['weekly'])
        monthly_by_month = {row['month']: row['booked_user_days'] for row in response.data['monthly']}
        self.assertEqual(weekly_total, 5)
        self.assertEqual(monthly_by_month['2026-01'], 4)
        self.assertEqual(monthly_by_month['2026-02'], 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_can_generate_report(self, mock_permission):
        self._create_booking(self.user_1, date(2026, 1, 1))

        response = self.client.get(
            self.url,
            {
                'slack_user_id': self.partner_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['totals']['booked_user_days'], 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_requires_points_admin(self, mock_permission):
        response = self.client.get(
            self.url,
            {
                'slack_user_id': self.other_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Points Admins', response.data['error'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_rejects_invalid_ranges(self, mock_permission):
        invalid_date_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-99-01',
                'end_date': '2026-01-31',
            },
        )
        reversed_range_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-02-01',
                'end_date': '2026-01-31',
            },
        )
        too_long_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2027-01-02',
            },
        )

        self.assertEqual(invalid_date_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reversed_range_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(too_long_response.status_code, status.HTTP_400_BAD_REQUEST)


class PartnerRoleRestrictionTests(APITestCase):
    def setUp(self):
        self.partner_slack_id = 'UPARTNERONLY'
        self.full_admin_slack_id = 'UFULLADMIN'
        self.member = User.objects.create_user(email='member@example.com', slack_id='UMEMBER')
        self.other_member = User.objects.create_user(email='other-member@example.com', slack_id='UOTHERBOOK')
        PointsAdmin.objects.create(
            slack_user_id=self.partner_slack_id,
            role='partner',
            is_active=True,
        )
        PointsAdmin.objects.create(
            slack_user_id=self.full_admin_slack_id,
            role='committee',
            is_active=True,
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_cannot_create_or_administer_tasks(self, mock_permission):
        create_response = self.client.post(
            reverse('task-list'),
            {
                'slack_user_id': self.partner_slack_id,
                'title': 'Partner blocked task',
                'points': 5,
                'created_by_user_id': self.partner_slack_id,
            },
            format='json',
        )
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

        task = Task.objects.create(
            title='Existing task',
            points=5,
            created_by_user_id=self.full_admin_slack_id,
            assigned_to_user_id=self.member.slack_id,
            assigned_user=self.member,
            status='submitted',
        )
        approve_response = self.client.post(
            reverse('task-approve', args=[task.id]),
            {'slack_user_id': self.partner_slack_id},
            format='json',
        )
        reject_response = self.client.post(
            reverse('task-reject', args=[task.id]),
            {'slack_user_id': self.partner_slack_id},
            format='json',
        )
        award_response = self.client.post(
            reverse('task-award', args=[task.id]),
            {
                'created_by_user_id': self.partner_slack_id,
                'assigned_to_user_id': self.member.slack_id,
            },
            format='json',
        )

        self.assertEqual(approve_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(reject_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(award_response.status_code, status.HTTP_403_FORBIDDEN)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_cannot_approve_points_requests(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id=self.member.slack_id,
            target_slack_id=self.member.slack_id,
            points=5,
            reason='Partner restriction test',
        )

        response = self.client.post(
            reverse('points-request-approve', args=[points_request.id]),
            {'admin_slack_id': self.partner_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_cannot_fetch_admin_allowance(self, mock_permission):
        response = self.client.get(
            reverse('admin-allowance'),
            {'slack_id': self.partner_slack_id},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_cannot_administer_rewards(self, mock_permission):
        reward = RewardsCatalog.objects.create(
            code='PARTNER_BLOCKED_REWARD',
            name='Partner Blocked Reward',
            cost_points=1,
        )
        redemption = RewardRedemption.objects.create(
            user=self.member,
            reward=reward,
            quantity=1,
            status='requested',
        )

        approve_response = self.client.post(
            reverse('rewards-approve'),
            {
                'slack_user_id': self.partner_slack_id,
                'redemption_id': redemption.id,
            },
            format='json',
        )
        pending_response = self.client.get(
            reverse('rewards-pending'),
            {'slack_user_id': self.partner_slack_id},
        )

        self.assertEqual(approve_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(pending_response.status_code, status.HTTP_403_FORBIDDEN)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_partner_cannot_set_capacity_or_cancel_other_user_booking(self, mock_permission):
        booking = CoworkingBooking.objects.create(
            user=self.other_member,
            date=date(2026, 1, 5),
            status='booked',
            points_cost=4,
        )

        capacity_response = self.client.post(
            reverse('coworking-set-capacity'),
            {
                'slack_user_id': self.partner_slack_id,
                'date': '2026-01-05',
                'capacity': 20,
            },
            format='json',
        )
        cancel_response = self.client.post(
            reverse('coworking-cancel'),
            {
                'slack_user_id': self.partner_slack_id,
                'booking_id': booking.id,
            },
            format='json',
        )

        self.assertEqual(capacity_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(cancel_response.status_code, status.HTTP_403_FORBIDDEN)


class FirstChannelPostAwardViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='intro@example.com',
            slack_id='UINTRO',
        )
        self.url = reverse('first_post_award')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_creates_marker_and_ledger_entry(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['awarded'], True)
        self.assertEqual(response.data['new_balance'], 4)
        self.assertEqual(response.data['points_awarded'], 4)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)

        ledger = Ledger.objects.get(idempotency_key='first_post_award:UINTRO:CSTART')
        self.assertEqual(ledger.delta, 4)
        self.assertEqual(ledger.description, 'Completed quest: First Contact')
        self.assertEqual(ledger.created_by_slack_id, 'SYSTEM')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_is_idempotent_on_repeat_request(self, mock_permission):
        first_response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )
        second_response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_response.data['awarded'], True)
        self.assertEqual(second_response.data['awarded'], False)
        self.assertEqual(first_response.data['points_awarded'], 4)
        self.assertNotIn('points_awarded', second_response.data)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile')
    def test_first_post_award_creates_missing_user_from_slack_profile(self, mock_profile, mock_permission):
        mock_profile.return_value = {
            'real_name': None,
            'display_name': 'New Founder',
            'name': 'new_founder',
            'email': 'new-founder@example.com',
            'image_url': 'https://example.com/avatar.png',
        }

        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UNEWINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['awarded'], True)
        self.assertEqual(response.data['new_balance'], 4)
        self.assertEqual(response.data['points_awarded'], 4)

        user = User.objects.get(slack_id='UNEWINTRO')
        self.assertEqual(user.email, 'new-founder@example.com')
        self.assertEqual(user.first_name, 'New')
        self.assertEqual(user.last_name, 'Founder')
        self.assertEqual(user.avatar_url, 'https://example.com/avatar.png')

        ledger = Ledger.objects.get(idempotency_key='first_post_award:UNEWINTRO:CSTART')
        self.assertEqual(ledger.user, user)
        self.assertEqual(ledger.delta, 4)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.SlackService.get_user_profile', return_value=None)
    def test_first_post_award_creates_placeholder_user_when_slack_profile_missing(self, mock_profile, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UNOPROFILEINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['awarded'], True)
        self.assertEqual(response.data['new_balance'], 4)
        self.assertEqual(response.data['points_awarded'], 4)

        user = User.objects.get(slack_id='UNOPROFILEINTRO')
        self.assertEqual(user.email, 'UNOPROFILEINTRO@slack.placeholder.com')
        self.assertEqual(user.first_name, 'Unknown Slack User')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.PointsService.award', side_effect=RuntimeError('boom'))
    def test_first_post_award_rolls_back_marker_when_points_award_fails(self, mock_award, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 0)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 0)


class FirstChannelPostAwardConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(
            email='intro-concurrency@example.com',
            slack_id='UINTRO',
        )
        self.url = reverse('first_post_award')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_is_idempotent_under_concurrency(self, mock_permission):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            client = APIClient()
            try:
                barrier.wait(timeout=5)
                response = client.post(
                    self.url,
                    {
                        'slack_user_id': 'UINTRO',
                        'channel_id': 'CSTART',
                    },
                    format='json',
                )
                results.append(response.data)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 1)
        self.assertEqual(sum(1 for result in results if result.get('awarded') is True), 1)
        self.assertEqual(sum(1 for result in results if result.get('awarded') is False), 1)
        awarded_results = [result for result in results if result.get('awarded') is True]
        self.assertEqual(awarded_results[0].get('points_awarded'), 4)
