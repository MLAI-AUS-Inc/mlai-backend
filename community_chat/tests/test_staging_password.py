"""Isolated password pilot contract; no production identity or adapter calls."""
import uuid
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from coincurve import PrivateKey
from community_chat.models import CommunityChatAccountSession, CommunityChatBootstrapToken, CommunityChatDevice
from community_chat.tests.test_api import sign_challenge
from community_chat.adapter import IssuedInvite, RelayMembership
from django.utils import timezone
from datetime import timedelta

ROOT='/api/v1/community-chat/'
STAGE='https://chat-staging.mlai.au'
NATIVE='mlaichat://callback'
PILOT=dict(ROOT_URLCONF='mlai.urls_staging_chat',
    SETTINGS_MODULE='mlai.settings_staging_chat',APP_ENV='chat-staging',
    COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED=True, COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=True,
    COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=False, COMMUNITY_CHAT_SIGNUP_ENABLED=False,
    COMMUNITY_CHAT_FRONTEND_URL=STAGE, COMMUNITY_CHAT_ALLOWED_ORIGINS=[STAGE,NATIVE],
    COMMUNITY_CHAT_RELAY_URL='wss://chat-staging.mlai.au',
    COMMUNITY_CHAT_API_AUDIENCE='https://api-staging.mlai.au',
    COMMUNITY_CHAT_HOST_ONLY_COOKIES=True, COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES=False,
    COMMUNITY_CHAT_ACCESS_COOKIE_NAME='__Host-mlai_staging_access',
    COMMUNITY_CHAT_REFRESH_COOKIE_NAME='__Host-mlai_staging_refresh')

