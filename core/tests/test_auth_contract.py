from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from hospital.models import Team as HospitalTeam
from roo.models import PointsAdmin

User = get_user_model()


class AuthContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_check_user_returns_true_for_existing_email(self):
        User.objects.create_user(email='existing@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/check-user/',
            {'email': 'existing@example.com', 'app': 'vibe-raising'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['user_exists'])

    def test_check_user_returns_false_for_missing_email(self):
        response = self.client.post(
            '/api/v1/auth/check-user/',
            {'email': 'missing@example.com', 'app': 'vibe-raising'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['user_exists'])

    def test_healthhack_admin_check_rejects_non_superuser(self):
        User.objects.create_user(email='closed@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/check-user/',
            {
                'email': 'closed@example.com',
                'app': 'hospital',
                'healthhack_admin_only': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.data['detail'],
            'HealthHack has closed. Administrator access only.',
        )

    def test_healthhack_admin_check_accepts_superuser(self):
        user = User.objects.create_user(email='healthhack-admin@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        response = self.client.post(
            '/api/v1/auth/check-user/',
            {
                'email': user.email,
                'app': 'hospital',
                'healthhack_admin_only': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['user_exists'])

    def test_current_user_returns_401_for_anonymous_requests(self):
        response = self.client.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['error'], 'Not authenticated')
        self.assertTrue(getattr(response, '_has_been_logged', False))

    def test_current_user_returns_authenticated_user_payload(self):
        user = User.objects.create_user(
            email='current-user@example.com',
            role='participant',
            first_name='Current',
            last_name='User',
        )
        self.client.force_authenticate(user=user)

        response = self.client.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['email'], 'current-user@example.com')
        self.assertEqual(response.data['full_name'], 'Current User')
        self.assertEqual(response.data['role'], 'participant')
        self.assertFalse(response.data['has_team'])
        # Non-admin users carry an explicit False so the frontend can gate the
        # Vibe Raising admin dashboard off this flag.
        self.assertIn('is_vibe_raising_admin', response.data)
        self.assertFalse(response.data['is_vibe_raising_admin'])

    def test_current_user_marks_superuser_as_vibe_raising_admin(self):
        user = User.objects.create_user(email='superuser@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
        self.client.force_authenticate(user=user)

        response = self.client.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['is_vibe_raising_admin'])

    def test_current_user_derives_has_team_from_membership(self):
        user = User.objects.create_user(
            email='team-user@example.com',
            first_name='Team',
            last_name='User',
        )
        team = HospitalTeam.objects.create(team_name='Team Contract')
        team.members.add(user)
        self.client.force_authenticate(user=user)

        response = self.client.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['has_team'])
        self.assertEqual(response.data['team']['team_name'], 'Team Contract')
        self.assertEqual(response.data['team']['members'][0]['role'], 'participant')

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link', return_value='http://localhost:5173/verify?token=abc')
    def test_create_user_returns_contract_shape(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'new@example.com',
                'firstName': 'Jane',
                'lastName': 'Doe',
                'phone': '412345678',
                'role': 'participant',
                'app': 'hospital',
                'next': '/hospital/app',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertIn('id', response.data)
        self.assertEqual(response.data['email'], 'new@example.com')
        self.assertEqual(response.data['full_name'], 'Jane Doe')
        # The magic link must NOT be exposed in the API response; it is only emailed.
        self.assertNotIn('magic_link', response.data)
        self.assertTrue(response.data['magic_link_sent'])

        emailed_magic_link = mock_send.call_args.args[1]
        self.assertIn('app=hospital', emailed_magic_link)
        self.assertIn('next=/hospital/app', emailed_magic_link)

        created_user = User.objects.get(email='new@example.com')
        self.assertFalse(created_user.is_active)
        mock_generate.assert_called_once()
        mock_send.assert_called_once()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_healthhack_admin_login_does_not_create_participant(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'closed-new-user@example.com',
                'firstName': 'Closed',
                'lastName': 'User',
                'app': 'hospital',
                'healthhack_admin_only': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email='closed-new-user@example.com').exists())
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link', return_value='http://localhost:5173/verify?token=vibe')
    def test_create_user_normalizes_vibe_raising_to_founder_tools_contract(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'founder@example.com',
                'firstName': 'Vibe',
                'lastName': 'Founder',
                'app': 'vibe-raising',
                'next': '/vibe-raising',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn('magic_link', response.data)
        emailed_magic_link = mock_send.call_args.args[1]
        self.assertIn('app=founder-tools', emailed_magic_link)
        self.assertIn('next=/vibe-raising', emailed_magic_link)
        mock_generate.assert_called_once()
        mock_send.assert_called_once()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_create_user_rejects_unsupported_app(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'unsupported-app@example.com',
                'firstName': 'Unsupported',
                'lastName': 'App',
                'app': 'unknown-product',
                'next': '/unknown-product',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Unsupported app.')
        self.assertFalse(User.objects.filter(email='unsupported-app@example.com').exists())
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @override_settings(MEDHACK_URL='http://localhost:3000')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_uses_hospital_frontend_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='origin@example.com', role='participant')

        self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'origin@example.com', 'app': 'hospital', 'next': '/hospital/app'},
            format='json',
        )

        mock_generate.assert_called_once_with(user, base_url='http://localhost:3000')
        mock_send.assert_called_once()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_healthhack_admin_login_does_not_email_non_superuser(self, mock_generate, mock_send):
        User.objects.create_user(email='closed-link@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {
                'email': 'closed-link@example.com',
                'app': 'hospital',
                'next': '/hospital/app',
                'healthhack_admin_only': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @override_settings(VIBE_RAISING_URL='http://localhost:5173')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_uses_vibe_raising_frontend_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='vibe-origin@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'vibe-origin@example.com', 'app': 'vibe-raising', 'next': '/vibe-raising'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['user_exists'])
        self.assertTrue(response.data['magic_link_sent'])
        mock_generate.assert_called_once_with(user, base_url='http://localhost:5173')
        mock_send.assert_called_once()

    @override_settings(WATT_THE_HACK_URL='http://localhost:5173')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_uses_watt_the_hack_frontend_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='watt-origin@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'watt-origin@example.com', 'app': 'watt-the-hack', 'next': '/watt-the-hack/dashboard'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['magic_link_sent'])
        mock_generate.assert_called_once_with(user, base_url='http://localhost:5173')
        mock_send.assert_called_once()

    @override_settings(VIBE_RAISING_URL='http://localhost:5173')
    @patch('core.views.send_magic_link_email')
    def test_send_magic_link_returns_missing_user_for_vibe_raising(self, mock_send):
        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'new-vibe@example.com', 'app': 'vibe-raising', 'next': '/vibe-raising'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['user_exists'])
        self.assertEqual(response.data['message'], 'User does not exist.')
        mock_send.assert_not_called()

    @override_settings(VIBE_RAISING_URL='http://localhost:5173')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link', return_value='http://localhost:5173/verify-email?token=next-query')
    def test_send_magic_link_encodes_nested_next_query(self, mock_generate, mock_send):
        user = User.objects.create_user(email='nested-next@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {
                'email': 'nested-next@example.com',
                'app': 'founder-tools',
                'next': '/founder-tools/marketing/create?step=baseline&googleBaseline=refresh',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['magic_link_sent'])
        magic_link = mock_send.call_args.args[1]
        self.assertIn(
            'next=/founder-tools/marketing/create%3Fstep%3Dbaseline%26googleBaseline%3Drefresh',
            magic_link,
        )
        parsed_params = parse_qs(urlparse(magic_link).query)
        self.assertEqual(parsed_params['token'], ['next-query'])
        self.assertEqual(parsed_params['app'], ['founder-tools'])
        self.assertEqual(
            parsed_params['next'],
            ['/founder-tools/marketing/create?step=baseline&googleBaseline=refresh'],
        )
        mock_generate.assert_called_once_with(user, base_url='http://localhost:5173')
        mock_send.assert_called_once()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_rejects_unsupported_app(self, mock_generate, mock_send):
        User.objects.create_user(email='unsupported-origin@example.com', role='participant')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {
                'email': 'unsupported-origin@example.com',
                'app': 'unknown-product',
                'next': '/unknown-product',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Unsupported app.')
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @override_settings(MEDHACK_URL='', DEFAULT_FRONTEND_URL='http://localhost:3000')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_falls_back_to_default_frontend_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='default-origin@example.com', role='participant')

        self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'default-origin@example.com', 'app': 'hospital', 'next': '/hospital/app'},
            format='json',
        )

        mock_generate.assert_called_once_with(user, base_url='http://localhost:3000')
        mock_send.assert_called_once()

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'verify@example.com'})
    def test_verify_healthhack_admin_magic_link_returns_redirect_and_user_id(self, mock_verify):
        user = User.objects.create_user(
            email='verify@example.com',
            role='participant',
            first_name='Verify',
            last_name='User',
        )
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=hospital&next=/hospital/app'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.id)
        self.assertEqual(response.data['user']['email'], 'verify@example.com')
        self.assertEqual(response.data['user']['role'], 'participant')
        self.assertFalse(response.data['user']['has_team'])
        self.assertEqual(response.data['redirect'], '/hospital/app')
        self.assertTrue(response.data['next_url'].endswith('/hospital/app'))
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)
        self.assertIn('sessionid', response.cookies)
        self.assertEqual(str(user.id), self.client.session.get('_auth_user_id'))

        mock_verify.assert_called_once_with('test-token')

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'closed-verify@example.com'})
    def test_verify_healthhack_magic_link_rejects_non_superuser(self, mock_verify):
        User.objects.create_user(
            email='closed-verify@example.com',
            role='participant',
        )

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=hospital&next=/hospital/app'
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.data['detail'],
            'HealthHack has closed. Administrator access only.',
        )
        self.assertNotIn('access_token', response.cookies)
        self.assertNotIn('refresh_token', response.cookies)
        self.assertNotIn('_auth_user_id', self.client.session)
        mock_verify.assert_called_once_with('test-token')

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'admin-verify@example.com'})
    def test_verify_magic_link_admin_app_returns_to_fixed_operations_frontend(self, mock_verify):
        user = User.objects.create_user(
            email='admin-verify@example.com',
            first_name='Ada',
            last_name='Admin',
        )
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/',
            {
                'token': 'test-token',
                'app': 'admin',
                'next': '/updates/42?mode=review',
                'origin': 'https://attacker.example',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.id)
        self.assertEqual(response.data['redirect'], '/updates/42?mode=review')
        self.assertEqual(response.data['next_url'], 'https://ops.mlai.au/updates/42?mode=review')
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)

    @patch('core.views.send_magic_link_email')
    @patch(
        'core.views.generate_magic_link',
        return_value='https://ops.mlai.au/verify-email?token=ops-token',
    )
    def test_send_magic_link_admin_app_uses_fixed_operations_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='ops-admin@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        with self.assertLogs('core.views', level='INFO') as captured_logs:
            response = self.client.post(
                '/api/v1/auth/send-magic-link/',
                {
                    'email': user.email,
                    'app': 'admin',
                    'next': '/review?queue=monthly',
                    'origin': 'https://attacker.example',
                    'redirect_uri': 'https://attacker.example/callback',
                },
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        mock_generate.assert_called_once_with(user, base_url='https://ops.mlai.au')
        emailed_link = mock_send.call_args.args[1]
        parsed_link = urlparse(emailed_link)
        self.assertEqual(f'{parsed_link.scheme}://{parsed_link.netloc}', 'https://ops.mlai.au')
        self.assertEqual(
            parse_qs(parsed_link.query),
            {
                'token': ['ops-token'],
                'app': ['admin'],
                'next': ['/review?queue=monthly'],
            },
        )
        emitted_logs = '\n'.join(captured_logs.output)
        self.assertNotIn('ops-token', emitted_logs)
        self.assertNotIn('/verify-email?', emitted_logs)

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_admin_rejects_open_redirect_targets(self, mock_generate, mock_send):
        user = User.objects.create_user(email='ops-open-redirect@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        for target in (
            'https://attacker.example/path',
            '//attacker.example/path',
            r'\\attacker.example\path',
            r'/\attacker.example/path',
            'updates/42',
            '/updates#https://attacker.example',
        ):
            with self.subTest(target=target):
                response = self.client.post(
                    '/api/v1/auth/send-magic-link/',
                    {'email': user.email, 'app': 'admin', 'next': target},
                    format='json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data['error'], 'Invalid next path.')

        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_admin_rejects_encoded_path_separators(self, mock_generate, mock_send):
        user = User.objects.create_user(email='ops-encoded-separator@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        for target in (
            '/%2f%2fattacker.example/path',
            '/%5c%5cattacker.example/path',
            '/%252f%252fattacker.example/path',
            '/updates/%2F42',
        ):
            with self.subTest(target=target):
                response = self.client.post(
                    '/api/v1/auth/send-magic-link/',
                    {'email': user.email, 'app': 'admin', 'next': target},
                    format='json',
                )
                self.assertEqual(response.status_code, 400)

        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_admin_rejects_non_admin_user(self, mock_generate, mock_send):
        user = User.objects.create_user(email='not-ops-admin@example.com')

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': user.email, 'app': 'admin', 'next': '/'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['detail'], 'MLAI Operations administrator access only.')
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch('core.views.send_magic_link_email')
    @patch(
        'core.views.generate_magic_link',
        return_value='https://ops.mlai.au/verify-email?token=points-admin-token',
    )
    def test_send_magic_link_admin_accepts_linked_points_admin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='points-ops-admin@example.com')
        PointsAdmin.objects.create(
            user=user,
            slack_user_id='UOPSADMIN',
            role='admin',
            is_active=True,
        )

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': user.email, 'app': 'admin', 'next': '/'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        mock_generate.assert_called_once_with(user, base_url='https://ops.mlai.au')
        mock_send.assert_called_once()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_admin_rejects_inactive_superuser(self, mock_generate, mock_send):
        user = User.objects.create_user(email='inactive-ops-superuser@example.com')
        user.is_superuser = True
        user.is_active = False
        user.save(update_fields=['is_active', 'is_superuser'])

        response = self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': user.email, 'app': 'admin', 'next': '/'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch(
        'core.views.verify_magic_link',
        return_value={'kind': 'user', 'email': 'inactive-points-ops-admin@example.com'},
    )
    def test_verify_magic_link_admin_rejects_inactive_points_admin_without_reactivation(
        self,
        mock_verify,
    ):
        user = User.objects.create_user(email='inactive-points-ops-admin@example.com')
        user.is_active = False
        user.save(update_fields=['is_active'])
        PointsAdmin.objects.create(
            user=user,
            slack_user_id='UINACTIVEOPS',
            role='admin',
            is_active=True,
        )

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/',
            {'token': 'test-token', 'app': 'admin', 'next': '/'},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('access_token', response.cookies)
        self.assertNotIn('refresh_token', response.cookies)
        self.assertNotIn('sessionid', response.cookies)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'not-ops-verify@example.com'})
    def test_verify_magic_link_admin_rejects_non_admin_user(self, mock_verify):
        user = User.objects.create_user(email='not-ops-verify@example.com')
        user.is_active = False
        user.save(update_fields=['is_active'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/',
            {'token': 'test-token', 'app': 'admin', 'next': '/'},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('access_token', response.cookies)
        self.assertNotIn('refresh_token', response.cookies)
        self.assertNotIn('sessionid', response.cookies)
        self.assertNotIn('_auth_user_id', self.client.session)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'ops-invalid-next@example.com'})
    def test_verify_magic_link_admin_rejects_invalid_next_before_login(self, mock_verify):
        user = User.objects.create_user(email='ops-invalid-next@example.com')
        user.is_superuser = True
        user.is_active = False
        user.save(update_fields=['is_active', 'is_superuser'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/',
            {'token': 'test-token', 'app': 'admin', 'next': '/%252f%252fattacker.example'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn('access_token', response.cookies)
        self.assertNotIn('refresh_token', response.cookies)
        self.assertNotIn('sessionid', response.cookies)
        self.assertNotIn('_auth_user_id', self.client.session)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_create_user_rejects_admin_app_context(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'self-provisioned-ops-admin@example.com',
                'firstName': 'Untrusted',
                'app': 'admin',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email='self-provisioned-ops-admin@example.com').exists())
        mock_generate.assert_not_called()
        mock_send.assert_not_called()

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'vibe-verify@example.com'})
    def test_verify_magic_link_defaults_to_founder_tools_for_vibe_raising_alias(self, mock_verify):
        user = User.objects.create_user(
            email='vibe-verify@example.com',
            role='participant',
            first_name='Vibe',
            last_name='Verify',
        )
        user.is_active = False
        user.save(update_fields=['is_active'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=vibe-raising'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.id)
        self.assertEqual(response.data['redirect'], '/founder-tools')
        self.assertTrue(response.data['next_url'].endswith('/founder-tools'))
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'watt-verify@example.com'})
    def test_verify_magic_link_defaults_to_watt_the_hack_dashboard(self, mock_verify):
        user = User.objects.create_user(
            email='watt-verify@example.com',
            role='participant',
            first_name='Watt',
            last_name='Verify',
        )
        user.is_active = False
        user.save(update_fields=['is_active'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=watt-the-hack'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.id)
        self.assertEqual(response.data['redirect'], '/watt-the-hack/dashboard')
        self.assertTrue(response.data['next_url'].endswith('/watt-the-hack/dashboard'))
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)

    @patch('core.views.verify_magic_link', return_value={'kind': 'user', 'email': 'unsupported-verify@example.com'})
    def test_verify_magic_link_rejects_unsupported_app(self, mock_verify):
        user = User.objects.create_user(
            email='unsupported-verify@example.com',
            role='participant',
            first_name='Unsupported',
            last_name='Verify',
        )
        user.is_active = False
        user.save(update_fields=['is_active'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=unknown-product'
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Unsupported app.')
        self.assertNotIn('access_token', response.cookies)
        self.assertNotIn('refresh_token', response.cookies)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    @patch(
        'core.views.verify_magic_link',
        return_value={'kind': 'pending_signup', 'pending_signup_id': 1, 'email': 'pending-vibe@example.com'},
    )
    def test_verify_magic_link_rejects_pending_signup_tokens(self, mock_verify):
        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=vibe-raising&next=/vibe-raising'
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Invalid or expired token.')
        self.assertFalse(User.objects.filter(email='pending-vibe@example.com').exists())
