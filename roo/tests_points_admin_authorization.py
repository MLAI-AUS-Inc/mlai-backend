"""Real permission/view checks with no database, migrations, or external calls."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import PointsAdmin
from .permissions import is_points_super_admin
from .views import PointsAdminViewSet


@override_settings(
    ROO_API_KEY='synthetic-roo-key', INTERNAL_API_KEY='synthetic-internal-key',
    MLAI_API_KEY='synthetic-website-key',
    POINTS_SUPER_ADMIN_SLACK_ID='UDEVADMIN', POINTS_SUPER_ADMIN_SLACK_TEAM_ID='TDEV',
)
class PointsAdminAuthorizationTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        manager_patch = patch('roo.views.PointsAdmin.objects')
        self.manager = manager_patch.start()
        self.addCleanup(manager_patch.stop)
        lookup_patch = patch('roo.views.PointsService.get_user_by_slack_id', return_value=None)
        self.lookup = lookup_patch.start()
        self.addCleanup(lookup_patch.stop)
        self.admin = PointsAdmin(slack_user_id='UTARGET', is_active=True, weekly_allowance=100)
        self.admin.save = Mock()
        self.manager.filter.return_value.first.return_value = self.admin
        self.manager.create.return_value = self.admin

    def request(self, method='post', *, user='UDEVADMIN', team='TDEV', key='synthetic-roo-key', superuser=False):
        actions = {'post': 'create', 'patch': 'partial_update', 'put': 'update', 'delete': 'destroy'}
        data = {'requester_slack_id': user, 'slack_team_id': team,
                'target_slack_id': 'UTARGET', 'weekly_allowance': 150}
        request = getattr(self.factory, method)(
            '/api/v1/points/admins/UTARGET/', data, format='json', HTTP_X_API_KEY=key,
        )
        if superuser:
            force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=True))
        return PointsAdminViewSet.as_view({method: actions[method]})(request, slack_user_id='UTARGET')

    def assert_no_changes(self):
        self.manager.assert_not_called()
        self.assertEqual(self.manager.mock_calls, [])
        self.lookup.assert_not_called()
        self.admin.save.assert_not_called()

    def test_configured_dev_and_existing_production_identity_can_mutate(self):
        for user, team in (('UDEVADMIN', 'TDEV'), ('U05QPB483K9', 'TPROD')):
            for method in ('post', 'patch', 'put', 'delete'):
                with self.subTest(user=user, method=method), override_settings(
                    POINTS_SUPER_ADMIN_SLACK_ID=user, POINTS_SUPER_ADMIN_SLACK_TEAM_ID=team,
                ):
                    self.admin.is_active = True
                    if method == 'post':
                        self.manager.filter.return_value.first.return_value = None
                    else:
                        self.manager.filter.return_value.first.return_value = self.admin
                    response = self.request(method, user=user, team=team)
                    self.assertEqual(response.status_code, 201 if method == 'post' else 200)
                    if method == 'post':
                        self.assertEqual(self.manager.create.call_args.kwargs['added_by_slack_id'], user)
                    elif method in ('patch', 'put'):
                        self.assertEqual(self.admin.weekly_allowance, 150)
                        self.admin.save.assert_called_with(update_fields=['weekly_allowance'])
                    else:
                        self.assertFalse(self.admin.is_active)
                        self.admin.save.assert_called_with(update_fields=['is_active'])

    def test_wrong_or_missing_identity_denied_before_database(self):
        for method in ('post', 'patch', 'put', 'delete'):
            for user, team in (('UOTHER', 'TDEV'), ('UDEVADMIN', 'TOTHER'),
                               ('UDEVADMIN', ''), ('', 'TDEV'), ('U05QPB483K9', 'TDEV')):
                with self.subTest(method=method, user=user, team=team):
                    response = self.request(method, user=user, team=team)
                    self.assertIn(response.status_code, (400, 403))
        self.assert_no_changes()

    def test_unset_or_malformed_configuration_denies_even_matching_request(self):
        for user, team in (('', 'TDEV'), ('UDEVADMIN', ''), (None, None),
                           ('<@UDEVADMIN>', 'TDEV'), ('B123', 'TDEV'), ('UDEVADMIN ', 'TDEV'),
                           ('UDEVADMIN,UOTHER', 'TDEV'), ('UDEVADMIN', 'CDEV'),
                           ('UDEVADMIN', 'TDEV,TOTHER')):
            with self.subTest(user=user, team=team), override_settings(
                POINTS_SUPER_ADMIN_SLACK_ID=user, POINTS_SUPER_ADMIN_SLACK_TEAM_ID=team,
            ):
                self.assertFalse(is_points_super_admin(user, team))
                for method in ('post', 'patch', 'put', 'delete'):
                    self.assertIn(self.request(method, user=user, team=team).status_code, (400, 403))
        self.assert_no_changes()

    def test_other_keys_and_django_superusers_cannot_bypass_service_auth(self):
        for method in ('post', 'patch', 'put', 'delete'):
            for key in ('synthetic-internal-key', 'synthetic-website-key', 'invalid', ''):
                with self.subTest(method=method, key=key):
                    self.assertIn(self.request(method, key=key, superuser=True).status_code, (401, 403))
            self.assertEqual(self.request(method, user='UOTHER', superuser=True).status_code, 403)
        self.assert_no_changes()

    def test_shared_service_keys_cannot_assert_actor(self):
        for name in ('INTERNAL_API_KEY', 'MLAI_API_KEY'):
            with self.subTest(name=name), override_settings(**{name: 'synthetic-roo-key'}):
                for method in ('post', 'patch', 'put', 'delete'):
                    self.assertIn(self.request(method).status_code, (401, 403))
        self.assert_no_changes()

    def test_read_access_keeps_existing_service_credentials(self):
        with patch.object(PointsAdminViewSet, 'get_queryset', return_value=[]):
            for key in ('synthetic-roo-key', 'synthetic-internal-key', 'synthetic-website-key'):
                with self.subTest(key=key):
                    request = self.factory.get('/api/v1/points/admins/', HTTP_X_API_KEY=key)
                    response = PointsAdminViewSet.as_view({'get': 'list'})(request)
                    self.assertEqual(response.status_code, 200)
        self.assert_no_changes()

    def test_django_role_helpers_are_independent(self):
        from .permissions import is_points_admin_user
        self.assertTrue(is_points_admin_user(SimpleNamespace(is_authenticated=True, is_superuser=True)))
        self.assertFalse(is_points_super_admin('UOTHER', 'TDEV'))
        self.assertFalse(is_points_super_admin('UDEVADMIN'))
        self.assertTrue(is_points_super_admin('UDEVADMIN', 'TDEV'))