@override_settings(**PILOT)
class StagingPasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user=get_user_model().objects.create_user(email='pilot@staging.mlai.au',password='Synthetic-private-pilot-472!')
        self.private=PrivateKey.from_int(117)
        self.key=self.private.public_key.format(compressed=True)[1:].hex()
        self.installation=str(uuid.uuid4())
        self.client=APIClient()

    def login(self,platform='web',origin=None,**changes):
        payload=dict(email=self.user.email,password='Synthetic-private-pilot-472!',client_id='mlai-chat-'+platform,
            device=dict(public_key=self.key,installation_id=self.installation,platform=platform,name='Pilot'))
        payload.update(changes)
        return self.client.post(ROOT+'auth/password/',payload,format='json',HTTP_ORIGIN=origin or (STAGE if platform=='web' else NATIVE))

    def test_web_and_native_credentials_are_separate(self):
        for platform in ('web','ios','android'):
            with self.subTest(platform=platform):
                self.client.cookies.clear()
                response=self.login(platform)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.data['relay_url'],'wss://chat-staging.mlai.au')
                self.assertTrue(response.data['bootstrap_token'].startswith('mlai_chat_'))
                if platform=='web':
                    self.assertNotIn('access_token',response.data['session'])
                    self.assertEqual(set(response.cookies),{'__Host-mlai_staging_access','__Host-mlai_staging_refresh'})
                    for cookie in response.cookies.values():
                        self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly']);self.assertFalse(cookie['domain'])
                else:
                    self.assertTrue(response.data['session']['access_token'].startswith('mlai_session_access_'))
                    self.assertFalse(response.cookies)

    @override_settings(COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED=False)
    def test_default_off_and_production_routes_absent(self):
        self.assertEqual(self.login().status_code,404)
        for path in ('auth/email-code/request/','account/signup/','account/onboarding/','slack/','account/deletion/'):
            self.assertEqual(self.client.post(ROOT+path,{},format='json').status_code,404)
        self.assertEqual(self.client.get('/api/v1/auth/me/').status_code,404)

    def test_invalid_disabled_and_unknown_accounts_denied(self):
        self.assertEqual(self.login(password='bad').status_code,401)
        self.assertEqual(self.login(email='missing@staging.mlai.au').status_code,401)
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        self.assertEqual(self.login().status_code,401)
        self.assertFalse(CommunityChatAccountSession.objects.exists())

    def test_hostile_origin_desktop_and_mixed_credentials_denied(self):
        self.assertEqual(self.login(origin='https://chat.mlai.au').status_code,403)
        self.assertEqual(self.login('desktop').status_code,403)
        self.assertEqual(self.login('ios',origin=STAGE).status_code,403)
        self.assertEqual(self.client.post(ROOT+'federation/start/',{},format='json').status_code,404)

    def test_browser_refresh_rotation_stale_token_and_logout(self):
        self.assertEqual(self.login().status_code,200)
        stale=APIClient();stale.cookies=self.client.cookies.copy()
        response=self.client.post(ROOT+'auth/session/refresh/',{},format='json',HTTP_ORIGIN=STAGE)
        self.assertEqual(response.status_code,200)
        loser=stale.post(ROOT+'auth/session/refresh/',{},format='json',HTTP_ORIGIN=STAGE)
        self.assertEqual(loser.status_code,401);self.assertFalse(loser.cookies)
        self.assertEqual(self.client.get(ROOT+'account/',HTTP_ORIGIN=STAGE).status_code,200)
        self.assertEqual(self.client.post(ROOT+'auth/session/logout/',{},format='json',HTTP_ORIGIN=STAGE).status_code,200)
        self.assertEqual(self.client.get(ROOT+'account/',HTTP_ORIGIN=STAGE).status_code,401)

    def test_refresh_rejects_origin_and_browser_native_mix(self):
        response=self.login('ios');token=response.data['session']['refresh_token']
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',dict(refresh_token=token),format='json',HTTP_ORIGIN=STAGE).status_code,403)
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',dict(refresh_token=token),format='json',HTTP_ORIGIN=NATIVE).status_code,200)
        response=self.login();raw=self.client.cookies['__Host-mlai_staging_refresh'].value
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',dict(refresh_token=raw),format='json',HTTP_ORIGIN=NATIVE).status_code,403)
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',{},format='json',HTTP_ORIGIN='https://evil.example').status_code,403)

    def test_revoked_account_and_production_cookie_names_are_rejected(self):
        self.assertEqual(self.login().status_code,200)
        raw=self.client.cookies['__Host-mlai_staging_access'].value
        self.client.cookies.clear();self.client.cookies['__Host-mlai_chat_access']=raw
        self.assertEqual(self.client.get(ROOT+'account/',HTTP_ORIGIN=STAGE).status_code,401)
        self.assertEqual(self.login().status_code,200)
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(ROOT+'account/',HTTP_ORIGIN=STAGE).status_code,401)
        self.assertEqual(self.client.post(ROOT+'auth/session/refresh/',{},format='json',HTTP_ORIGIN=STAGE).status_code,401)

    def test_bootstrap_proof_is_key_bound_one_use_and_staging_only(self):
        response=self.login('android');token=response.data['bootstrap_token']
        client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+token,HTTP_ORIGIN=NATIVE)
        other=PrivateKey.from_int(118).public_key.format(compressed=True)[1:].hex()
        self.assertEqual(client.post(ROOT+'bootstrap/challenge/',dict(public_key=other),format='json').status_code,403)
        challenge=client.post(ROOT+'bootstrap/challenge/',dict(public_key=self.key),format='json')
        self.assertEqual(challenge.status_code,201)
        proof=sign_challenge(self.private,challenge.data)
        data=dict(challenge_id=challenge.data['challenge_id'],nonce=challenge.data['nonce'],event=proof)
        with patch('community_chat.views.issue_member_invite',return_value=IssuedInvite('invite','audit',timezone.now()+timedelta(minutes=5),uuid.uuid4())):
            self.assertEqual(client.post(ROOT+'bootstrap/invite/',data,format='json').status_code,200)
            self.assertEqual(client.post(ROOT+'bootstrap/invite/',data,format='json').status_code,409)
        with patch('community_chat.adapter.get_relay_membership',return_value=RelayMembership(True,'owner',timezone.now())):
            self.assertEqual(client.post(ROOT+'bootstrap/confirm/',dict(public_key=self.key),format='json').status_code,409)
        with patch('community_chat.adapter.get_relay_membership',return_value=RelayMembership(True,'member',timezone.now())):
            self.assertEqual(client.post(ROOT+'bootstrap/confirm/',dict(public_key=self.key),format='json').status_code,200)
        self.assertFalse(CommunityChatBootstrapToken.objects.filter(revoked_at__isnull=True).exists())
        account=APIClient();account.credentials(HTTP_AUTHORIZATION='Bearer '+response.data['session']['access_token'],HTTP_ORIGIN=NATIVE)
        with patch('community_chat.adapter.revoke_relay_membership',return_value=('revoked',uuid.uuid4())):
            self.assertEqual(account.delete(ROOT+'devices/'+self.key+'/',{},format='json').status_code,200)
        self.assertFalse(CommunityChatAccountSession.objects.filter(revoked_at__isnull=True).exists())

    def test_runtime_guard_rejects_both_modes_and_wrong_target(self):
        from community_chat.staging_password import require_password_pilot
        from django.core.exceptions import ImproperlyConfigured
        require_password_pilot()
        with override_settings(COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=True):
            with self.assertRaises(ImproperlyConfigured):require_password_pilot()
        with override_settings(COMMUNITY_CHAT_RELAY_URL='wss://chat.mlai.au'):
            with self.assertRaises(ImproperlyConfigured):require_password_pilot()
        with override_settings(SETTINGS_MODULE='mlai.settings'):
            with self.assertRaises(ImproperlyConfigured):require_password_pilot()

    def test_all_restricted_account_routes_bind_native_origin(self):
        response=self.login('ios')
        client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+response.data['session']['access_token'],HTTP_ORIGIN='https://chat.mlai.au')
        for path in ('account/','session/','permissions/'):
            self.assertEqual(client.get(ROOT+path).status_code,401)
        self.assertEqual(client.post(ROOT+'profiles/batch/',{'public_keys':[self.key]},format='json').status_code,401)
