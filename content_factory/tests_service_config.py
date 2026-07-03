from django.test import TestCase, override_settings


@override_settings(ROO_API_KEY="service-test-key", PAGESPEED_API_KEY="ps-live-key")
class ContentFactoryServiceConfigTest(TestCase):
    url = "/api/content-factory/service/config"

    def test_rejects_missing_api_key(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (401, 403))

    def test_rejects_wrong_api_key(self):
        response = self.client.get(self.url, HTTP_X_API_KEY="wrong-key")
        self.assertIn(response.status_code, (401, 403))

    def test_returns_pagespeed_key_with_service_auth(self):
        response = self.client.get(self.url, HTTP_X_API_KEY="service-test-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagespeedApiKey"], "ps-live-key")

    @override_settings(PAGESPEED_API_KEY="")
    def test_returns_empty_string_when_key_unset(self):
        response = self.client.get(self.url, HTTP_X_API_KEY="service-test-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagespeedApiKey"], "")
