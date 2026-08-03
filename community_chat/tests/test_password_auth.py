import hashlib
import uuid
from unittest.mock import patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.adapter import RelayMembership
from community_chat.models import CommunityChatBootstrapToken, CommunityChatDevice


ORIGIN = 'https://chat.mlai.au'


def public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, 'mlaichat://callback'],
    COMMUNITY_CHAT_FRONTEND_URL=ORIGIN,
    COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=True,
    COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS=300,
)
class CommunityChatPasswordAuthTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.password = 'Existing-secure-password-42!'
        self.user = get_user_model().objects.create_user(
            email='member@example.com',
            password=self.password,
            first_name='MLAI',
            last_name='Member',
            about='Community member',
        )
        self.public_key = public_key(31)
        self.installation_id = uuid.uuid4()

    def login(self, **overrides):
        request_origin = overrides.pop('request_origin', ORIGIN)
        payload = {
            'email': self.user.email,
            'password': self.password,
            'client_id': 'mlai-chat-web',
            'device': {
                'installation_id': str(self.installation_id),
                'public_key': self.public_key,
                'platform': 'web',
                'name': 'Chrome',
            },
        }
        payload.update(overrides)
        request_kwargs = {'HTTP_ORIGIN': request_origin} if request_origin is not None else {}
        return self.client.post(
            reverse('community_chat_password_auth'),
            payload,
            format='json',
            **request_kwargs,
        )

    @override_settings(COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=False)
    def test_password_auth_is_disabled_for_email_code_only_launches(self):
        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {'error': 'password_auth_disabled'})

    def test_success_issues_only_scoped_bootstrap_and_safe_own_profile(self):
        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'authenticated')
        raw_token = response.data['bootstrap_token']
        self.assertTrue(raw_token.startswith('mlai_chat_'))
        self.assertNotIn('refresh', response.data)
        self.assertNotIn('access_token', response.data)
        token = CommunityChatBootstrapToken.objects.get()
        self.assertEqual(token.public_key, self.public_key)
        self.assertEqual(token.installation_id, self.installation_id)
        self.assertEqual(token.client_id, 'mlai-chat-web')
        self.assertEqual(token.origin, ORIGIN)
        self.assertEqual(token.platform, 'web')
        self.assertEqual(token.name, 'Chrome')
        self.assertEqual(
            token.token_hash,
            hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        )
        self.assertNotEqual(token.token_hash, raw_token)
        self.assertEqual(response.data['profile']['email'], self.user.email)
        self.assertEqual(response.data['profile']['display_name'], 'MLAI Member')
        self.assertNotIn('phone', response.data['profile'])
        self.assertNotIn('slack_id', response.data['profile'])

    def test_all_account_failures_share_one_response(self):
        cases = [
            self.login(password='wrong-password'),
            self.login(email='unknown@example.com'),
        ]
        passwordless = get_user_model().objects.create_user(email='passwordless@example.com')
        cases.append(self.login(email=passwordless.email))
        inactive = get_user_model().objects.create_user(
            email='inactive@example.com',
            password=self.password,
        )
        inactive.is_active = False
        inactive.save(update_fields=('is_active',))
        cases.append(self.login(email=inactive.email))

        for response in cases:
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(response.data, {'error': 'invalid_credentials'})
        self.assertFalse(CommunityChatBootstrapToken.objects.exists())

    def test_client_platform_and_origin_are_strict(self):
        mismatched = self.login(
            client_id='mlai-chat-ios',
            device={
                'installation_id': str(self.installation_id),
                'public_key': self.public_key,
                'platform': 'web',
            },
        )
        self.assertEqual(mismatched.status_code, status.HTTP_400_BAD_REQUEST)

        rejected = self.client.post(
            reverse('community_chat_password_auth'),
            {
                'email': self.user.email,
                'password': self.password,
                'client_id': 'mlai-chat-web',
                'device': {
                    'installation_id': str(self.installation_id),
                    'public_key': self.public_key,
                    'platform': 'web',
                },
            },
            format='json',
            HTTP_ORIGIN='https://attacker.example',
        )
        self.assertEqual(rejected.status_code, status.HTTP_403_FORBIDDEN)

    def test_mobile_login_uses_registered_callback_origin_without_header(self):
        response = self.login(
            request_origin=None,
            client_id='mlai-chat-ios',
            device={
                'installation_id': str(self.installation_id),
                'public_key': self.public_key,
                'platform': 'ios',
                'name': 'iPhone',
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['origin'], 'mlaichat://callback')

    def test_token_context_is_copied_to_challenge_and_session_profiles_are_separated(self):
        login = self.login()
        token = login.data['bootstrap_token']
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        challenge = client.post(
            reverse('community_chat_challenge'),
            {'public_key': self.public_key},
            format='json',
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(challenge.status_code, status.HTTP_201_CREATED)
        row = self.user.community_chat_challenges.get(id=challenge.data['challenge_id'])
        self.assertEqual(row.installation_id, self.installation_id)
        self.assertEqual(row.client_id, 'mlai-chat-web')

        session = client.get(reverse('community_chat_session'))
        self.assertEqual(session.status_code, status.HTTP_200_OK)
        self.assertEqual(session.data['profile']['email'], self.user.email)
        self.assertNotIn('email', session.data['public_profile'])
        self.assertNotIn('phone', session.data['public_profile'])
        self.assertNotIn('slack_id', session.data['public_profile'])

    @patch('community_chat.views.get_relay_membership')
    def test_confirm_consumes_bootstrap_token(self, mock_membership):
        login = self.login()
        raw_token = login.data['bootstrap_token']
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            installation_id=self.installation_id,
            client_id='mlai-chat-web',
            platform='web',
            name='Chrome',
        )
        mock_membership.return_value = RelayMembership(True, 'member', timezone.now())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw_token}')

        response = client.post(
            reverse('community_chat_confirm'),
            {'public_key': self.public_key},
            format='json',
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('email', response.data['public_profile'])
        token = CommunityChatBootstrapToken.objects.get()
        self.assertIsNotNone(token.revoked_at)
        replay = client.get(reverse('community_chat_session'))
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('community_chat.views.revoke_relay_membership')
    def test_password_reauthentication_can_revoke_only_its_bound_device(self, mock_revoke):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            installation_id=self.installation_id,
            client_id='mlai-chat-web',
            platform='web',
            status='verified',
            verified_at=timezone.now(),
        )
        login = self.login()
        raw_token = login.data['bootstrap_token']
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw_token}')
        mock_revoke.return_value = ('revoked', uuid.uuid4())

        response = client.delete(
            reverse('community_chat_device', args=(self.public_key,)),
            {'reason': 'removed_by_user'},
            format='json',
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.assertEqual(device.status, 'revoked')
        self.assertEqual(device.revocation_reason, 'removed_by_user')
        token = CommunityChatBootstrapToken.objects.get(
            token_hash=hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        )
        self.assertIsNotNone(token.revoked_at)

        replay = client.get(reverse('community_chat_session'))
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('community_chat.views.revoke_relay_membership')
    def test_password_reauthentication_cannot_revoke_another_device(self, mock_revoke):
        other_key = public_key(32)
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=other_key,
            installation_id=uuid.uuid4(),
            status='verified',
            verified_at=timezone.now(),
        )
        login = self.login()
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['bootstrap_token']}"
        )

        response = client.delete(
            reverse('community_chat_device', args=(other_key,)),
            {'reason': 'removed_by_user'},
            format='json',
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_revoke.assert_not_called()
