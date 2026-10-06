from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from core.middleware import DesktopAuthCorsMiddleware


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

    @override_settings(DEBUG=False, ALLOWED_HOSTS=['api.mlai.au'])
    def test_browser_startup_lifecycle_preflight_allows_actual_mutation_headers(self):
        from corsheaders.defaults import default_headers

        requested = {'content-type', 'idempotency-key', 'x-request-id'}
        for path in (
            '/api/v1/my-startup/vibe-marketing/article-system-setup',
            '/api/v1/my-startup/vibe-marketing/scan',
            '/api/v1/my-startup/vibe-marketing/website-connection/reconnect',
        ):
            with self.subTest(path=path):
                response = self.client.options(
                    path, HTTP_HOST='api.mlai.au', secure=True,
                    HTTP_ORIGIN='https://chat.mlai.au',
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS=','.join(sorted(requested)),
                )
                allowed = {
                    value.strip().lower()
                    for value in response.headers.get('Access-Control-Allow-Headers', '').split(',')
                }
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), 'https://chat.mlai.au')
                self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')
                self.assertTrue(requested.issubset(allowed))
                self.assertTrue(set(default_headers).issubset(allowed))
                self.assertNotIn('*', allowed)
                self.assertNotIn('cookie', allowed)
                self.assertNotIn('x-untrusted-header', allowed)
                self.assertIn('POST', response.headers.get('Access-Control-Allow-Methods', ''))

    def test_browser_startup_lifecycle_preflight_keeps_untrusted_origins_denied(self):
        for origin in ('https://chat.mlai.au.attacker.example', 'https://attacker.example', 'null'):
            with self.subTest(origin=origin):
                response = self.client.options(
                    '/api/v1/my-startup/vibe-marketing/article-system-setup',
                    HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type,idempotency-key,x-request-id',
                )
                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    def test_native_startup_idempotency_keeps_method_and_response_credential_boundaries(self):
        response = self.client.options(
            '/api/v1/my-startup/vibe-marketing/article-system-setup',
            HTTP_ORIGIN=TAURI_ORIGINS[0],
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='CONNECT',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='idempotency-key',
        )
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

        def accidental_credential_response(request):
            response = HttpResponse(status=202)
            response['Access-Control-Allow-Credentials'] = 'true'
            return response

        middleware = DesktopAuthCorsMiddleware(accidental_credential_response)
        for origin in TAURI_ORIGINS:
            with self.subTest(origin=origin):
                request = RequestFactory().post(
                    '/api/v1/my-startup/vibe-marketing/article-system-setup',
                    HTTP_ORIGIN=origin, HTTP_IDEMPOTENCY_KEY='synthetic-request-key',
                )
                response = middleware(request)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), origin)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
                self.assertIn('idempotency-key', response.headers['Access-Control-Allow-Headers'])

    def test_native_startup_lifecycle_preflight_allows_idempotency_without_cookies(self):
        for origin in TAURI_ORIGINS:
            with self.subTest(origin=origin):
                response = self.client.options(
                    '/api/v1/my-startup/vibe-marketing/article-system-setup',
                    HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS='Authorization, Content-Type, Idempotency-Key, X-Request-ID',
                )
                allowed = {
                    value.strip().lower()
                    for value in response.headers.get('Access-Control-Allow-Headers', '').split(',')
                }
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), origin)
                self.assertIn('idempotency-key', allowed)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
                self.assertNotIn('cookie', allowed)

        for path, headers in (
            ('/api/v1/community-chat/session/', 'idempotency-key'),
            ('/api/v1/my-startup-other/', 'idempotency-key'),
            ('/api/v1/my-startup/vibe-marketing/article-system-setup', 'idempotency-key,cookie'),
            ('/api/v1/my-startup/vibe-marketing/article-system-setup', 'idempotency-key,x-untrusted-header'),
        ):
            with self.subTest(path=path, headers=headers):
                response = self.client.options(
                    path, HTTP_ORIGIN=TAURI_ORIGINS[0],
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
                )
                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

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

    def test_startup_desktop_preflight_supports_account_reads_and_catalogue_saves(self):
        for origin in TAURI_ORIGINS:
            for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
                with self.subTest(origin=origin, method=method):
                    response = self.client.options(
                        '/api/v1/my-startup/vibe-marketing/editorial-catalog/',
                        HTTP_ORIGIN=origin,
                        HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
                        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization, content-type, x-request-id',
                    )
                    self.assertEqual(response.status_code, 204)
                    self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), origin)
                    self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    def test_startup_desktop_cors_keeps_other_paths_origins_and_cookie_headers_denied(self):
        for path, origin, headers in (
            ('/api/v1/my-startup-other/', TAURI_ORIGINS[0], 'authorization'),
            ('/api/v1/founder-tools/profile/', TAURI_ORIGINS[0], 'authorization'),
            ('/api/v1/my-startup/auth/me/', 'tauri://localhost.attacker.example', 'authorization'),
            ('/api/v1/my-startup/auth/me/', 'null', 'authorization'),
            ('/api/v1/my-startup/auth/me/', TAURI_ORIGINS[0], 'cookie'),
        ):
            with self.subTest(path=path, origin=origin, headers=headers):
                response = self.client.options(
                    path, HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
                )
                self.assertNotIn('Access-Control-Allow-Origin', response.headers)
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

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
