"""Read-only receipt proving ordinary domain URL deployment without stage registries."""
from types import SimpleNamespace
from django.test import SimpleTestCase,override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory,force_authenticate
from roo.development import DevelopmentBuildView

class DevelopmentBuildTests(SimpleTestCase):
    def request(self,authenticated=True):
        request=APIRequestFactory().get('/api/v1/points/development/build/')
        if authenticated:force_authenticate(request,user=SimpleNamespace(is_authenticated=True))
        return DevelopmentBuildView.as_view()(request)
    @override_settings(APP_ENV='chat-staging',APP_RELEASE='a'*40)
    def test_normal_domain_route_returns_exact_active_staging_release(self):
        self.assertIs(resolve('/api/v1/points/development/build/',urlconf='mlai.urls').func.view_class,DevelopmentBuildView)
        result=self.request();self.assertEqual(result.status_code,200)
        self.assertEqual(result.data,{'environment':'chat-staging','release':'a'*40})
    @override_settings(APP_ENV='chat-staging')
    def test_anonymous_cannot_read_receipt(self):self.assertEqual(self.request(False).status_code,401)
    @override_settings(APP_ENV='production')
    def test_authenticated_production_receipt_unavailable(self):self.assertEqual(self.request().status_code,404)
