"""Database acceptance: run only after approval of the migration inventory."""
import secrets
import uuid
from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from core.models import User
from roo.models import CybertestCheckout, Ledger, PointsAccount
from roo.cybertest_policy import CheckoutError
from roo import cybertest_checkout as checkout


@override_settings(CYBERTEST_BILLING_MODE='test', CYBERTEST_AUDIT_POINTS=10,
                   CYBERTEST_TEST_BALANCE_POINTS=20)
class CheckoutAcceptanceTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user(email='payer@example.invalid')
        self.other=User.objects.create_user(email='other@example.invalid')
        self.intent={'github_actor_id':'123','repository_id':'456',
            'workspace_id':str(uuid.uuid4()),'repository':'customer/project',
            'scope':['get-client-referrals']}
        self.id=uuid.uuid4();self.token=secrets.token_urlsafe(32);self.audit_id=uuid.uuid4()
        checkout.create_quote(self.id,self.token,self.intent)

    def authorize(self):
        return checkout.confirm(self.id,self.token,self.user,expected_points=10)

    def test_confirmation_binds_verified_user_and_duplicate_start_debits_once(self):
        self.authorize();self.authorize()
        first=checkout.charge(self.id,self.audit_id,self.intent)
        replay=checkout.charge(self.id,self.audit_id,self.intent)
        self.assertEqual(first['status'],'charged');self.assertEqual(first,replay)
        self.assertEqual(checkout.balance(self.user,test_mode=True),10)
        self.assertEqual(Ledger.objects.count(),0)

    def test_other_account_or_changed_scope_cannot_reuse_consent(self):
        self.authorize()
        with self.assertRaises(CheckoutError):checkout.confirm(self.id,self.token,self.other,expected_points=10)
        with self.assertRaises(CheckoutError):checkout.charge(self.id,self.audit_id,{**self.intent,'scope':['other']})
        self.assertEqual(checkout.balance(self.user,test_mode=True),20)

    def test_test_mode_insufficient_balance_blocks_start(self):
        with override_settings(CYBERTEST_TEST_BALANCE_POINTS=5):
            with self.assertRaises(CheckoutError):self.authorize()
        self.assertEqual(CybertestCheckout.objects.get(pk=self.id).status,'quoted')

    def test_expired_or_repriced_confirmation_is_rejected(self):
        with self.assertRaises(CheckoutError):checkout.confirm(self.id,self.token,self.user,expected_points=11)
        CybertestCheckout.objects.filter(pk=self.id).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(CheckoutError):self.authorize()

    def test_startup_refund_is_exact_and_idempotent(self):
        self.authorize();checkout.charge(self.id,self.audit_id,self.intent)
        checkout.refund_start_failure(self.id,self.audit_id,'PREPARATION_FAILED')
        checkout.refund_start_failure(self.id,self.audit_id,'PREPARATION_FAILED')
        self.assertEqual(checkout.balance(self.user,test_mode=True),20)
        self.assertEqual(Ledger.objects.count(),0)
        with self.assertRaises(CheckoutError):checkout.charge(self.id,self.audit_id,self.intent)

    def test_dispatch_is_one_shot_and_cannot_be_auto_refunded(self):
        self.authorize();checkout.charge(self.id,self.audit_id,self.intent)
        self.assertTrue(checkout.start_dispatch(self.id,self.audit_id)['dispatch_allowed'])
        self.assertFalse(checkout.start_dispatch(self.id,self.audit_id)['dispatch_allowed'])
        with self.assertRaises(CheckoutError):checkout.refund_start_failure(self.id,self.audit_id,'PREPARATION_FAILED')

    def test_wrong_audit_id_cannot_refund_or_dispatch(self):
        self.authorize();checkout.charge(self.id,self.audit_id,self.intent)
        for operation in [lambda:checkout.start_dispatch(self.id,uuid.uuid4()),
                          lambda:checkout.refund_start_failure(self.id,uuid.uuid4(),'PREPARATION_FAILED')]:
            with self.assertRaises(CheckoutError):operation()

    def test_invalid_handoff_and_disabled_billing_fail_closed(self):
        with self.assertRaises(CheckoutError):checkout.confirm(self.id,'x'*43,self.user,expected_points=10)
        with override_settings(CYBERTEST_BILLING_MODE='disabled'):
            with self.assertRaises(CheckoutError):self.authorize()

    def test_deactivated_payer_cannot_dispatch_after_charge(self):
        self.authorize();checkout.charge(self.id,self.audit_id,self.intent)
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        with self.assertRaises(CheckoutError):checkout.start_dispatch(self.id,self.audit_id)
        self.assertEqual(CybertestCheckout.objects.get(pk=self.id).status,'charged')

    def test_recovery_receipts_and_refund_survive_disabling_new_billing(self):
        self.authorize();checkout.charge(self.id,self.audit_id,self.intent)
        with override_settings(CYBERTEST_BILLING_MODE='disabled'):
            pending=checkout.pending_refunds()
            self.assertEqual([row['checkout_id'] for row in pending['receipts']],[str(self.id)])
            checkout.refund_start_failure(self.id,self.audit_id,'QUEUE_START_FAILED')
            self.assertEqual(checkout.pending_refunds()['receipts'],[])


