from django.conf import settings
from django.test import SimpleTestCase, override_settings


OPERATIONS_ORIGIN = 'https://ops.mlai.au'
PLANE_ORIGIN = 'https://admin.mlai.au'
TAURI_ORIGINS = ('tauri://localhost', 'http://tauri.localhost')


class OperationsOriginSettingsTests(SimpleTestCase):
    def test_only_ops_origin_is_credentialed_and_csrf_trusted(self):
        self.assertTrue(settings.CORS_ALLOW_CREDENTIALS)
        self.assertIn(OPERATIONS_ORIGIN, settings.CORS_ALLOWED_ORIGINS)
        self.assertIn(OPERATIONS_ORIGIN, settings.CSRF_TRUSTED_ORIGINS)
        self.assertNotIn(PLANE_ORIGIN, settings.CORS_ALLOWED_ORIGINS)
        self.assertNotIn(PLANE_ORIGIN, settings.CSRF_TRUSTED_ORIGINS)
        for origin in TAURI_ORIGINS:
            self.assertNotIn(origin, settings.CORS_ALLOWED_ORIGINS)
            self.assertNotIn(origin, settings.CSRF_TRUSTED_ORIGINS)

    def test_cors_preflight_allows_exact_ops_origin(self):
        response = self.client.options(
            '/api/v1/auth/logout/',
            HTTP_ORIGIN=OPERATIONS_ORIGIN,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type',
        )

        self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), OPERATIONS_ORIGIN)
        self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')

    def test_credential_free_cors_allows_exact_desktop_origins_on_community_chat(self):
        for origin in TAURI_ORIGINS:
            for path, method, headers in (
                (
                    '/api/v1/community-chat/auth/device/start/',
                    'POST',
                    'content-type',
                ),
                (
                    '/api/v1/community-chat/session/',
                    'GET',
                    'authorization',
                ),
                (
                    '/api/v1/community-chat/usage/token/',
                    'PATCH',
                    'authorization, content-type',
                ),
            ):
                with self.subTest(origin=origin, path=path, method=method):
                    response = self.client.options(
                        path,
                        HTTP_ORIGIN=origin,
                        HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
                        HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
                    )

                    self.assertEqual(
                        response.headers.get('Access-Control-Allow-Origin'),
                        origin,
                    )
                    self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

            unrelated = self.client.options(
                '/api/v1/auth/logout/',
                HTTP_ORIGIN=origin,
                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type',
            )
            self.assertNotIn('Access-Control-Allow-Origin', unrelated.headers)
            self.assertNotIn('Access-Control-Allow-Credentials', unrelated.headers)

    def test_desktop_cors_rejects_unknown_headers_and_methods(self):
        for method, headers in (
            ('CONNECT', 'content-type'),
            ('POST', 'cookie'),
            ('POST', 'x-untrusted-header'),
        ):
            with self.subTest(method=method, headers=headers):
                response = self.client.options(
                    '/api/v1/community-chat/session/',
                    HTTP_ORIGIN=TAURI_ORIGINS[0],
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
                )

                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    @override_settings(
        CORS_ALLOWED_ORIGINS=[OPERATIONS_ORIGIN, 'tauri://localhost'],
        CORS_ALLOW_CREDENTIALS=True,
    )
    def test_desktop_cors_strips_global_credential_header_defensively(self):
        response = self.client.options(
            '/api/v1/community-chat/session/',
            HTTP_ORIGIN='tauri://localhost',
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization',
        )

        self.assertEqual(
            response.headers.get('Access-Control-Allow-Origin'),
            'tauri://localhost',
        )
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    def test_cors_preflight_does_not_allow_lookalike_or_untrusted_origins(self):
        for origin in (
            PLANE_ORIGIN,
            'https://ops.mlai.au.attacker.example',
            'https://admin.mlai.au.attacker.example',
            'https://attacker.example',
            'http://ops.mlai.au',
            'tauri://attacker.example',
            'http://tauri.localhost.attacker.example',
        ):
            with self.subTest(origin=origin):
                response = self.client.options(
                    '/api/v1/auth/logout/',
                    HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                )

                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
