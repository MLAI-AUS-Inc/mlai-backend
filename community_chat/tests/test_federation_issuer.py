import base64
import hashlib
from datetime import timedelta
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatDevice
from types import SimpleNamespace

ROOT = '/api/v1/community-chat/federation/'
CALLBACK = 'https://api-staging.mlai.au/api/v1/community-chat/federation/callback/'
VERIFIER = 'a' * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@override_settings(COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=True,
                   COMMUNITY_CHAT_HOST_ONLY_COOKIES=True,
                   COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='synthetic-confidential-client-credential',
                   COMMUNITY_CHAT_FEDERATION_CALLBACK_URL=CALLBACK,
                   COMMUNITY_CHAT_FEDERATION_APPROVAL_URL='https://chat.mlai.au/auth/staging',
                   COMMUNITY_CHAT_FEDERATION_ISSUER='https://api.mlai.au',
                   COMMUNITY_CHAT_FRONTEND_URL='https://chat.mlai.au',
                   COMMUNITY_CHAT_ALLOWED_ORIGINS=['https://chat.mlai.au'],
                   COMMUNITY_CHAT_SIGNUP_ENABLED=False)
class FederationIssuerTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(email='federation@example.test', first_name='Tester')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer synthetic-confidential-client-credential')
        self.tester_setting = override_settings(COMMUNITY_CHAT_FEDERATION_TESTER_IDS=[str(self.user.community_chat_profile_id)])
        self.tester_setting.enable()
        self.addCleanup(self.tester_setting.disable)
        credentials = issue_account_session(self.user, SimpleNamespace(public_key='a'*64,
            installation_id=uuid.uuid4(), client_id='mlai-chat-web', origin='https://chat.mlai.au',
            platform='web', device_name='Browser'))
        self.browser = APIClient()
        self.browser.cookies['__Host-mlai_chat_access'] = credentials.access_token
        self.browser.credentials(HTTP_ORIGIN='https://chat.mlai.au')

    def register(self, **changes):
        data = dict(client_id='mlai-chat-staging', callback_url=CALLBACK, state='s'*48,
                    code_challenge=CHALLENGE, code_challenge_method='S256')
        data.update(changes)
        return self.client.post(ROOT+'requests/', data, format='json')

    def authorize(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        response = self.browser.post(ROOT+'authorize/', {'request_id':response.data['request_id']}, format='json')
        self.assertEqual(response.status_code, 200)
        from urllib.parse import urlsplit, parse_qs
        return parse_qs(urlsplit(response.data['callback_url']).query)['code'][0]

    def exchange(self, code, **changes):
        data = dict(client_id='mlai-chat-staging', callback_url=CALLBACK, code=code, code_verifier=VERIFIER)
        data.update(changes)
        return self.client.post(ROOT+'token/', data, format='json')

    def test_one_use_exchange_returns_only_minimal_identity_and_no_device(self):
        code = self.authorize()
        response = self.exchange(code)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {'issuer','subject','display_name','eligible'})
        self.assertEqual(response.data['display_name'], 'Tester')
        self.assertTrue(response.data['eligible'])
        self.assertFalse(CommunityChatDevice.objects.exists())
        self.assertEqual(self.exchange(code).status_code, 400)
        eligibility = self.client.post(ROOT+'eligibility/', {'subject':response.data['subject']}, format='json')
        self.assertEqual(eligibility.status_code, 200)
        self.assertTrue(eligibility.data['eligible'])

    def test_wrong_verifier_does_not_consume_grant(self):
        code = self.authorize()
        self.assertEqual(self.exchange(code, code_verifier='b'*64).status_code, 400)
        self.assertEqual(self.exchange(code).status_code, 200)

    def test_wrong_callback_and_wrong_client_are_rejected(self):
        self.assertEqual(self.register(callback_url='https://evil.example/callback').status_code, 400)
        self.assertEqual(self.register(client_id='other-client').status_code, 400)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer incorrect')
        self.assertEqual(self.register().status_code, 401)

    @override_settings(COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=False)
    def test_disabled_feature_is_unavailable(self):
        self.assertEqual(self.register().status_code, 404)

    @override_settings(COMMUNITY_CHAT_HOST_ONLY_COOKIES=False)
    def test_issuer_cannot_enable_before_cookie_cutover(self):
        self.assertEqual(self.register().status_code, 503)

    @override_settings(COMMUNITY_CHAT_FEDERATION_TESTER_IDS=[])
    def test_non_tester_cannot_approve(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.browser.post(ROOT+'authorize/', {'request_id':response.data['request_id']}, format='json').status_code, 403)

    def test_account_removed_between_approval_and_exchange_is_rejected(self):
        code = self.authorize()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.exchange(code).status_code, 403)

    def test_expired_code_is_rejected(self):
        code = self.authorize()
        with __import__('unittest').mock.patch('community_chat.federation.timezone.now', return_value=timezone.now()+timedelta(seconds=61)):
            self.assertEqual(self.exchange(code).status_code, 400)

    def test_malformed_request_and_ungranted_subject_fail_closed(self):
        response = self.browser.post(ROOT+'authorize/', {'request_id':'not-a-uuid'}, format='json')
        self.assertEqual(response.status_code, 400)
        response = self.client.post(ROOT+'eligibility/', {'subject':'not-a-uuid'}, format='json')
        self.assertEqual(response.status_code, 403)
        response = self.client.post(ROOT+'eligibility/', {'subject':str(self.user.community_chat_profile_id)}, format='json')
        self.assertEqual(response.status_code, 403)


from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections, connection
from django.test import TransactionTestCase


@override_settings(COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=True,
                   COMMUNITY_CHAT_HOST_ONLY_COOKIES=True,
                   COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='synthetic-confidential-client-credential',
                   COMMUNITY_CHAT_FEDERATION_CALLBACK_URL=CALLBACK,
                   COMMUNITY_CHAT_SIGNUP_ENABLED=False)
class FederationRedemptionRaceTests(TransactionTestCase):
    def test_concurrent_redemptions_have_exactly_one_winner(self):
        if connection.vendor != 'postgresql':
            self.skipTest('This race is verified against the disposable local PostgreSQL instance.')
        from community_chat.federation_models import ChatFederationGrant
        user = get_user_model().objects.create_user(email='race@example.test')
        code = 'c'*64
        ChatFederationGrant.objects.create(client_id='mlai-chat-staging', callback_url=CALLBACK,
            state='s'*48, code_challenge=CHALLENGE, expires_at=timezone.now()+timedelta(minutes=5),
            user=user, code_hash=hashlib.sha256(code.encode()).hexdigest(), code_expires_at=timezone.now()+timedelta(seconds=60))
        def redeem(_):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION='Bearer synthetic-confidential-client-credential')
                return client.post(ROOT+'token/', dict(client_id='mlai-chat-staging', callback_url=CALLBACK,
                    code=code, code_verifier=VERIFIER), format='json').status_code
            finally:
                connection.close()
        with override_settings(COMMUNITY_CHAT_FEDERATION_TESTER_IDS=[str(user.community_chat_profile_id)]), ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(redeem, range(2))), [200,400])