@override_settings(CYBERTEST_BILLING_MODE='live', CYBERTEST_LIVE_BILLING_ENABLED=True,
                   CYBERTEST_AUDIT_POINTS=10)
class SyntheticLedgerTests(TestCase):
    """Exercise the actual shared service on fabricated local accounts only."""

    def test_shared_service_debits_once_and_refunds_original_buckets_once(self):
        user=User.objects.create_user(email='synthetic-ledger@example.invalid')
        account=PointsAccount.objects.create(user=user, microroo_initialized=True,
            digital_balance_microroo=3_000_000, balance_microroo=12_000_000,
            purchased_topup_balance_microroo=4_000_000, earned_balance_microroo=8_000_000)
        intent={'github_actor_id':'123','repository_id':'456',
            'workspace_id':str(uuid.uuid4()),'repository':'fixture/local-only',
            'scope':['get-client-referrals']}
        quote_id=uuid.uuid4();audit_id=uuid.uuid4();token=secrets.token_urlsafe(32)
        checkout.create_quote(quote_id,token,intent)
        checkout.confirm(quote_id,token,user,expected_points=10)
        checkout.charge(quote_id,audit_id,intent)
        checkout.charge(quote_id,audit_id,intent)
        spend=Ledger.objects.get(user=user,kind='SPEND')
        self.assertEqual(spend.digital_delta_microroo,-3_000_000)
        self.assertEqual(spend.purchased_delta_microroo,-4_000_000)
        self.assertEqual(checkout.balance(user,test_mode=False),5)
        checkout.refund_start_failure(quote_id,audit_id,'QUEUE_START_FAILED')
        checkout.refund_start_failure(quote_id,audit_id,'QUEUE_START_FAILED')
        refund=Ledger.objects.get(user=user,kind='REFUND')
        self.assertEqual(refund.refund_of_id,spend.pk)
        account.refresh_from_db()
        self.assertEqual(account.digital_balance_microroo,3_000_000)
        self.assertEqual(account.purchased_topup_balance_microroo,4_000_000)
        self.assertEqual(account.earned_balance_microroo,8_000_000)

    @override_settings(CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
    def test_model_only_settlement_uses_shared_ledger_once(self):
        user, account, quote_id, audit_id = self._started_settlement_fixture()
        costs = {'auditId': str(audit_id), 'status': 'complete',
                 'modelCostUsd': 2.5, 'pendingCalls': 0, 'unpricedCalls': 0}
        result = checkout.settle_failed_review(quote_id, audit_id, 'EVIDENCE_REVIEW_FAILED', costs)
        repeated = checkout.settle_failed_review(quote_id, audit_id, 'EVIDENCE_REVIEW_FAILED', costs)
        self.assertEqual(result, repeated)
        self.assertEqual(result['settlement']['charge_microroo'], 2_500_000)
        self.assertEqual(result['settlement']['refund_microroo'], 7_500_000)
        self.assertEqual(Ledger.objects.filter(user=user, kind='REFUND').count(), 1)
        self.assertEqual(Ledger.objects.filter(user=user, kind='SPEND').count(), 2)
        account.refresh_from_db()
        self.assertEqual(account.digital_balance_microroo, 500_000)
        self.assertEqual(account.purchased_topup_balance_microroo, 4_000_000)
        self.assertEqual(account.earned_balance_microroo, 8_000_000)

    @override_settings(CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
    def test_retained_charge_failure_rolls_back_refund_and_receipt(self):
        user, account, quote_id, audit_id = self._started_settlement_fixture()
        costs = {'auditId': str(audit_id), 'status': 'complete',
                 'modelCostUsd': 2.5, 'pendingCalls': 0, 'unpricedCalls': 0}
        account.refresh_from_db()
        original = (account.digital_balance_microroo,
                    account.purchased_topup_balance_microroo, account.earned_balance_microroo)
        with patch.object(checkout.PointsService, 'spend_microroo', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                checkout.settle_failed_review(quote_id, audit_id, 'EVIDENCE_REVIEW_FAILED', costs)
        account.refresh_from_db()
        self.assertEqual(original, (account.digital_balance_microroo,
                         account.purchased_topup_balance_microroo, account.earned_balance_microroo))
        self.assertEqual(Ledger.objects.filter(user=user).count(), 1)
        row = CybertestCheckout.objects.get(pk=quote_id)
        self.assertIsNone(row.settlement)
        self.assertIsNone(row.settled_at)
        self.assertIsNone(row.refund_ledger_id)
        checkout.settle_failed_review(quote_id, audit_id, 'EVIDENCE_REVIEW_FAILED', costs)
        self.assertEqual(Ledger.objects.filter(user=user, kind='REFUND').count(), 1)

    def _started_settlement_fixture(self):
        user = User.objects.create_user(email='settlement-ledger@example.invalid')
        account = PointsAccount.objects.create(user=user, microroo_initialized=True,
            digital_balance_microroo=3_000_000, balance_microroo=12_000_000,
            purchased_topup_balance_microroo=4_000_000, earned_balance_microroo=8_000_000)
        intent = {'github_actor_id': '123', 'repository_id': '456',
                  'workspace_id': str(uuid.uuid4()), 'repository': 'fixture/local-only',
                  'scope': ['get-client-referrals']}
        quote_id, audit_id, token = uuid.uuid4(), uuid.uuid4(), secrets.token_urlsafe(32)
        checkout.create_quote(quote_id, token, intent)
        checkout.confirm(quote_id, token, user, expected_points=10)
        checkout.charge(quote_id, audit_id, intent)
        checkout.start_dispatch(quote_id, audit_id)
        return user, account, quote_id, audit_id

    def test_quote_uses_available_balance_after_existing_reservations(self):
        user=User.objects.create_user(email='reserved@example.invalid')
        PointsAccount.objects.create(user=user,microroo_initialized=True,
            digital_balance_microroo=12_000_000)
        with patch.object(checkout.PointsService,'_reserved_microroo',return_value=5_000_000):
            self.assertEqual(checkout.balance(user,test_mode=False),7)


@override_settings(CYBERTEST_BILLING_MODE='test', CYBERTEST_AUDIT_POINTS=10,
    CYBERTEST_TEST_BALANCE_POINTS=20, CYBERTEST_BILLING_API_KEY='synthetic-service-key-for-tests-only',
    COMMUNITY_CHAT_FRONTEND_URL='https://chat.mlai.au',
    COMMUNITY_CHAT_ALLOWED_ORIGINS=['https://chat.mlai.au'],
    COMMUNITY_CHAT_SIGNUP_ENABLED=False, CYBERTEST_APP_ORIGIN='https://audit.example.invalid',
    ALLOWED_HOSTS=['testserver'], DEBUG=False)
class CheckoutHttpTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        from rest_framework.test import APIClient
        from community_chat.tests.test_account_profiles import credentials_for
        from community_chat.account_cookies import ACCESS_COOKIE
        cache.clear()
        self.client=APIClient()
        self.user=User.objects.create_user(email='http-payer@example.invalid')
        self.session=credentials_for(self.user,web=True)
        self.access_cookie=ACCESS_COOKIE
        self.intent={'github_actor_id':'123','repository_id':'456',
            'workspace_id':str(uuid.uuid4()),'repository':'fixture/local-only',
            'scope':['get-client-referrals']}
        self.id=uuid.uuid4();self.token=secrets.token_urlsafe(32)
        checkout.create_quote(self.id,self.token,self.intent)

    def capture(self,origin='https://chat.mlai.au'):
        return self.client.post('/api/v1/my-startup/cybertest/capture/',
            {'handoff':f'{self.id}.{self.token}'},format='json',HTTP_ORIGIN=origin)

    def test_service_key_does_not_authorize_customer_consent(self):
        self.client.credentials(HTTP_X_CYBERTEST_KEY='synthetic-service-key-for-tests-only')
        response=self.client.get(f'/api/v1/points/cybertest/quotes/{self.id}/')
        self.assertEqual(response.status_code,200)
        self.assertNotIn(self.token,response.content.decode())
        response=self.client.get('/api/v1/my-startup/cybertest/preview/')
        self.assertEqual(response.status_code,401)

    def test_handoff_requires_trusted_origin_and_cookie_is_private(self):
        self.assertEqual(self.capture('https://untrusted.example.invalid').status_code,403)
        response=self.capture()
        self.assertEqual(response.status_code,200)
        cookie=response.cookies['cybertest_checkout']
        self.assertTrue(cookie['httponly']);self.assertTrue(cookie['secure'])
        self.assertEqual(cookie['path'],'/api/v1/my-startup/cybertest/')
        self.assertIn('no-store',response['Cache-Control'])

    def test_browser_consent_reuses_existing_session_origin_checks(self):
        self.capture()
        self.client.cookies[self.access_cookie]=self.session.access_token
        url='/api/v1/my-startup/cybertest/confirm/'
        for origin in ('','https://untrusted.example.invalid'):
            response=self.client.post(url,{'expected_points':10},format='json',HTTP_ORIGIN=origin)
            self.assertEqual(response.status_code,401)
        consent={'expected_points':10,'checkout_id':str(self.id),
            'binding_sha256':checkout.status(self.id)['binding_sha256']}
        forged=self.client.post(url,{**consent,'user_id':self.user.pk},
            format='json',HTTP_ORIGIN='https://chat.mlai.au')
        self.assertEqual(forged.status_code,400)
        response=self.client.post(url,consent,format='json',HTTP_ORIGIN='https://chat.mlai.au')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['status'],'authorized')
        self.assertEqual(response.json()['return_url'],f'https://audit.example.invalid/?billing_return={self.id}')
        self.assertEqual(Ledger.objects.count(),0)

    def test_second_checkout_tab_cannot_change_displayed_consent(self):
        self.capture()
        self.client.cookies[self.access_cookie]=self.session.access_token
        displayed={'expected_points':10,'checkout_id':str(self.id),
            'binding_sha256':checkout.status(self.id)['binding_sha256']}
        other_id=uuid.uuid4();other_token=secrets.token_urlsafe(32)
        checkout.create_quote(other_id,other_token,{**self.intent,'scope':['other-function']})
        self.client.post('/api/v1/my-startup/cybertest/capture/',
            {'handoff':f'{other_id}.{other_token}'},format='json',HTTP_ORIGIN='https://chat.mlai.au')
        response=self.client.post('/api/v1/my-startup/cybertest/confirm/',displayed,
            format='json',HTTP_ORIGIN='https://chat.mlai.au')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['code'],'STALE_CHECKOUT')
        self.assertFalse(CybertestCheckout.objects.exclude(status='quoted').exists())

    def test_mode_switch_invalidates_preview_before_reading_balance(self):
        self.capture()
        self.client.cookies[self.access_cookie]=self.session.access_token
        with override_settings(CYBERTEST_BILLING_MODE='live',CYBERTEST_LIVE_BILLING_ENABLED=True):
            response=self.client.get('/api/v1/my-startup/cybertest/preview/')
            self.assertEqual(response.status_code,409)
            self.assertEqual(response.json()['code'],'QUOTE_MODE_CHANGED')

    def test_customer_session_cannot_call_private_charge_endpoint(self):
        self.client.cookies[self.access_cookie]=self.session.access_token
        response=self.client.post(f'/api/v1/points/cybertest/quotes/{self.id}/charge/',
            {'audit_id':str(uuid.uuid4()),'binding':self.intent},format='json',HTTP_ORIGIN='https://chat.mlai.au')
        self.assertEqual(response.status_code,403)

@override_settings(CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
class FailedReviewSettlementTests(CheckoutAcceptanceTests):
    """Requires separate migration-execution approval before running."""
    def usage(self, cost=2):
        return {'auditId':str(self.audit_id),'status':'complete','modelCostUsd':cost,'pendingCalls':0,'unpricedCalls':0}

    def started(self):
        self.authorize(); checkout.charge(self.id,self.audit_id,self.intent)
        checkout.start_dispatch(self.id,self.audit_id)

    def test_failed_review_only_retains_model_cost_once(self):
        self.started()
        first=checkout.settle_failed_review(self.id,self.audit_id,'EVIDENCE_REVIEW_FAILED',self.usage())
        self.assertEqual(first['settlement']['charge_microroo'],2_000_000)
        self.assertEqual(first['settlement']['refund_microroo'],8_000_000)
        self.assertEqual(checkout.balance(self.user,test_mode=True),18)
        self.assertEqual(first,checkout.settle_failed_review(self.id,self.audit_id,'EVIDENCE_REVIEW_FAILED',self.usage()))
        with self.assertRaises(CheckoutError):checkout.settle_failed_review(self.id,self.audit_id,'EVIDENCE_REVIEW_FAILED',self.usage(3))

    def test_wrong_audit_or_unknown_usage_cannot_settle(self):
        self.started()
        for audit,costs in [(uuid.uuid4(),self.usage()),(self.audit_id,{**self.usage(),'status':'incomplete'}),(self.audit_id,{**self.usage(),'auditId':str(uuid.uuid4())})]:
            with self.assertRaises(CheckoutError):checkout.settle_failed_review(self.id,audit,'EVIDENCE_REVIEW_FAILED',costs)
        self.assertIsNone(CybertestCheckout.objects.get(pk=self.id).settlement)
