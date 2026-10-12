"""Real issuer/relying-party handlers against disjoint disposable databases."""
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
import uuid

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from rest_framework.test import APIClient
from community_chat.account_sessions import issue_account_session
from community_chat.federation_models import ChatFederationGrant,ChatFederationIdentity
from community_chat.models import CommunityChatAccountSession,CommunityChatDevice
from tests.federation_database_support import active_api_database


@override_settings(DATABASE_ROUTERS=['tests.federation_database_support.FederationTestRouter'],
    COMMUNITY_CHAT_HOST_ONLY_COOKIES=True,COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='synthetic-confidential-client-credential',
    COMMUNITY_CHAT_SIGNUP_ENABLED=False)
class FederationTwoDatabaseTests(TestCase):
    databases={'default','staging'}

    @contextmanager
    def api(self,staging=False):
        token=active_api_database.set('staging' if staging else 'default')
        values=dict(ROOT_URLCONF='mlai.urls_staging_chat' if staging else 'mlai.urls',
            COMMUNITY_CHAT_FRONTEND_URL='https://chat-staging.mlai.au' if staging else 'https://chat.mlai.au',
            COMMUNITY_CHAT_ALLOWED_ORIGINS=['https://chat-staging.mlai.au' if staging else 'https://chat.mlai.au'],
            COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=not staging,COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=staging,
            COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES=not staging,
            COMMUNITY_CHAT_ACCESS_COOKIE_NAME='__Host-mlai_staging_access' if staging else '__Host-mlai_chat_access',
            COMMUNITY_CHAT_REFRESH_COOKIE_NAME='__Host-mlai_staging_refresh' if staging else '__Host-mlai_chat_refresh')
        try:
            with override_settings(**values):yield
        finally:active_api_database.reset(token)

    def test_identity_grant_never_copies_production_session_or_devices(self):
        with self.api():
            user=get_user_model().objects.create_user(email='production-tester@example.test',first_name='Pilot',is_staff=True)
            subject=str(user.community_chat_profile_id)
            credentials=issue_account_session(user,SimpleNamespace(public_key='a'*64,
                installation_id=uuid.uuid4(),client_id='mlai-chat-web',origin='https://chat.mlai.au',platform='web',device_name='Production browser'))
        with override_settings(COMMUNITY_CHAT_FEDERATION_TESTER_IDS=[subject]):
            def transport(endpoint,payload):
                with self.api():
                    client=APIClient()
                    client.credentials(HTTP_AUTHORIZATION='Bearer synthetic-confidential-client-credential')
                    response=client.post('/api/v1/community-chat/federation/'+endpoint+'/',payload,format='json')
                    self.assertIn(response.status_code,[200,201])
                    return response.data
            browser=APIClient()
            browser.credentials(HTTP_ORIGIN='https://chat-staging.mlai.au')
            with self.api(True),patch('community_chat.federation_relying.issuer_post',side_effect=transport):
                key=PrivateKey.from_int(93).public_key.format(compressed=True)[1:].hex()
                started=browser.post('/api/v1/community-chat/federation/start/',dict(public_key=key,installation_id=str(uuid.uuid4()),platform='web'),format='json')
                self.assertEqual(started.status_code,201)
                request_id=parse_qs(urlsplit(started.data['authorization_url']).query)['request_id'][0]
            with self.api():
                production_browser=APIClient()
                production_browser.credentials(HTTP_ORIGIN='https://chat.mlai.au')
                production_browser.cookies['__Host-mlai_chat_access']=credentials.access_token
                approved=production_browser.post('/api/v1/community-chat/federation/authorize/',{'request_id':request_id},format='json')
                self.assertEqual(approved.status_code,200)
                query=parse_qs(urlsplit(approved.data['callback_url']).query)
            with self.api(True),patch('community_chat.federation_relying.issuer_post',side_effect=transport):
                callback=browser.get('/api/v1/community-chat/federation/callback/',{key:values[0] for key,values in query.items()})
                self.assertEqual(callback.status_code,302)
                local_user=get_user_model().objects.get()
                self.assertFalse(local_user.is_staff)
                self.assertFalse(local_user.has_usable_password())
                self.assertNotEqual(local_user.email,user.email)
                self.assertEqual(ChatFederationIdentity.objects.get().subject,subject)
                self.assertEqual(CommunityChatAccountSession.objects.count(),1)
                self.assertNotEqual(CommunityChatAccountSession.objects.get().access_token_hash,credentials.session.access_token_hash)
                self.assertEqual(browser.post('/api/v1/community-chat/auth/session/logout/',{},format='json').status_code,200)
            with self.api():
                self.assertEqual(get_user_model().objects.count(),1)
                self.assertFalse(CommunityChatDevice.objects.exists())
                self.assertEqual(ChatFederationGrant.objects.filter(consumed_at__isnull=False).count(),1)
                credentials.session.refresh_from_db()
                self.assertIsNone(credentials.session.revoked_at)
