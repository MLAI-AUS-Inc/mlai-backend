from django.conf import settings
from django.test import SimpleTestCase


OPERATIONS_ORIGIN = 'https://ops.mlai.au'
PLANE_ORIGIN = 'https://admin.mlai.au'


class OperationsOriginSettingsTests(SimpleTestCase):
    def test_only_ops_origin_is_credentialed_and_csrf_trusted(self):
        self.assertTrue(settings.CORS_ALLOW_CREDENTIALS)
        self.assertIn(OPERATIONS_ORIGIN, settings.CORS_ALLOWED_ORIGINS)
        self.assertIn(OPERATIONS_ORIGIN, settings.CSRF_TRUSTED_ORIGINS)
        self.assertNotIn(PLANE_ORIGIN, settings.CORS_ALLOWED_ORIGINS)
        self.assertNotIn(PLANE_ORIGIN, settings.CSRF_TRUSTED_ORIGINS)

    def test_cors_preflight_allows_exact_ops_origin(self):
        response = self.client.options(
            '/api/v1/auth/logout/',
            HTTP_ORIGIN=OPERATIONS_ORIGIN,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type',
        )

        self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), OPERATIONS_ORIGIN)
        self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')

    def test_cors_preflight_does_not_allow_lookalike_or_untrusted_origins(self):
        for origin in (
            PLANE_ORIGIN,
            'https://ops.mlai.au.attacker.example',
            'https://admin.mlai.au.attacker.example',
            'https://attacker.example',
            'http://ops.mlai.au',
        ):
            with self.subTest(origin=origin):
                response = self.client.options(
                    '/api/v1/auth/logout/',
                    HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                )

                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
