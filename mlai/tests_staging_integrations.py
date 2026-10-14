"""Unavailable providers never dispatch or return a false success."""
from unittest.mock import patch
from django.test import SimpleTestCase,override_settings
from rest_framework.test import APIClient

@override_settings(SETTINGS_MODULE='mlai.settings_staging_chat',APP_ENV='chat-staging',ROOT_URLCONF='mlai.urls_staging_chat',STAGING_CAPABILITIES={})
class IntegrationTests(SimpleTestCase):
    def test_disabled_capability_is_explicit(self):
        from mlai.staging_integrations import require_staging_capability,StagingIntegrationUnavailable
        with self.assertRaises(StagingIntegrationUnavailable) as raised:require_staging_capability('email')
        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(raised.exception.detail['code'],'staging_integration_unavailable')
    def test_auth_creation_and_callback_fail_before_provider(self):
        from mlai.staging_integrations import IntegrationMiddleware
        from django.test import RequestFactory
        from core.views import SendMagicLinkView,CreateUserView
        from core.auth_views import PasswordResetRequestView
        middleware=IntegrationMiddleware(lambda r:None)
        for view in (SendMagicLinkView,CreateUserView,PasswordResetRequestView):
            response=middleware.process_view(RequestFactory().post('/api/'),view.as_view(),(),{})
            self.assertEqual(response.status_code,503)
    def test_email_gate_precedes_customerio(self):
        from core.email_utils import send_magic_link_email_to_address
        from mlai.staging_integrations import StagingIntegrationUnavailable
        with patch('core.email_utils.APIClient') as provider,self.assertRaises(StagingIntegrationUnavailable):
            send_magic_link_email_to_address('synthetic@staging.mlai.au','private',identifier=1)
        provider.assert_not_called()
    def test_production_boundary_unchanged(self):
        from mlai.staging_integrations import require_staging_capability
        with override_settings(SETTINGS_MODULE='mlai.settings',APP_ENV='production'):require_staging_capability('email')
    def test_tokenmaxer_is_unavailable_before_any_provider_or_cache_success(self):
        from mlai.staging_integrations import StagingIntegrationUnavailable
        from community_chat.tokenmaxer_federation import fetch_public_tokenmaxer_entries
        with patch('community_chat.tokenmaxer_federation.requests.get') as provider,self.assertRaises(StagingIntegrationUnavailable):fetch_public_tokenmaxer_entries('week')
        provider.assert_not_called()
    def test_digital_wallet_read_allowed_but_store_delivery_disabled(self):
        from mlai.staging_integrations import IntegrationMiddleware
        from django.test import RequestFactory
        from community_chat.apple_iap_views import AppleIapView,AppleIapTransactionView
        middleware=IntegrationMiddleware(lambda r:None);factory=RequestFactory()
        self.assertIsNone(middleware.process_view(factory.get('/api/'),AppleIapView.as_view(),(),{}))
        self.assertEqual(middleware.process_view(factory.post('/api/'),AppleIapTransactionView.as_view(),(),{}).status_code,503)
    def test_clean_profiles_pin_reporter_and_relay_and_disable_federation(self):
        import os,sys,subprocess,secrets,json
        base={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG'}}
        base.update(SECRET_KEY=secrets.token_hex(48),REDIS_URL='redis://127.0.0.1:65451/0',DEBUG='false')
        for profile,environment,database,api,relay in [('mlai.settings_staging_chat','chat-staging','staging_account_api','https://api-staging.mlai.au','wss://chat-staging.mlai.au'),('mlai.settings_staging_local','staging-local','local_mlai','http://127.0.0.1:8000','ws://127.0.0.1:3000')]:
            env={**base,'DJANGO_SETTINGS_MODULE':profile,'APP_ENV':environment,'DATABASE_URL':f'postgresql://{database}:unused@127.0.0.1:65450/{database}'}
            code="import importlib,json; s=importlib.import_module('"+profile+"'); print(json.dumps({'api':s.TOKEN_USAGE_API_BASE,'relay':s.COMMUNITY_CHAT_RELAY_URL,'audience':s.COMMUNITY_CHAT_API_AUDIENCE,'tokenmaxer':s.TOKENMAXER_FEDERATION_ENABLED,'apple':s.APPLE_IAP_ENABLED}))"
            result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr[-500:]);values=json.loads(result.stdout)
            self.assertEqual(values['api'],api+'/api/v1/community-chat/usage');self.assertEqual(values['relay'],relay)
            self.assertEqual(values['audience'],api)
            self.assertFalse(values['tokenmaxer']);self.assertFalse(values['apple'])
