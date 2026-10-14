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
